"""Uploaded files stored on disk under FILES_DIR, named by their sha256."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import uuid
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db.engine import read_session, write_session
from app.models.tenancy import FileRef

MAX_BYTES = 20 * 1024 * 1024


class FileTooLargeError(ValueError):
    pass


async def save_upload(business_id: uuid.UUID, data: bytes, mime: str, name: str,
                      user_id: uuid.UUID | None = None) -> FileRef:
    if len(data) > MAX_BYTES:
        raise FileTooLargeError("file is larger than 20 MB")
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
