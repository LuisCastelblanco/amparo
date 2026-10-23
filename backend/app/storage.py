"""Finished tutela documents, stored under an unguessable ID so the UI can link to them directly.

Each case's .docx and PDF are rendered when the case completes and kept for DOCUMENT_TTL_DAYS: in a Cloud
Storage bucket whose lifecycle rule deletes them (DOCUMENT_STORE=gcs), or in process memory for local
development. Downloads are plain links (GET with Content-Disposition: attachment), which mobile browsers
handle better than a fetch followed by a blob URL.
"""

import asyncio
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Protocol

from app import document
from app.config import get_settings
from app.schemas import TutelaDraft, TutelaSections

DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
FILENAME_RE = re.compile(r"^tutela-[a-z0-9-]{1,80}\.(docx|pdf)$")


class DocumentStore(Protocol):
    async def put(self, doc_id: str, name: str, content: bytes, content_type: str) -> None: ...

    async def get(self, doc_id: str, name: str) -> tuple[bytes, str] | None:
        """The file and its content type, or None if it does not exist or has expired."""


def _ttl() -> timedelta:
    return timedelta(days=get_settings().document_ttl_days)


class MemoryDocumentStore:
    MAX_FILES = 400

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str, float]] = {}

    async def put(self, doc_id: str, name: str, content: bytes, content_type: str) -> None:
        if len(self.files) >= self.MAX_FILES:  # bound memory: drop the oldest
            oldest = min(self.files, key=lambda k: self.files[k][2])
            del self.files[oldest]
        self.files[f"{doc_id}/{name}"] = (content, content_type, time.time())

    async def get(self, doc_id: str, name: str) -> tuple[bytes, str] | None:
        item = self.files.get(f"{doc_id}/{name}")
        if item is None or time.time() - item[2] > _ttl().total_seconds():
            return None
        return item[0], item[1]


class GcsDocumentStore:
    """Objects <doc_id>/<name>; the bucket's lifecycle rule deletes them after DOCUMENT_TTL_DAYS."""

    def __init__(self, bucket: str) -> None:
        from google.cloud import storage

        self.bucket = storage.Client().bucket(bucket)

    async def put(self, doc_id: str, name: str, content: bytes, content_type: str) -> None:
        blob = self.bucket.blob(f"{doc_id}/{name}")
        await asyncio.to_thread(blob.upload_from_string, content, content_type=content_type)

    async def get(self, doc_id: str, name: str) -> tuple[bytes, str] | None:
        def fetch() -> tuple[bytes, str] | None:
            blob = self.bucket.get_blob(f"{doc_id}/{name}")
            # Lifecycle deletion can lag by up to a day; enforce the TTL here too.
            if blob is None or blob.time_created < datetime.now(UTC) - _ttl():
                return None
            return blob.download_as_bytes(), blob.content_type or "application/octet-stream"

        return await asyncio.to_thread(fetch)


@lru_cache
def get_document_store() -> DocumentStore:
    settings = get_settings()
    if settings.document_store == "gcs":
        if not settings.documents_bucket:
            raise RuntimeError("DOCUMENTS_BUCKET must be set when DOCUMENT_STORE=gcs")
        return GcsDocumentStore(settings.documents_bucket)
    if settings.document_store != "memory":
        raise ValueError(f"DOCUMENT_STORE must be 'gcs' or 'memory', not {settings.document_store!r}")
    return MemoryDocumentStore()


def render(draft: TutelaDraft | dict[str, Any]) -> dict[str, tuple[bytes, str]]:
    """{filename: (content, content type)} for the .docx and the PDF of a draft."""
    draft = draft if isinstance(draft, TutelaDraft) else TutelaDraft(**draft)
    sections = TutelaSections(**draft.model_dump(include=set(TutelaSections.model_fields)))
    blocks = document.tutela_blocks(draft.accionada, draft.ciudad, sections)
    return {
        document.filename(draft.accionada, "docx"): (document.render_docx(blocks), document.CONTENT_TYPES["docx"]),
        document.filename(draft.accionada, "pdf"): (document.render_pdf(blocks), document.CONTENT_TYPES["pdf"]),
    }


async def save(draft: dict[str, Any]) -> dict[str, str]:
    """Render and store both files; return their download paths on the API, keyed by format."""
    files = await asyncio.to_thread(render, draft)
    doc_id = secrets.token_urlsafe(24)
    store = get_document_store()
    await asyncio.gather(*(store.put(doc_id, name, content, ctype) for name, (content, ctype) in files.items()))
    return {name.rsplit(".", 1)[1]: f"/api/documents/{doc_id}/{name}" for name in files}
