"""Reference sample: claims document ingestion pipeline.

Databricks (Unity Catalog) -> Azure AI Content Understanding -> Blob Storage -> Azure AI Search indexer
-> status written back to Databricks.

Not production code. See README "Production considerations".
"""
import json
import logging
import mimetypes
import os
import time

from azure.ai.contentunderstanding import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import AnalysisInput
from azure.core.exceptions import HttpResponseError, ServiceRequestError
from azure.identity import DefaultAzureCredential
from azure.search.documents.indexes import SearchIndexerClient
from azure.storage.blob import BlobServiceClient, ContentSettings
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem, StatementState
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# The Azure SDKs log every HTTP request at INFO, which buries the pipeline's own messages.
logging.getLogger("azure").setLevel(logging.WARNING)
log = logging.getLogger("claims-pipeline")

# --- Configuration (all from environment; see .env.example) ---------------------------------
WAREHOUSE_ID = os.environ["DATABRICKS_WAREHOUSE_ID"]
DOCS_TABLE = os.environ["DATABRICKS_DOCS_TABLE"]        # e.g. claims.bronze.claim_documents
STATUS_TABLE = os.environ["DATABRICKS_STATUS_TABLE"]    # e.g. claims.bronze.claim_document_status
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))
CU_ENDPOINT = os.environ["CU_ENDPOINT"]
CU_ANALYZER_ID = os.getenv("CU_ANALYZER_ID", "prebuilt-documentSearch")
# The SDK defaults to a newer *preview* API version, so pin the GA version explicitly.
CU_API_VERSION = os.getenv("CU_API_VERSION", "2025-11-01")
STORAGE_ACCOUNT_URL = os.environ["STORAGE_ACCOUNT_URL"]
ANALYZED_CONTAINER = os.getenv("ANALYZED_CONTAINER", "analyzed")
SEARCH_ENDPOINT = os.environ["SEARCH_ENDPOINT"]
SEARCH_INDEXER_NAME = os.environ["SEARCH_INDEXER_NAME"]

# One credential for every Azure service: managed identity in Azure, `az login` on a laptop.
credential = DefaultAzureCredential()
# WorkspaceClient reads DATABRICKS_HOST plus PAT, OAuth M2M, or Azure MSI settings from the environment.
dbx = WorkspaceClient()
cu_client = ContentUnderstandingClient(CU_ENDPOINT, credential, api_version=CU_API_VERSION)
blob_service = BlobServiceClient(STORAGE_ACCOUNT_URL, credential=credential)
indexer_client = SearchIndexerClient(SEARCH_ENDPOINT, credential)


def with_retry(fn, *args, attempts=3, base_delay=2, **kwargs):
    """Retry transient Azure failures (throttling, 5xx, network) with exponential backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except (HttpResponseError, ServiceRequestError) as e:
            status = getattr(e, "status_code", None)
            # 4xx other than 408/429 means a bad request; retrying won't help.
            retryable = status is None or status in (408, 429) or status >= 500
            if not retryable or attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1)
            log.warning("  transient error (%s), retry %d/%d in %ss", status, attempt, attempts - 1, delay)
            time.sleep(delay)


def run_sql(statement, params=None):
    """Run SQL on a Databricks SQL warehouse and return rows as dicts.
    Table names come from config (identifiers can't be bound); values always use :named parameters."""
    resp = dbx.statement_execution.execute_statement(
        statement=statement, warehouse_id=WAREHOUSE_ID, wait_timeout="30s",
        parameters=[StatementParameterListItem(name=k, value=v) for k, v in (params or {}).items()])
    while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        resp = dbx.statement_execution.get_statement(resp.statement_id)
    if resp.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"SQL failed: {resp.status.error}")
    if not resp.result or not resp.result.data_array:
        return []
    cols = [c.name for c in resp.manifest.schema.columns]
    return [dict(zip(cols, row)) for row in resp.result.data_array]


# --- Step 1: find work and fetch files from Databricks ---------------------------------------
def get_pending_documents():
    """Documents with no 'processed' row in the status table. 'failed' ones are retried on the next run."""
    return run_sql(f"""
        SELECT d.claim_id, d.document_id, d.document_name, d.volume_path
        FROM {DOCS_TABLE} d
        LEFT ANTI JOIN (SELECT document_id FROM {STATUS_TABLE} WHERE status = 'processed') s
          ON d.document_id = s.document_id
        LIMIT {BATCH_SIZE}""")


def download_from_databricks(volume_path):
    """Read the file bytes from a Unity Catalog Volume (path like /Volumes/catalog/schema/volume/file.pdf)."""
    return dbx.files.download(volume_path).contents.read()


# --- Step 2: Content Understanding -------------------------------------------------------------
def analyze_with_cu(file_bytes, document_name):
    """Extract Markdown, a summary, and figure descriptions with prebuilt-documentSearch.
    The bytes go inline (AnalysisInput.data), so there's no need to stage the file in a 'raw' container."""
    mime_type = mimetypes.guess_type(document_name)[0] or "application/octet-stream"
    poller = cu_client.begin_analyze(
        CU_ANALYZER_ID, inputs=[AnalysisInput(data=file_bytes, mime_type=mime_type, name=document_name)])
    result = poller.result()  # blocks until the long-running operation finishes
    content = result.contents[0]  # one input, so one content item
    summary_field = (content.fields or {}).get("Summary")
    return {
        # Figure descriptions also appear inline in the Markdown, as the image title text,
        # so they get indexed together with the surrounding text.
        "markdown": content.markdown or "",
        "summary": summary_field.value if summary_field else "",
        "figure_descriptions": [
            {"id": f.id, "description": f.description} for f in (getattr(content, "figures", None) or [])],
        "raw": result.as_dict(),
    }


# --- Step 3: Blob Storage ----------------------------------------------------------------------
def upload_to_blob(doc, analysis):
    """Write {claim_id}/{document_id}.md (what Search indexes) plus the raw CU JSON (for debugging).
    The blob metadata becomes filterable fields in the search index."""
    container = blob_service.get_container_client(ANALYZED_CONTAINER)
    prefix = f"{doc['claim_id']}/{doc['document_id']}"
    # Put the summary at the top so it appears in the first chunk.
    body = f"## Summary\n\n{analysis['summary']}\n\n---\n\n{analysis['markdown']}"
    # Blob metadata must be ASCII; non-ASCII Volume paths would need encoding (e.g. URL-quoting).
    metadata = {"claim_id": str(doc["claim_id"]), "document_id": str(doc["document_id"]),
                "source_path": doc["volume_path"]}
    # overwrite=True keeps reruns idempotent: reprocessing a document replaces its blobs.
    with_retry(container.upload_blob, f"{prefix}.md", body.encode("utf-8"), overwrite=True,
               metadata=metadata, content_settings=ContentSettings(content_type="text/markdown"))
    with_retry(container.upload_blob, f"{prefix}.cu.json", json.dumps(analysis["raw"], indent=2, default=str),
               overwrite=True, metadata=metadata,
               content_settings=ContentSettings(content_type="application/json"))


# --- Step 4: Azure AI Search -----------------------------------------------------------------
# Assumed to exist already (this script doesn't create them):
#   Data source: blob container ANALYZED_CONTAINER, accessed with the Search service's managed identity.
#   Indexer:     parsingMode = "markdown" (markdownParsingSubmode "oneToOne" when a skillset does the chunking),
#                indexedFileNameExtensions = ".md" so the *.cu.json debug files are skipped,
#                field mappings for claim_id / document_id / source_path (blob metadata, mapped by name).
#   Skillset:    Text Split skill (chunking) + Azure OpenAI Embedding skill, with index projections
#                writing one search document per chunk into a vector-enabled index.
def run_search_indexer(timeout_s=900, poll_s=15):
    """Run the indexer once per batch, not once per document: an indexer can only run one
    execution at a time, and on-demand runs are rate-limited."""
    # Note the previous run's start time so we can tell when a new run shows up (avoids local clock skew).
    before = indexer_client.get_indexer_status(SEARCH_INDEXER_NAME).last_result
    previous_start = before.start_time if before else None
    try:
        indexer_client.run_indexer(SEARCH_INDEXER_NAME)
    except HttpResponseError as e:
        if e.status_code != 409:  # 409 = a run is already in progress; wait for it instead
            raise
        log.info("Step 4: indexer already running; waiting for the current run")
        previous_start = None  # accept the in-progress run's result once it completes
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_s)
        last = indexer_client.get_indexer_status(SEARCH_INDEXER_NAME).last_result
        # Right after the trigger, last_result can still describe the previous run.
        if not last or last.start_time == previous_start or last.status == "inProgress":
            continue
        log.info("Step 4: indexer finished: status=%s processed=%s failed=%s",
                 last.status, last.item_count, last.failed_item_count)
        for err in (last.errors or [])[:10]:
            log.error("  indexer error on %s: %s", err.key, err.error_message)
        return last.status == "success", last.error_message
    return False, f"indexer did not finish within {timeout_s}s"


# --- Step 5: write status back to Databricks -----------------------------------------------------
def update_status(doc, status, error=None):
    """Upsert one row per document so reruns skip finished work and retry failures."""
    run_sql(f"""
        MERGE INTO {STATUS_TABLE} t
        USING (SELECT :document_id AS document_id, :claim_id AS claim_id, :status AS status,
                      current_timestamp() AS processed_at, NULLIF(:error, '') AS error_message) s
        ON t.document_id = s.document_id
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *""",
            {"document_id": str(doc["document_id"]), "claim_id": str(doc["claim_id"]),
             "status": status, "error": (error or "")[:4000]})


def main():
    docs = get_pending_documents()
    log.info("Step 1: %d pending document(s) in %s", len(docs), DOCS_TABLE)
    succeeded, failed = [], []

    for doc in docs:  # each document gets its own try/except so one bad file doesn't stop the batch
        label = f"claim={doc['claim_id']} doc={doc['document_id']} ({doc['document_name']})"
        try:
            log.info("Step 1: downloading %s from %s", label, doc["volume_path"])
            data = download_from_databricks(doc["volume_path"])
            log.info("Step 2: analyzing with %s (%d bytes)", CU_ANALYZER_ID, len(data))
            analysis = with_retry(analyze_with_cu, data, doc["document_name"])
            log.info("Step 2: done: %d chars of Markdown, %d figure(s) described",
                     len(analysis["markdown"]), len(analysis["figure_descriptions"]))
            log.info("Step 3: uploading to container '%s'", ANALYZED_CONTAINER)
            upload_to_blob(doc, analysis)
            succeeded.append(doc)
        except Exception as e:
            log.exception("FAILED %s", label)
            failed.append((doc, f"{type(e).__name__}: {e}"))

    # Only index if something new landed in Blob.
    index_ok, index_err = True, None
    if succeeded:
        log.info("Step 4: running indexer '%s'", SEARCH_INDEXER_NAME)
        try:
            index_ok, index_err = run_search_indexer()
        except Exception as e:
            log.exception("Step 4: indexer call failed")
            index_ok, index_err = False, f"{type(e).__name__}: {e}"

    # Mark a document 'processed' only once it's searchable; otherwise the next run retries it.
    log.info("Step 5: writing status to %s", STATUS_TABLE)
    for doc in succeeded:
        update_status(doc, "processed" if index_ok else "failed",
                      None if index_ok else f"indexer: {index_err}")
    for doc, err in failed:
        update_status(doc, "failed", err)
    log.info("Done: %d analyzed, %d failed, indexer ok=%s", len(succeeded), len(failed), index_ok)


if __name__ == "__main__":
    main()
