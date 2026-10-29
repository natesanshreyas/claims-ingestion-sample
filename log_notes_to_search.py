"""OPTIONAL: make Databricks claim log notes searchable next to the documents.

Reads the log-notes Delta table, joins each claim's long-text fields into one Markdown file, writes it to the
"lognotes" container as {claim_id}.md (with claim_id metadata), and runs that container's own indexer.

Why a separate container and indexer: log notes are structured rows, not documents. They change on their own
schedule, and keeping them apart lets you rebuild either index without touching the other. Point both indexers
at the same index (or give the agent both indexes) so one question can draw on documents and notes.
"""
import os
import re

from azure.storage.blob import BlobServiceClient, ContentSettings

from shared import credential, log, run_search_indexer, run_sql, with_retry

LOGNOTES_TABLE = os.environ["DATABRICKS_LOGNOTES_TABLE"]            # e.g. claims.silver.claim_log_notes
TEXT_COLUMNS = [c.strip() for c in os.environ["LOGNOTES_TEXT_COLUMNS"].split(",")]  # the ~8 long-text fields
DATE_COLUMN = os.getenv("LOGNOTES_DATE_COLUMN", "note_date")        # for ordering notes within a claim
CLAIM_IDS = [c.strip() for c in os.getenv("LOGNOTES_CLAIM_IDS", "").split(",") if c.strip()]  # empty = all claims
CONTAINER = os.getenv("LOGNOTES_CONTAINER", "lognotes")
INDEXER = os.environ["LOGNOTES_INDEXER_NAME"]
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_notes():
    # Column names can't be bound as parameters, so validate them before building the SQL.
    for col in TEXT_COLUMNS + [DATE_COLUMN]:
        if not IDENT.match(col):
            raise ValueError(f"Invalid column name: {col!r}")
    where, params = "", {}
    if CLAIM_IDS:  # MVP: restrict to the ~10 pilot claims
        where = "WHERE claim_id IN (" + ", ".join(f":c{i}" for i in range(len(CLAIM_IDS))) + ")"
        params = {f"c{i}": c for i, c in enumerate(CLAIM_IDS)}
    return run_sql(f"SELECT claim_id, {DATE_COLUMN}, {', '.join(TEXT_COLUMNS)} FROM {LOGNOTES_TABLE} {where} "
                   f"ORDER BY claim_id, {DATE_COLUMN}", params)


def to_markdown(claim_id, rows):
    parts = [f"# Claim log notes: {claim_id}"]
    for row in rows:
        parts.append(f"\n## Note dated {row.get(DATE_COLUMN) or 'unknown'}")
        parts += [f"\n### {col}\n\n{row[col].strip()}" for col in TEXT_COLUMNS if row.get(col)]
    return "\n".join(parts)


def main():
    rows = read_notes()
    by_claim = {}
    for r in rows:
        by_claim.setdefault(r["claim_id"], []).append(r)
    log.info("Read %d note row(s) for %d claim(s) from %s", len(rows), len(by_claim), LOGNOTES_TABLE)

    container = BlobServiceClient(os.environ["STORAGE_ACCOUNT_URL"], credential=credential).get_container_client(
        CONTAINER)
    for claim_id, claim_rows in by_claim.items():
        # One file per claim, overwritten each run: notes get appended over time, so rebuilding is simplest.
        with_retry(container.upload_blob, f"{claim_id}.md", to_markdown(claim_id, claim_rows).encode("utf-8"),
                   overwrite=True, metadata={"claim_id": claim_id, "document_group": "log_notes"},
                   content_settings=ContentSettings(content_type="text/markdown"))
        log.info("  wrote %s/%s.md (%d notes)", CONTAINER, claim_id, len(claim_rows))

    if by_claim:
        ok, err = run_search_indexer(INDEXER)
        log.info("Log notes indexer ok=%s%s", ok, f" ({err})" if err else "")


if __name__ == "__main__":
    main()
