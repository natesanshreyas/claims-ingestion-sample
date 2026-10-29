"""ANALYZER_BACKEND=docintel (MVP): Document Intelligence prebuilt-layout -> chat-completion classifier ->
chat-completion extraction against the group schema.

Doc Intel reads text and layout but doesn't describe images. For image-heavy documents, this backend crops each
figure (Doc Intel `output=figures`) and asks the vision-capable chat model to describe it.
"""
import base64
import json
import os
from functools import lru_cache

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import (
    AnalyzeDocumentRequest,
    AnalyzeOutputOption,
    DocumentContentFormat,
)
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from analyzers import (
    GROUPS,
    OTHER,
    build_record,
    email_to_markdown,
    extraction_schema,
    group_description,
    text_outside_figures,
)

CHAT_MODEL = os.getenv("CHAT_MODEL_DEPLOYMENT", "gpt-4.1")
# Rough cap on how much text goes to the model. Very long documents need chunked extraction (see README).
MAX_CHARS = int(os.getenv("MAX_EXTRACTION_CHARS", "100000"))
MAX_FIGURES = int(os.getenv("MAX_FIGURES_PER_DOCUMENT", "10"))  # bounds vision-model cost per document


@lru_cache
def _clients():
    cred = DefaultAzureCredential()
    di = DocumentIntelligenceClient(os.environ["DOCINTEL_ENDPOINT"], cred)
    # The Foundry project client returns an OpenAI client already authenticated with Entra ID.
    # The OpenAI client retries 429/5xx itself (max_retries=2 by default), so it isn't wrapped in with_retry.
    chat = AIProjectClient(os.environ["FOUNDRY_PROJECT_ENDPOINT"], cred).get_openai_client()
    return di, chat


def _chat_json(messages, schema, name):
    """One chat completion that must return JSON matching `schema`. strict=False because our schemas have
    optional fields; strict mode would require every property to be listed as required."""
    _, chat = _clients()
    resp = chat.chat.completions.create(
        model=CHAT_MODEL, temperature=0, messages=messages,
        response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": False}})
    return json.loads(resp.choices[0].message.content)


def classify(markdown, figure_count=0):
    """Lightweight classifier: group names + descriptions in the prompt, constrained JSON out.
    Doc Intel shows a photo only as <figure> plus any OCR'd text, so the figure count and text length are passed
    in as hints. Without them, a photo-only page looks like an empty document."""
    options = "\n".join(f"- {g}: {group_description(g)}" for g in GROUPS + [OTHER])
    schema = {"type": "object", "required": ["document_group", "confidence"], "properties": {
        "document_group": {"type": "string", "enum": GROUPS + [OTHER]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}
    out = _chat_json([
        {"role": "system", "content": f"Classify an insurance claim document into exactly one group. "
                                      f"Use '{OTHER}' if none clearly fits.\n{options}"},
        {"role": "user", "content": f"[Layout stats: {figure_count} figure(s); "
                                    f"{text_outside_figures(markdown)} "
                                    f"characters of text outside figures]\n\n{markdown[:12000]}"}],
        schema, "classification")
    return out["document_group"], out.get("confidence")


def extract(markdown, group):
    return _chat_json([
        {"role": "system", "content": "Extract the requested fields from this insurance claim document. Use only "
                                      "what the document says; leave a field out if it isn't present. "
                                      "Dates as YYYY-MM-DD."},
        {"role": "user", "content": markdown[:MAX_CHARS]}], extraction_schema(group), group)


def describe_figures(di, operation_id, figures):
    """Crop each figure from the layout result and have the chat model describe it."""
    _, chat = _clients()
    out = []
    for f in (figures or [])[:MAX_FIGURES]:
        png = b"".join(di.get_analyze_result_figure("prebuilt-layout", operation_id, f.id))
        resp = chat.chat.completions.create(model=CHAT_MODEL, temperature=0, messages=[{"role": "user", "content": [
            {"type": "text", "text": "Describe this image from an insurance claim file in 2-3 sentences: what it "
                                     "shows and any visible damage. Don't speculate beyond what is visible."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}])
        out.append({"figure_id": f.id, "description": resp.choices[0].message.content})
    return out


def analyze(doc, data):
    """Return (record matching the output contract, raw Doc Intel result for debugging)."""
    di, _ = _clients()
    figures, operation_id, raw = [], None, {}
    if doc["document_name"].lower().endswith(".eml"):
        markdown = email_to_markdown(data)
    else:
        poller = di.begin_analyze_document(
            "prebuilt-layout", AnalyzeDocumentRequest(bytes_source=data),
            output_content_format=DocumentContentFormat.MARKDOWN, output=[AnalyzeOutputOption.FIGURES])
        result = poller.result()
        markdown, figures, operation_id = result.content, result.figures, poller.details["operation_id"]
        raw = result.as_dict()

    group, confidence = classify(markdown, len(figures or []))
    descriptions = describe_figures(di, operation_id, figures) if group == "image_heavy_document" and figures else []
    # Add the descriptions to the Markdown before extraction, so extraction can use them (e.g. document_type=Photo)
    # and the search index (and the agent) can find image content.
    if descriptions:
        markdown += "\n\n## Figure descriptions\n\n" + "\n".join(
            f"- **Figure {d['figure_id']}:** {d['description']}" for d in descriptions)
    extracted = extract(markdown, group)
    return build_record(doc, group, confidence, extracted, markdown, descriptions), raw
