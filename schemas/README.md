# Document schemas

> **Status: proposed.** The groups and fields below are a starting point built from what we know about the
> claim files. They'll be finalized in a live document-review session with the claims team, going through real
> claim files and agreeing what each group should capture.

## Two layers

1. **Envelope** ([`envelope.schema.json`](envelope.schema.json)): fields every document gets, whatever its group.
   - Filled in by code (`x-source: pipeline`): `claim_id`, `document_id`, `source_path`, `document_group`,
     `full_text_markdown`, `confidence`.
   - Extracted by the model: `document_type` (closed list with `Other`), `document_date`, `summary`, `key_entities`.
2. **Group fields**: one file per group, each declaring `allOf: [envelope]` plus its own fields.

| Group | Adds | File |
|---|---|---|
| `correspondence_email` | sender, recipients, sent_date, thread_topic, action_requested, claim_activity_described | [`correspondence_email.schema.json`](correspondence_email.schema.json) |
| `investigation_report` (fire, police, adjuster) | incident_date, loss_location, cause_or_origin, findings, parties_involved, report_author_or_agency | [`investigation_report.schema.json`](investigation_report.schema.json) |
| `external_agency_document` | issuing_agency, document_purpose, key_determinations, amounts | [`external_agency_document.schema.json`](external_agency_document.schema.json) |
| `medical_legal_document` | party, dates_of_service_or_filing, diagnoses_or_rulings, outcome | [`medical_legal_document.schema.json`](medical_legal_document.schema.json) |
| `image_heavy_document` | figure_descriptions (from figure analysis, `x-source: figures`) | [`image_heavy_document.schema.json`](image_heavy_document.schema.json) |
| `other` | nothing; envelope fields only | (none) |

## How they're used

- **Classification**: each group's top-level `description` is its classifier definition, for both backends. Write
  descriptions as "what belongs here" with concrete examples. That wording is the main lever on accuracy.
- **Doc Intel backend**: the envelope and group fields are merged into one JSON Schema and passed to the chat model as
  `response_format` (`analyzers/__init__.py: extraction_schema`).
- **CU backend**: `python -m analyzers.create_cu_analyzers` turns the same merged schema into one CU analyzer per
  group, plus a classifier analyzer that routes to them. Re-run it after any schema change.
- `x-source` is a custom keyword this sample uses to decide who fills a field; JSON Schema validators ignore it.

## Editing tips

- Keep `other` narrow. In testing, a vague catch-all description pulled photo-only documents into `other`.
- Prefer a few well-described fields to many vague ones. Every field is extra work for the model on every document.
- `format: date` makes both backends return `YYYY-MM-DD`.
- Arrays of objects (`parties_involved`, `amounts`) work in both backends, but they're the most expensive and least
  consistent fields. Use them only where the structure matters.

## Cost

Schema size and complexity drive cost. Each field adds output tokens on every document, in every group it
belongs to. The Doc Intel path also adds two chat calls per document (classify, then extract) plus one vision call
per figure for image-heavy documents (capped by `MAX_FIGURES_PER_DOCUMENT`). The CU path bills content extraction
plus generative tokens per field. Before scaling past the MVP (~10 claims × ~100 documents), measure cost per
document on a representative sample and cut fields nobody uses.
