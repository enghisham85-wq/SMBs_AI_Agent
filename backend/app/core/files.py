"""Uploaded files stored on disk under FILES_DIR, named by their sha256."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.core.errors import AppError
from app.db.engine import read_session, write_session
from app.models.tenancy import FileRef

MAX_BYTES = 20 * 1024 * 1024
# Room for the other form fields and multipart framing around a file of MAX_BYTES.
_FORM_OVERHEAD = 1024 * 1024
_CHUNK = 1024 * 1024


class FileTooLargeError(AppError, ValueError):
    def __init__(self) -> None:
        super().__init__(413, "file_too_large", message_en="The file is larger than 20 MB.",
                         message_ar="حجم الملف أكبر من 20 ميغابايت.")


async def read_upload(file: UploadFile) -> bytes:
    """The upload's bytes, refusing anything over MAX_BYTES without reading past the limit."""
    if file.size is not None and file.size > MAX_BYTES:
        raise FileTooLargeError()
    buf = bytearray()
    while chunk := await file.read(_CHUNK):
        buf += chunk
        if len(buf) > MAX_BYTES:
            raise FileTooLargeError()
    return bytes(buf)


class UploadLimit:
    """ASGI middleware: refuses a multipart body whose Content-Length is already over the limit,
    before Starlette spools it to disk. Bodies without a Content-Length are capped by read_upload."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            ctype = headers.get(b"content-type", b"")
            length = headers.get(b"content-length", b"")
            if ctype.startswith(b"multipart/form-data") and length.isdigit()                     and int(length) > MAX_BYTES + _FORM_OVERHEAD:
                from fastapi.responses import JSONResponse

                err = FileTooLargeError()
                await JSONResponse(err.body(), status_code=err.status, headers={"Connection": "close"})(
                    scope, receive, send)
                return
        await self.app(scope, receive, send)


async def save_upload(business_id: uuid.UUID, data: bytes, mime: str, name: str,
                      user_id: uuid.UUID | None = None) -> FileRef:
    if len(data) > MAX_BYTES:
        raise FileTooLargeError()
    digest = hashlib.sha256(data).hexdigest()
    ext = Path(name).suffix or (mimetypes.guess_extension(mime) or "")
    path = Path(get_settings().FILES_DIR) / f"{digest}{ext}"

    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)

    await asyncio.to_thread(_write)
    async with write_session() as s:
        ref = FileRef(business_id=business_id, sha256=digest, path=str(path), mime=mime, original_name=name[:255],
                      uploaded_by=user_id)
        s.add(ref)
        await s.flush()
        return ref


async def existing_by_sha(business_id: uuid.UUID, digest: str) -> list[FileRef]:
    async with read_session() as s:
        return list((await s.execute(select(FileRef).where(FileRef.business_id == business_id,
                                                           FileRef.sha256 == digest))).scalars())


def read_bytes(ref: FileRef) -> bytes:
    return Path(ref.path).read_bytes()
