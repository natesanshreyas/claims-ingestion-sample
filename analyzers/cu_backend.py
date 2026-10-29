"""ANALYZER_BACKEND=cu (default): Content Understanding classifies the document and extracts the group schema in
one call.

Why it's the default: CU describes every image in every document (the description is written into the Markdown),
so photos and diagrams become searchable. Doc Intel only reads text and layout.

The {prefix}classifier analyzer (built by create_cu_analyzers.py) labels the document with a group and routes it
to that group's analyzer. The result has one content item for the classifier plus one per routed segment, and
the routed item holds the extracted fields. Figure descriptions come from CU directly; no extra model call.
"""
import os
from functools import lru_cache

from azure.ai.contentunderstanding import ContentUnderstandingClient
from azure.ai.contentunderstanding.models import AnalysisInput
from azure.identity import DefaultAzureCredential

from analyzers import (
    GROUPS,
    OTHER,
    build_record,
    email_to_markdown,
    text_outside_figures,
)

PREFIX = os.getenv("CU_ANALYZER_PREFIX", "claims_")
IMAGE_HEAVY_MAX_TEXT_CHARS = int(os.getenv("IMAGE_HEAVY_MAX_TEXT_CHARS", "300"))


@lru_cache
def _client():
    # The SDK defaults to a preview API version; pin GA.
    return ContentUnderstandingClient(os.environ["CU_ENDPOINT"], DefaultAzureCredential(),
                                      api_version=os.getenv("CU_API_VERSION", "2025-11-01"))


def _value(field):
    """Turn a CU field (REST JSON shape) into a plain Python value."""
    t = field.get("type")
    if t == "array":
        return [_value(f) for f in field.get("valueArray", [])]
    if t == "object":
        return {k: _value(v) for k, v in field.get("valueObject", {}).items()}
    key = {"string": "valueString", "date": "valueDate", "number": "valueNumber", "integer": "valueInteger",
           "boolean": "valueBoolean", "time": "valueTime", "json": "valueJson"}.get(t)
    return field.get(key) if key else None


def analyze(doc, data):
    """Return (record matching the output contract, raw CU result for debugging)."""
    name = doc["document_name"]
    if name.lower().endswith(".eml"):
        # Same email handling as the Doc Intel path, so both backends see identical input.
        data, mime = email_to_markdown(data).encode("utf-8"), "text/plain"
    else:
        mime = None  # let the service detect PDF / image / Office formats
    result = _client().begin_analyze(f"{PREFIX}classifier",
                                     inputs=[AnalysisInput(data=data, mime_type=mime, name=name)]).result()
    raw = result.as_dict()
    contents = raw["contents"]

    # contents[0] is the classifier's view of the whole file; routed items carry "category" and the fields.
    routed = next((c for c in contents if c.get("category")), None)
    group = routed["category"] if routed and routed["category"] in GROUPS else OTHER
    source = routed or contents[0]
    extracted = {k: _value(v) for k, v in (source.get("fields") or {}).items()}
    figures = [{"figure_id": f["id"], "description": f.get("description")}
               for f in source.get("figures", []) if f.get("description")]
    markdown = source.get("markdown") or contents[0].get("markdown", "")
    # CU's classifier works from text, so a photo-only page is classified by whatever sign text OCR finds in it,
    # and in testing it never picked image_heavy_document. If a document landed in "other" but is basically all
    # figures, relabel it. That's exact: image_heavy's extracted fields are the envelope fields "other" produced.
    if group == OTHER and figures and text_outside_figures(markdown) < IMAGE_HEAVY_MAX_TEXT_CHARS:
        group = "image_heavy_document"
    # CU doesn't return a classification confidence for routed categories in this setup, so leave it empty
    # rather than invent one.
    return build_record(doc, group, None, extracted, markdown, figures), raw
