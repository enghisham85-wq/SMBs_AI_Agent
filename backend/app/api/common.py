"""Response helpers shared by routers."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.db.engine import read_session
from app.harness.audit import jsonable
from app.models.tenancy import Business


def J(data: Any, status: int = 200) -> JSONResponse:
    """JSON response with Money, UUID, Decimal and dates converted (Money includes `decimals`)."""
    return JSONResponse(jsonable(data), status_code=status)


def with_freshness(data: dict[str, Any], sources: dict[str, datetime | date | None]) -> dict[str, Any]:
    """Attach `data_as_of` (FR-046): when each source behind the figures was last updated."""
    data["data_as_of"] = {k: (v.isoformat() if v is not None else None) for k, v in sources.items()}
    return data


async def get_business(business_id: uuid.UUID) -> Business:
    async with read_session() as s:
        b = await s.get(Business, business_id)
    assert b is not None
    return b


async def first_business_id() -> uuid.UUID | None:
    async with read_session() as s:
        return (await s.execute(select(Business.id).order_by(Business.created_at))).scalars().first()
