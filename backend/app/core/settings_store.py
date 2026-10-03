"""Per-business settings with defaults."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import read_session
from app.models.tenancy import SETTING_DEFAULTS, Setting


async def get_all(business_id: uuid.UUID, session: AsyncSession | None = None) -> dict[str, Any]:
    async def _load(s: AsyncSession) -> dict[str, Any]:
        rows = (await s.execute(select(Setting).where(Setting.business_id == business_id))).scalars().all()
        values = dict(SETTING_DEFAULTS)
        values.update({r.key: r.value for r in rows})
        return values

    if session is not None:
        return await _load(session)
    async with read_session() as s:
        return await _load(s)


async def get(business_id: uuid.UUID, key: str, session: AsyncSession | None = None) -> Any:
    return (await get_all(business_id, session))[key]


async def put(session: AsyncSession, business_id: uuid.UUID, key: str, value: Any) -> None:
    row = (
        await session.execute(select(Setting).where(Setting.business_id == business_id, Setting.key == key))
    ).scalar_one_or_none()
    if row is None:
        session.add(Setting(business_id=business_id, key=key, value=value))
    else:
        row.value = value
