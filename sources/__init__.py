"""Where claim documents come from. Pick one with DOCUMENT_SOURCE=local|blob|documentum.

Claim documents live on a file share (not in Databricks). Every source returns the same document dict:
    {claim_id, document_id, document_name, source_path}
and can read a document's bytes. Databricks is used only for structured claims data and log notes
(see log_notes_to_search.py) and for the pipeline's status table.
"""
import hashlib
import os
from abc import ABC, abstractmethod
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

# File types the analyzers handle. Anything else on the share is skipped (and logged) rather than failing.
SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".docx", ".xlsx", ".pptx", ".html", ".eml"}


def _doc(claim_id, relative_path, source_path):
    # document_id = hash of the path within the share. It stays the same across runs (idempotent re-processing)
    # and is safe to use in blob names and as a search key. A renamed or moved file counts as a new document.
    return {"claim_id": claim_id, "document_id": hashlib.sha1(relative_path.encode()).hexdigest()[:16],
            "document_name": Path(relative_path).name, "source_path": source_path}


class DocumentSource(ABC):
    @abstractmethod
    def list_documents(self):
        """Return document dicts for every supported file."""

    @abstractmethod
    def read(self, doc):
        """Return the document's bytes."""


class LocalOrBlobSource(DocumentSource):
    """Stand-in for the claims file share. Expected layout: <root>/<claim_id>/<any subfolders>/<file>.

    local: RAW_LOCAL_DIR is a folder, e.g. the file share mounted or copied to the machine running this.
    blob:  RAW_CONTAINER in STORAGE_ACCOUNT_URL, e.g. after AzCopy or Azure File Sync lands the share in Blob.
    """

    def __init__(self, mode):
        self.mode = mode
        if mode == "local":
            self.root = Path(os.environ["RAW_LOCAL_DIR"])
        else:
            self.container = ContainerClient(os.environ["STORAGE_ACCOUNT_URL"], os.getenv("RAW_CONTAINER", "raw"),
                                             credential=DefaultAzureCredential())

    def list_documents(self):
        if self.mode == "local":
            paths = [p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()]
        else:
            paths = [b.name for b in self.container.list_blobs()]
        docs = []
        for rel in sorted(paths):
            parts = rel.split("/")
            if len(parts) < 2 or Path(rel).suffix.lower() not in SUPPORTED:
                continue  # files must sit under a claim folder and be a supported type
            source_path = str(self.root / rel) if self.mode == "local" else f"{self.container.url}/{rel}"
            docs.append(_doc(parts[0], rel, source_path))
        return docs

    def read(self, doc):
        if self.mode == "local":
            return Path(doc["source_path"]).read_bytes()
        name = doc["source_path"][len(self.container.url) + 1:]
        return self.container.download_blob(name).readall()


class DocumentumSource(DocumentSource):
    """TODO: Documentum (on-prem) source. Not implemented.

    It would need:
      - API access: Documentum REST Services (or D2/DFC) reachable from where this runs. On-prem means a
        self-hosted runtime, VPN/ExpressRoute, or an export job that lands files in Blob
        (then use DOCUMENT_SOURCE=blob).
      - Auth: a Documentum service account (basic auth or the customer's SSO/OAuth setup), with credentials
        in Key Vault, never in code or .env.
      - Mapping: a DQL query that lists a claim's documents (e.g. by a claim number attribute) -> claim_id,
        r_object_id -> document_id, object_name -> document_name.
      - Content download: GET the object's content (primary rendition) as bytes.
    """

    def list_documents(self):
        raise NotImplementedError("Documentum source is a stub; see the class docstring")

    def read(self, doc):
        raise NotImplementedError("Documentum source is a stub; see the class docstring")


def get_document_source():
    kind = os.getenv("DOCUMENT_SOURCE", "local").lower()
    if kind in ("local", "blob"):
        return LocalOrBlobSource(kind)
    if kind == "documentum":
        return DocumentumSource()
    raise ValueError(f"DOCUMENT_SOURCE must be local, blob, or documentum; got {kind!r}")
