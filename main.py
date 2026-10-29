"""Reference sample: claims document ingestion pipeline.

File share (DOCUMENT_SOURCE) -> classify + extract (ANALYZER_BACKEND = Doc Intel or Content Understanding)
-> Blob "analyzed" container -> Azure AI Search indexer -> status written back to Databricks.

Not production code. See README "Production considerations".
"""
import json
import os
from urllib.parse import quote

from azure.storage.blob import BlobServiceClient, ContentSettings

from analyzers import get_backend
from shared import credential, log, run_search_indexer, run_sql, with_retry
from sources import get_document_source

# --- Configuration (all from environment; see .env.example) ---------------------------------
STATUS_TABLE = os.environ["DATABRICKS_STATUS_TABLE"]    # e.g. claims.ai.document_ingestion_status
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))
STORAGE_ACCOUNT_URL = os.environ["STORAGE_ACCOUNT_URL"]
ANALYZED_CONTAINER = os.getenv("ANALYZED_CONTAINER", "analyzed")
SEARCH_INDEXER_NAME = os.environ["SEARCH_INDEXER_NAME"]

blob_service = BlobServiceClient(STORAGE_ACCOUNT_URL, credential=credential)


# --- Step 1: find work on the file share ------------------------------------------------------
def get_pending_documents(source):
    """Everything on the share that has no 'processed' row in the status table.
    'failed' documents are retried on the next run."""
    done = {r["document_id"] for r in run_sql(
        f"SELECT document_id FROM {STATUS_TABLE} WHERE status = 'processed'")}
    return [d for d in source.list_documents() if d["document_id"] not in done][:BATCH_SIZE]


# --- Step 2: classify + extract (Doc Intel or CU, chosen by ANALYZER_BACKEND) --------------------
def analyze_document(backend, doc, data):
    """Both backends return the same contract (see analyzers/__init__.py), so the rest of the pipeline is shared."""
    return backend.analyze(doc, data)


# --- Step 3: Blob Storage ----------------------------------------------------------------------
def to_markdown(record):
    """What Search indexes: a header with the extracted fields, then the full text. Putting the fields in the text
    makes them searchable and gives the agent a summary at the top of the first chunk."""
    skip = {"full_text_markdown", "summary"}
    lines = [f"- **{k}:** {json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v}"
             for k, v in record.items() if k not in skip and v not in (None, [], "")]
    return (f"## Summary\n\n{record.get('summary') or ''}\n\n## Extracted fields\n\n" + "\n".join(lines)
            + f"\n\n---\n\n{record['full_text_markdown']}")


def upload_to_blob(record, raw):
    """Write {claim_id}/{document_id}.md (what Search indexes), .json (the output contract), and .raw.json
    (the analyzer's raw result, for debugging). The blob metadata becomes filterable fields in the search index."""
    container = blob_service.get_container_client(ANALYZED_CONTAINER)
    prefix = f"{record['claim_id']}/{record['document_id']}"
    # Blob metadata must be ASCII, so the source path is URL-quoted (file share paths often contain spaces).
    metadata = {"claim_id": record["claim_id"], "document_id": record["document_id"],
                "document_group": record["document_group"], "document_type": record.get("document_type") or "",
                "source_path": quote(record["source_path"], safe="/:\\")}
    files = [(".md", to_markdown(record), "text/markdown"),
             (".json", json.dumps(record, indent=2, ensure_ascii=False), "application/json"),
             (".raw.json", json.dumps(raw, indent=2, default=str), "application/json")]
    # overwrite=True keeps reruns idempotent: reprocessing a document replaces its blobs.
    for suffix, body, content_type in files:
        with_retry(container.upload_blob, prefix + suffix, body.encode("utf-8"), overwrite=True,
                   metadata=metadata, content_settings=ContentSettings(content_type=content_type))


# --- Step 4: Azure AI Search -----------------------------------------------------------------
# Assumed to exist already (this script doesn't create them):
#   Data source: blob container ANALYZED_CONTAINER, accessed with the Search service's managed identity.
#   Indexer:     parsingMode = "markdown" (markdownParsingSubmode "oneToOne" when a skillset does the chunking),
#                indexedFileNameExtensions = ".md" so the .json / .raw.json files are skipped,
#                field mappings for claim_id / document_id / document_group / document_type / source_path
#                (blob metadata, mapped by name). Make claim_id and document_group filterable.
#   Skillset:    Text Split skill (chunking) + Azure OpenAI Embedding skill, with index projections
#                writing one search document per chunk into a vector-enabled index.
# Retry and polling for the indexer live in shared.run_search_indexer.


# --- Step 5: write status back to Databricks -----------------------------------------------------
def update_status(doc, status, group=None, error=None):
    """Upsert one row per document so reruns skip finished work and retry failures."""
    run_sql(f"""
        MERGE INTO {STATUS_TABLE} t
        USING (SELECT :document_id AS document_id, :claim_id AS claim_id, :source_path AS source_path,
                      NULLIF(:document_group, '') AS document_group, :status AS status,
                      current_timestamp() AS processed_at, NULLIF(:error, '') AS error_message) s
        ON t.document_id = s.document_id
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *""",
            {"document_id": doc["document_id"], "claim_id": doc["claim_id"], "source_path": doc["source_path"],
             "document_group": group or "", "status": status, "error": (error or "")[:4000]})


def main():
    source, backend = get_document_source(), get_backend()
    docs = get_pending_documents(source)
    log.info("Step 1: %d pending document(s) from %s (backend: %s)",
             len(docs), type(source).__name__, backend.__name__.rsplit(".", 1)[-1])
    succeeded, failed = [], []

    for doc in docs:  # each document gets its own try/except so one bad file doesn't stop the batch
        label = f"claim={doc['claim_id']} doc={doc['document_id']} ({doc['document_name']})"
        try:
            log.info("Step 1: reading %s", label)
            data = with_retry(source.read, doc)
            log.info("Step 2: classifying + extracting (%d bytes)", len(data))
            record, raw = with_retry(analyze_document, backend, doc, data)
            log.info("Step 2: done: group=%s type=%s, %d chars of Markdown",
                     record["document_group"], record.get("document_type"), len(record["full_text_markdown"]))
            log.info("Step 3: uploading to container '%s'", ANALYZED_CONTAINER)
            upload_to_blob(record, raw)
            succeeded.append((doc, record["document_group"]))
        except Exception as e:  # noqa: BLE001 - by design: one bad document must not stop the batch
            log.exception("FAILED %s", label)
            failed.append((doc, f"{type(e).__name__}: {e}"))

    # Only index if something new landed in Blob.
    index_ok, index_err = True, None
    if succeeded:
        log.info("Step 4: running indexer '%s'", SEARCH_INDEXER_NAME)
        try:
            index_ok, index_err = run_search_indexer(SEARCH_INDEXER_NAME)
        except Exception as e:  # noqa: BLE001 - record indexer failure in the status table instead of crashing
            log.exception("Step 4: indexer call failed")
            index_ok, index_err = False, f"{type(e).__name__}: {e}"

    # Mark a document 'processed' only once it's searchable; otherwise the next run retries it.
    log.info("Step 5: writing status to %s", STATUS_TABLE)
    for doc, group in succeeded:
        update_status(doc, "processed" if index_ok else "failed", group,
                      None if index_ok else f"indexer: {index_err}")
    for doc, err in failed:
        update_status(doc, "failed", error=err)
    log.info("Done: %d analyzed, %d failed, indexer ok=%s", len(succeeded), len(failed), index_ok)


if __name__ == "__main__":
    main()
