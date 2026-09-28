"""Document intake from the dashboard or Telegram (FR-032)."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select

from app.agents.accountant.graphs import process_document
from app.core.files import save_upload
from app.db.engine import read_session, write_session
from app.models.books import Document

ALLOWED = ("application/pdf", "image/jpeg", "image/png", "image/webp")


class UnsupportedFileError(ValueError):
    pass


async def submit(business_id: uuid.UUID, data: bytes, mime: str, name: str, channel: str,
                 user_id: uuid.UUID | None) -> dict[str, Any]:
    """Store the original file, create a Document and run document_graph.

    An identical file (same sha256) short-circuits to the existing document instead of being read again.
    """
    if mime not in ALLOWED:
        raise UnsupportedFileError(f"unsupported file type {mime}")
    digest = hashlib.sha256(data).hexdigest()
    async with read_session() as s:
        existing = (await s.execute(select(Document).where(Document.business_id == business_id, Document.sha256 == digest)
                                    .order_by(Document.created_at))).scalars().first()
    if existing is not None:
        return {"document_id": str(existing.id), "invoice_id": str(existing.payable_invoice_id) if existing.payable_invoice_id else None,
                "outcome": "same_file_already_received", "status": existing.status, "waiting_for_owner": False, "issues": []}
    ref = await save_upload(business_id, data, mime, name, user_id)
    async with write_session() as s:
        doc = Document(business_id=business_id, file_id=ref.id, sha256=digest, mime=mime, original_name=name[:255],
                       channel=channel, uploaded_by=user_id, status="received")
        s.add(doc)
        await s.flush()
        doc_id = doc.id
    return await process_document(business_id, doc_id)
