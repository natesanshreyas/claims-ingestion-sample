# Claims document ingestion — reference sample

An end-to-end sample that makes insurance claim documents searchable, including the contents of photos and diagrams inside them.

```
Databricks UC table + Volume ──► Content Understanding ──► Blob "analyzed" ──► AI Search indexer
        ▲                        (prebuilt-documentSearch)   {claim_id}/{document_id}.md        │
        └──────────────── status table (processed / failed) ◄───────────────────────────────────┘
```

| Step | Function | What it does |
|---|---|---|
| 1 | `get_pending_documents`, `download_from_databricks` | Selects documents with no `processed` row in the status table, then reads each file from its UC Volume. |
| 2 | `analyze_with_cu` | Sends the bytes **inline** (`AnalysisInput(data=...)`) to `prebuilt-documentSearch`. Returns Markdown with figure descriptions embedded, a `Summary` field, and the raw JSON. No raw/staging container is needed. |
| 3 | `upload_to_blob` | Writes `{claim_id}/{document_id}.md` (summary followed by Markdown) and `{claim_id}/{document_id}.cu.json`, both carrying `claim_id`, `document_id`, and `source_path` metadata. |
| 4 | `run_search_indexer` | Runs the existing indexer **once per batch** and polls until it succeeds or fails. |
| 5 | `update_status` | `MERGE`s one status row per document. A document is marked `processed` only after the indexer succeeds. |

## Prerequisites

- Python 3.10+
- **A Foundry / AI Services resource with Content Understanding default model deployments already configured.** `prebuilt-documentSearch` uses a chat completion model (`gpt-4.1-mini` / `gpt-4.1`) and `text-embedding-3-large`. Set these once in the Content Understanding defaults (Foundry portal, or `PATCH /contentunderstanding/defaults`). This sample doesn't set them up.
- A Databricks SQL warehouse, plus Unity Catalog tables like these:
  ```sql
  CREATE TABLE claims.bronze.claim_documents (
    claim_id STRING, document_id STRING, document_name STRING,
    volume_path STRING  -- e.g. /Volumes/claims/bronze/raw_docs/CLM-1/fnol.pdf
  );
  CREATE TABLE claims.bronze.claim_document_status (
    document_id STRING, claim_id STRING, status STRING,  -- 'processed' | 'failed'
    processed_at TIMESTAMP, error_message STRING
  );
  ```
- A storage account with an `analyzed` container.
- An AI Search data source, index, and indexer that already exist and point at `analyzed`:
  - Indexer `parsingMode: "markdown"`, plus `indexedFileNameExtensions: ".md"` so the `.cu.json` debug files aren't indexed.
  - A skillset with a Text Split skill and an Azure OpenAI Embedding skill, using index projections (one search document per chunk).
  - Index fields `claim_id`, `document_id`, and `source_path`. Blob metadata is mapped to fields by name.

## RBAC

| Identity running the script | Role | Scope |
|---|---|---|
| App / managed identity | **Cognitive Services User** | Foundry / AI Services resource (CU) |
| App / managed identity | **Storage Blob Data Contributor** | Storage account (or the `analyzed` container) |
| App / managed identity | **Search Service Contributor** | AI Search service (needed to run indexers and read their status) |
| App / managed identity | Databricks: `CAN USE` on the warehouse; `SELECT` on the docs table; `SELECT, MODIFY` on the status table; `READ VOLUME` on the Volume | Databricks workspace / Unity Catalog |
| Search service managed identity | **Storage Blob Data Reader** | Storage account (for the indexer's data source) |
| Search service managed identity | **Cognitive Services OpenAI User** | Resource hosting the embedding model (for the skillset) |

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in values
az login                  # local dev; in Azure, DefaultAzureCredential uses the managed identity
python main.py
```

Running it again is safe. Documents already marked `processed` are skipped, `failed` ones are retried, and blobs are overwritten.

## Notes on the SDKs (checked against installed packages)

- `azure-ai-contentunderstanding` 1.2.0b3: **the client defaults to `api_version="2026-06-01-preview"`**, so `main.py` pins `2025-11-01` (GA). `begin_analyze(analyzer_id, inputs=[AnalysisInput(data=bytes, mime_type=...)])` accepts bytes inline (sent base64-encoded). A URL/SAS input is only needed if you'd rather not send bytes inline. Tested live: `result.contents[0].markdown`, `.fields["Summary"].value`, and `.figures[i].description`.
- `azure-search-documents` 11.6.0 (GA): `run_indexer(name)` and `get_indexer_status(name).last_result` (`status`, `start_time`, `errors`, `item_count`, `failed_item_count`).
- `databricks-sdk`: `statement_execution.execute_statement(...)` and `files.download(path).contents`. One auth configuration covers both SQL and Volumes (PAT, OAuth M2M, or Azure MSI), so `databricks-sql-connector` isn't used.

## Production considerations

- **Secrets:** use managed identity everywhere. If a Databricks OAuth secret or PAT is unavoidable, load it from Key Vault. Never put it in `.env` in source control.
- **Concurrency:** documents are processed one at a time. Use a bounded thread pool, or fan out over a queue (Service Bus or Durable Functions), and keep within CU and Search throttling limits. Don't run two copies of this script against the same table without a claim/lease column.
- **Large files:** the whole file is held in memory and sent inline. For large PDFs, stage them in Blob and pass a SAS or managed-identity URL, and check CU's file size and page limits. Consider `content_range` to process page ranges.
- **Idempotency:** writes are overwrite-based and status is a `MERGE`. Still, a crash between Step 3 and Step 5 leaves blobs that aren't marked processed (they're reprocessed next run, which is safe). Deleted or replaced source documents also need a delete path in Blob and in the index (soft-delete column or deletion detection policy).
- **Monitoring:** send logs to Application Insights (azure-monitor-opentelemetry), alert on the failed count and indexer `transientFailure`, and set a max-retry count so a permanently bad file doesn't retry forever.
- **Indexer timing:** this sample waits up to 15 minutes. With large batches, run the indexer on a schedule and reconcile status separately instead of waiting.
- **Blob metadata limits:** values must be ASCII and total ≤ 8 KB per blob. URL-encode non-ASCII Volume paths.
- **Cost:** `prebuilt-documentSearch` is billed for content extraction plus generative model tokens. Measure cost per page on a representative sample before scaling.
