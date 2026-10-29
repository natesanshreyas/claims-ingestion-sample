"""Helpers shared by main.py and log_notes_to_search.py: logging, retry, Databricks SQL, and Search indexer runs."""
import logging
import os
import time
from functools import lru_cache

from azure.core.exceptions import HttpResponseError, ServiceRequestError
from azure.identity import DefaultAzureCredential
from azure.search.documents.indexes import SearchIndexerClient
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# The Azure SDKs log every HTTP request at INFO, which buries the pipeline's own messages.
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)  # the OpenAI client logs every request the same way
log = logging.getLogger("claims-pipeline")

# One credential for every Azure service: managed identity in Azure, `az login` on a laptop.
credential = DefaultAzureCredential()


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


@lru_cache
def _dbx():
    # Imported here so scripts that don't touch Databricks don't need it configured.
    from databricks.sdk import WorkspaceClient
    # Reads DATABRICKS_HOST plus PAT, OAuth M2M, or Azure MSI settings from the environment.
    return WorkspaceClient()


def run_sql(statement, params=None):
    """Run SQL on a Databricks SQL warehouse and return rows as dicts.
    Table names come from config (identifiers can't be bound); values always use :named parameters."""
    from databricks.sdk.service.sql import StatementParameterListItem, StatementState
    dbx = _dbx()
    resp = dbx.statement_execution.execute_statement(
        statement=statement, warehouse_id=os.environ["DATABRICKS_WAREHOUSE_ID"], wait_timeout="30s",
        parameters=[StatementParameterListItem(name=k, value=v) for k, v in (params or {}).items()])
    while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        resp = dbx.statement_execution.get_statement(resp.statement_id)
    if resp.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"SQL failed: {resp.status.error}")
    if not resp.result or not resp.result.data_array:
        return []
    # Inline results are capped (about 25 MB, first chunk only). Fine for an MVP of ~10 claims; for bigger
    # reads, page through next_chunk_index or use disposition=EXTERNAL_LINKS.
    cols = [c.name for c in resp.manifest.schema.columns]
    return [dict(zip(cols, row, strict=True)) for row in resp.result.data_array]


def run_search_indexer(indexer_name, timeout_s=900, poll_s=15):
    """Run an existing indexer and wait for the result. Run it once per batch, not once per document:
    an indexer can only run one execution at a time, and on-demand runs are rate-limited."""
    client = SearchIndexerClient(os.environ["SEARCH_ENDPOINT"], credential)
    # Note the previous run's start time so we can tell when a new run shows up (avoids local clock skew).
    before = client.get_indexer_status(indexer_name).last_result
    previous_start = before.start_time if before else None
    try:
        client.run_indexer(indexer_name)
    except HttpResponseError as e:
        if e.status_code != 409:  # 409 = a run is already in progress; wait for it instead
            raise
        log.info("  indexer '%s' already running; waiting for the current run", indexer_name)
        previous_start = None  # accept the in-progress run's result once it completes
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_s)
        last = client.get_indexer_status(indexer_name).last_result
        # Right after the trigger, last_result can still describe the previous run.
        if not last or last.start_time == previous_start or last.status == "inProgress":
            continue
        log.info("  indexer '%s' finished: status=%s processed=%s failed=%s",
                 indexer_name, last.status, last.item_count, last.failed_item_count)
        for err in (last.errors or [])[:10]:
            log.error("  indexer error on %s: %s", err.key, err.error_message)
        return last.status == "success", last.error_message
    return False, f"indexer did not finish within {timeout_s}s"
