"""Analyzer backends and the shared pieces both use: schema loading and the output contract.

Every backend's analyze(doc, data) returns (record, raw_result). The record has the same shape for both, so
nothing downstream (Blob, Search, the agent) cares which one ran:
    {claim_id, document_id, source_path, document_group, document_type, document_date, summary,
     key_entities, confidence, <group fields>, full_text_markdown}
"""
import copy
import email
import json
import os
import re
from email import policy
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
GROUPS = ["correspondence_email", "investigation_report", "external_agency_document",
          "medical_legal_document", "image_heavy_document"]
OTHER = "other"  # documents that match no group get the envelope fields only


def _load(name):
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())


def group_description(group):
    if group == OTHER:  # keep the catch-all narrow, or classifiers drift toward it (photo pages did in testing)
        return ("Text documents that clearly fit none of the other groups, such as policy forms or internal notes. "
                "Never use this for documents that are mostly photos or diagrams.")
    return _load(group)["description"]


def text_outside_figures(markdown):
    """Length of the text that isn't inside a figure. Strips both Doc Intel (<figure>...</figure>) and
    CU (![alt](figures/1.1 "description")) figure markup. A tiny value plus some figures means a photo document."""
    text = re.sub(r"<figure>.*?</figure>", "", markdown, flags=re.DOTALL)
    text = re.sub(r'!\[[^\]]*\]\(figures/[^)"]*(?:"[^"]*")?\)', "", text)
    return len(re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip())


def extraction_schema(group):
    """Fields the model must extract for a group: envelope fields + group fields, minus the ones code fills in
    (x-source=pipeline) or that come from figure analysis (x-source=figures)."""
    props = copy.deepcopy(_load("envelope")["properties"])
    if group != OTHER:
        props.update(copy.deepcopy(_load(group)["properties"]))
    props = {k: v for k, v in props.items() if v.get("x-source") not in ("pipeline", "figures")}
    for v in props.values():
        v.pop("x-source", None)
    return {"type": "object", "properties": props}


def build_record(doc, group, confidence, extracted, markdown, figure_descriptions=None):
    """Assemble the output contract. Pipeline fields win over anything the model returned."""
    # Empty strings become None so both backends report "not found" the same way.
    record = {k: (extracted.get(k) if extracted.get(k) != "" else None) for k in extraction_schema(group)["properties"]}
    if group == "image_heavy_document":
        record["figure_descriptions"] = figure_descriptions or []
    return {"claim_id": doc["claim_id"], "document_id": doc["document_id"], "source_path": doc["source_path"],
            "document_group": group, "confidence": confidence, **record, "full_text_markdown": markdown}


def email_to_markdown(data):
    """Neither Doc Intel layout nor this sample's CU path takes .eml directly, so emails become Markdown text
    up front. Headers stay in, so sender, recipients, and date can still be extracted."""
    msg = email.message_from_bytes(data, policy=policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    headers = "\n".join(f"**{h}:** {msg[h]}" for h in ("From", "To", "Cc", "Date", "Subject") if msg[h])
    attachments = [a.get_filename() for a in msg.iter_attachments() if a.get_filename()]
    note = ("\n\n_Attachments (not analyzed here; save them to the share as separate documents):_ "
            + ", ".join(attachments)) if attachments else ""
    return f"# {msg['Subject'] or 'Email'}\n\n{headers}\n\n{text}{note}"


def get_backend():
    """ANALYZER_BACKEND=cu (default) or docintel. Imported lazily so you only need the SDK you use."""
    backend = os.getenv("ANALYZER_BACKEND", "cu").lower()
    if backend == "docintel":
        from analyzers import docintel_backend
        return docintel_backend
    if backend == "cu":
        from analyzers import cu_backend
        return cu_backend
    raise ValueError(f"ANALYZER_BACKEND must be 'docintel' or 'cu', got {backend!r}")
