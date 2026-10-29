"""Build the Content Understanding analyzers from /schemas (needed only for ANALYZER_BACKEND=cu).

Creates:
  {prefix}{group}      one extraction analyzer per group (+ {prefix}other with envelope fields only)
  {prefix}classifier   classifies each document and routes it to the matching group analyzer in the same call

Run again after editing a schema; analyzers are replaced. Usage: python -m analyzers.create_cu_analyzers
"""
import logging
import os

from azure.ai.contentunderstanding import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import (
    ContentAnalyzer,
    ContentAnalyzerConfig,
    ContentCategoryDefinition,
    ContentFieldDefinition,
    ContentFieldSchema,
)
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from analyzers import GROUPS, OTHER, extraction_schema, group_description

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("azure").setLevel(logging.WARNING)
log = logging.getLogger("create-cu-analyzers")

PREFIX = os.getenv("CU_ANALYZER_PREFIX", "claims_")
COMPLETION_MODEL = os.getenv("CU_COMPLETION_MODEL", "gpt-4.1")  # model *name*; CU maps it via its defaults


def to_cu_field(prop, top_level=True):
    """Map one JSON Schema property onto a CU field definition (string/number/integer/boolean/date/array/object)."""
    jtype = prop.get("type", "string")
    cu_type = "date" if prop.get("format") == "date" else jtype
    field = ContentFieldDefinition(type=cu_type, description=prop.get("description"))
    if top_level:
        field.method = "generate"  # generative extraction works across all document layouts
    if "enum" in prop:
        field.enum = prop["enum"]
    if jtype == "array":
        field.item_definition = to_cu_field(prop.get("items", {}), top_level=False)
    if jtype == "object":
        field.properties = {k: to_cu_field(v, top_level=False) for k, v in prop.get("properties", {}).items()}
    return field


def group_analyzer(group):
    return ContentAnalyzer(
        base_analyzer_id="prebuilt-document",
        description=f"Claims extraction: {group}",
        models={"completion": COMPLETION_MODEL},
        # Figure descriptions go into the Markdown for every group, so photos inside any report stay searchable.
        config=ContentAnalyzerConfig(return_details=True, enable_layout=True, enable_ocr=True,
                                     enable_figure_description=True),
        field_schema=ContentFieldSchema(
            name=group, fields={k: to_cu_field(v) for k, v in extraction_schema(group)["properties"].items()}))


def classifier_analyzer():
    return ContentAnalyzer(
        base_analyzer_id="prebuilt-document",
        description="Claims document classifier; routes to the per-group analyzers",
        models={"completion": COMPLETION_MODEL},
        config=ContentAnalyzerConfig(
            return_details=True,
            # Figure description is left off here: CU classifies from text either way (tested), so it would only
            # add cost. Photo-only documents are handled by the fallback in cu_backend.py.
            # Each file on the share is one document. Splitting is off because it breaks photo pages away
            # from the report they belong to. Turn it on only for scanned packets that bundle several documents.
            enable_segment=False,
            content_categories={g: ContentCategoryDefinition(description=group_description(g),
                                                             analyzer_id=f"{PREFIX}{g}") for g in GROUPS + [OTHER]}))


def main():
    client = ContentUnderstandingClient(os.environ["CU_ENDPOINT"], DefaultAzureCredential(),
                                        api_version=os.getenv("CU_API_VERSION", "2025-11-01"))
    # Group analyzers first: the classifier references them by ID.
    for group in GROUPS + [OTHER]:
        log.info("Creating %s%s", PREFIX, group)
        client.begin_create_analyzer(f"{PREFIX}{group}", group_analyzer(group), allow_replace=True).result()
    log.info("Creating %sclassifier", PREFIX)
    client.begin_create_analyzer(f"{PREFIX}classifier", classifier_analyzer(), allow_replace=True).result()
    log.info("Done")


if __name__ == "__main__":
    main()
