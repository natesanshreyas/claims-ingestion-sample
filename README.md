# Claims document ingestion + Q&A agent: reference sample

Reference sample for the claims AI MVP. Claim documents from the **file share** (scanned PDFs, OCR documents,
emails, and reports from many agencies, with no consistent format) are classified into document groups and
extracted against a **two-layer schema**. They're analyzed with **Azure AI Content Understanding** (the default;
it also describes every photo and diagram) or, optionally, **Azure Document Intelligence**, then written to Blob
Storage and indexed by **Azure AI Search**. A **Foundry agent**
then answers open-ended claim questions ("what does the fire report say?") from that index, with citations.

**Databricks** supplies structured claims data and claim log notes. It is *not* a document source here. The pipeline
also writes its per-document status to a Databricks table.

This is sample code for the customer's engineering team: clear over clever, not production-hardened. See
[Production considerations](#production-considerations).

## Flow

```mermaid
flowchart LR
    subgraph Sources
        FS["File share (ADFS)<br/>~100 docs per claim"]
        DM["Documentum (on-prem)<br/>stub"]
    end
    FS --> SRC["sources/<br/>DOCUMENT_SOURCE"]
    DM -.-> SRC
    SRC --> CLS{"Classifier<br/>→ document group"}
    CLS --> SCH["schemas/<br/>envelope + group fields"]
    SCH --> AN{"ANALYZER_BACKEND"}
    AN -->|cu (default)| CU["Content Understanding<br/>classifier → group analyzer<br/>+ image descriptions"]
    AN -.->|docintel (alternative)| DI["Doc Intel prebuilt-layout<br/>+ chat-completion extraction"]
    DI --> BLOB[("Blob 'analyzed'<br/>{claim_id}/{document_id}.md / .json")]
    CU --> BLOB
    BLOB --> IDX["AI Search indexer<br/>chunking + embeddings"]
    DBX[("Databricks Delta<br/>claims data + log notes")] -.->|optional:<br/>log_notes_to_search.py| LN[("Blob 'lognotes'")]
    LN -.-> IDX
    IDX --> AG["Foundry agent<br/>Azure AI Search tool"]
    DBX -.->|optional, preview:<br/>Genie tool| AG
    AG --> U(["Claims user"])
    BLOB -. status .-> ST[("Databricks<br/>status table")]
```

## Why Content Understanding is the default

Claim files are full of photos and diagrams: damage photos, fire scene images, police sketches. Users need to be able
to find them ("are there photos of the roof damage?").

| | Content Understanding (default) | Document Intelligence (alternative) |
|---|---|---|
| Text, tables, layout as Markdown | ✅ | ✅ |
| **Image descriptions** | ✅ Every image in every document, written into the Markdown, so it's searchable | ❌ Not by Doc Intel itself. Returns `<figure>` plus any text OCR'd *inside* the image. This sample works around that for photo-only documents (crop + GPT-4.1 vision) but **not for photos inside other documents** |
| Classification | Built in: categories defined by description, no training | Separate chat-completion call |
| Extraction against the schema | Built in: one analyzer per group, built from `/schemas` | Separate chat-completion call |
| Calls per document | 1 | 2–3 (+1 per image in photo documents) |

Tested on the same synthetic claim files: both backends classified and extracted all 6 documents the same way. The
difference is images. A damage photo inside a fire report is described by CU and invisible on the Doc Intel path.
The Doc Intel backend stays in the repo for teams that already run it (`ANALYZER_BACKEND=docintel`).

## Schemas

Every document is described in **two layers**:

1. **Envelope**: the same fields for every document: `claim_id`, `document_id`, `source_path`, `document_group`,
   `document_type` (closed list with `Other`), `document_date`, `summary`, `key_entities`, `full_text_markdown`,
   `confidence`.
2. **Group fields**: extra fields that depend on the document's group:

| Group | Adds | Schema file |
|---|---|---|
| `correspondence_email` | sender, recipients, sent_date, thread_topic, action_requested, claim_activity_described | [`schemas/correspondence_email.schema.json`](schemas/correspondence_email.schema.json) |
| `investigation_report` (fire, police, adjuster) | incident_date, loss_location, cause_or_origin, findings, parties_involved, report_author_or_agency | [`schemas/investigation_report.schema.json`](schemas/investigation_report.schema.json) |
| `external_agency_document` | issuing_agency, document_purpose, key_determinations, amounts | [`schemas/external_agency_document.schema.json`](schemas/external_agency_document.schema.json) |
| `medical_legal_document` | party, dates_of_service_or_filing, diagnoses_or_rulings, outcome | [`schemas/medical_legal_document.schema.json`](schemas/medical_legal_document.schema.json) |
| `image_heavy_document` | figure_descriptions | [`schemas/image_heavy_document.schema.json`](schemas/image_heavy_document.schema.json) |
| `other` (no match) | nothing; envelope only | [`schemas/envelope.schema.json`](schemas/envelope.schema.json) |

> **The groups are proposed.** They'll be finalized in a live document-review session with the claims team.

**How the schemas are used in the flow:**
classifier (assigns the group) → group schema (envelope + group fields) → CU (or Doc Intel) extraction →
`{claim_id}/{document_id}.md` + `.json` in Blob → AI Search indexer → agent.
Both backends return the same JSON shape, so nothing downstream depends on which one ran.
Details, editing tips, and cost notes: [`schemas/README.md`](schemas/README.md).

## Repo layout

```
main.py                    pipeline: source → classify/extract → Blob → indexer → status
shared.py                  logging, retry, Databricks SQL, indexer run-and-wait
log_notes_to_search.py     OPTIONAL: Databricks log notes → Blob 'lognotes' → its own indexer
sources/                   DocumentSource interface: LocalOrBlobSource (file share stand-in), Documentum stub
analyzers/
  __init__.py              schema loading, output contract, email → Markdown, backend switch
  cu_backend.py            DEFAULT: Content Understanding classifier → per-group analyzers (+ image descriptions)
  create_cu_analyzers.py   builds the CU analyzers from /schemas (run once, and after schema changes)
  docintel_backend.py      alternative: Doc Intel layout + chat-completion classifier/extractor
schemas/                   envelope + one JSON Schema per document group (+ README)
agent/
  create_agent.py          Foundry agent with the AI Search index as its knowledge source
  ask_agent.py             CLI: ask questions, see answers + citations, optional claim filter
```

## What's real vs. stubbed

| Piece | Status |
|---|---|
| Local-folder / Blob document source | Implemented |
| Documentum source | **Stub** (`# TODO` + requirements in `sources/__init__.py`) |
| CU backend (default) + `create_cu_analyzers.py` | Implemented; **run live** against a Foundry resource on synthetic claim files, including a fire report with an embedded scene photo |
| Doc Intel backend (alternative) | Implemented; **run live** on the same files (both backends agreed on all 6 base test docs) |
| Blob upload, Databricks status table, indexer trigger | Implemented; not run live in this round (no Databricks workspace or indexer available). Same code as the earlier version, which was tested against mocks |
| `agent/create_agent.py`, `ask_agent.py` | Implemented; agent creation, request routing, the per-claim filter, and the "not found" answer **run live** against an *empty* test index. Answers over real indexed content not yet tested |
| Search data source / index / skillset / indexer | **Not created by this repo**. Configure as described in `main.py` Step 4 |
| Log notes → search | Implemented, optional; not run live |
| Databricks Genie tool on the agent | Optional, **preview**, documented in `create_agent.py` |

## Prerequisites

- Python 3.10+.
- **Foundry (AI Services) resource + project** with a chat model deployment (e.g. `gpt-4.1`) for the agent.
- **Content Understanding default model deployments already configured** on that resource (chat completion, e.g.
  `gpt-4.1` / `gpt-4.1-mini`, + `text-embedding-3-large`; set once in the Foundry portal or with
  `PATCH /contentunderstanding/defaults`). This sample doesn't set them up. Then build the analyzers once:
  `python -m analyzers.create_cu_analyzers`.
- Only for `ANALYZER_BACKEND=docintel`: Document Intelligence on the same resource; the chat model must accept images.
- **Storage account** with `analyzed` (and, for `DOCUMENT_SOURCE=blob`, `raw`; for log notes, `lognotes`) containers.
- **AI Search**: data source → `analyzed`, an indexer with `parsingMode: markdown` and
  `indexedFileNameExtensions: .md`, a skillset (Text Split + Azure OpenAI Embedding, with index projections), and an
  index with filterable `claim_id`, `document_id`, `document_group`, `document_type`, plus vector fields and a
  semantic configuration. Add a **project connection** from Foundry to the search service.
- **Databricks**: SQL warehouse plus the status table:
  ```sql
  CREATE TABLE claims.ai.document_ingestion_status (
    document_id STRING, claim_id STRING, source_path STRING, document_group STRING,
    status STRING,             -- 'processed' | 'failed'
    processed_at TIMESTAMP, error_message STRING
  );
  ```
- File share layout for the source: `<root>/<claim_id>/.../<file>`. Supported types: PDF, images, Office files,
  HTML, `.eml`. Convert `.msg` to `.eml` (or PDF) first.

## RBAC

| Identity | Role | Scope | Why |
|---|---|---|---|
| Pipeline (app / managed identity) | **Cognitive Services User** | Foundry / AI Services resource | CU analysis and analyzer creation (and Doc Intel, if used) |
| Pipeline (Doc Intel backend only) | **Foundry User** (formerly Azure AI User) | Foundry project | Chat completions through the project endpoint |
| Pipeline | **Storage Blob Data Contributor** | Storage account | Read `raw`, write `analyzed` / `lognotes` |
| Pipeline | **Search Service Contributor** | AI Search | Run indexers and read their status |
| Pipeline | Databricks: `CAN USE` warehouse; `SELECT, MODIFY` status table; `SELECT` log-notes table | Databricks / Unity Catalog | Status and log notes |
| Whoever runs `create_agent.py` | **Foundry User** | Foundry project | Create agents |
| Foundry project managed identity | **Search Index Data Reader** | AI Search | Agent retrieval (if the connection uses Entra ID instead of a key) |
| Search service managed identity | **Storage Blob Data Reader** | Storage account | Indexer data source |
| Search service managed identity | **Cognitive Services OpenAI User** | Resource hosting the embedding model | Skillset embeddings |
| App users of `ask_agent.py` | **Foundry User** | Foundry project | Call the agent |

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in values
az login                      # local dev; in Azure, DefaultAzureCredential uses the managed identity

python -m analyzers.create_cu_analyzers    # once, and again after any schema change
python main.py                             # ingest: share → analyze → Blob → index → status
python log_notes_to_search.py              # optional: log notes → search

python agent/create_agent.py               # once (re-run to publish a new version)
python agent/ask_agent.py --claim CLM-1001 # "What does the fire report say?"
```

Re-running `main.py` is safe. Processed documents are skipped, failed ones are retried, and blobs are overwritten.

## Notes on SDKs and APIs

These were checked against the installed packages and Microsoft Learn (October 2026), and exercised live where noted
above.

- `azure-ai-documentintelligence` 1.0.2: `begin_analyze_document("prebuilt-layout", AnalyzeDocumentRequest(bytes_source=...), output_content_format="markdown", output=["figures"])`; `get_analyze_result_figure(model_id, result_id, figure_id)` returns the PNG crop.
- `azure-ai-contentunderstanding` 1.2.0b3: **defaults to a preview API version**, so the code pins `2025-11-01` (GA). Classifier routing uses `ContentCategoryDefinition(analyzer_id=...)`. The routed content item has `category` and `fields`. No classification confidence is returned, so `confidence` is `null` on the CU path.
- `azure-ai-projects` 2.9.0: `agents.create_version(..., PromptAgentDefinition(...))`, `AzureAISearchTool` (GA), `structured_inputs` for the per-request `claim_filter`, `get_openai_client(agent_name=...)` → `responses.create(...)`.
- **Foundry IQ** (knowledge base via `MCPTool`): the agent connection is **preview** (`RemoteTool` connection, `2026-08-01-preview`). Not used here; see the note in `create_agent.py`.
- **Databricks in Foundry**: the Azure Databricks connection and the Genie tool are **preview** (not GA). The older Genie MCP endpoint sunsets on October 31, 2026. The Genie option is documented, not wired into the core flow.

## Production considerations

- **Secrets**: managed identity end to end. The only possible secrets are a Databricks OAuth client secret and a
  Documentum service account. Keep both in Key Vault, never in `.env` in source control. Prefer Entra ID over a key
  for the Foundry→Search connection.
- **Concurrency**: documents are processed one at a time. At ~100 documents per claim, use a bounded worker pool or
  a queue (Service Bus / Durable Functions), and size it to CU (or Doc Intel) and model TPM quotas. Don't run two pipelines
  over the same share without a lease or claim column.
- **Large files (1,000+ pages per claim)**: CU (and Doc Intel) have per-file page and size limits. Split very large
  PDFs, or use `content_range` (CU) / `pages` (Doc Intel). On the Doc Intel path, extraction sends at most
  `MAX_EXTRACTION_CHARS` to the model; long reports need chunked extraction instead of truncation. Stream from Blob rather than holding
  whole files in memory.
- **Idempotency**: `document_id` is a hash of the file's path on the share, blobs are overwritten, and status is a
  `MERGE`. A moved or renamed file is treated as a new document; handle deletes with a soft-delete flag plus Search
  deletion detection. Re-run `create_cu_analyzers.py` after schema changes, and re-process affected groups.
- **PII / medical data**: medical and legal documents contain PHI. Restrict the `analyzed` container and the index
  (separate index or document-level access control if not all users may see medical content). Avoid logging
  extracted fields, set retention on `.raw.json` debug blobs, and review the data-residency settings on the model
  deployments (prefer regional / DataZone over Global deployments if required).
- **Network isolation**: private endpoints for Storage, Search, Foundry, and Databricks; disable public access;
  trusted-service access or shared private links for Search → Storage and Search → OpenAI; on-prem connectivity
  (ExpressRoute / VPN) for the file share and Documentum.
- **Monitoring**: send logs and traces to Application Insights (`azure-monitor-opentelemetry`; Foundry tracing for the
  agent). Alert on failed documents, indexer `transientFailure`, and classifier `other` rate. Track cost per document,
  and keep a small labeled set to re-check classification and extraction accuracy after every schema or model change.
