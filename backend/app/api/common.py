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

# big enough that the current screens never actually page
DEFAULT_LIMIT, MAX_LIMIT, MAX_OFFSET = 500, 1000, 2**31


def page(limit: int, offset: int) -> tuple[int, int]:
    """Clamp limit/offset. The offset cap stops SQLite choking on integers past 64 bits."""
    return max(1, min(limit, MAX_LIMIT)), max(0, min(offset, MAX_OFFSET))


def J(data: Any, status: int = 200) -> JSONResponse:
    """JSONResponse that understands Money, UUID, Decimal and dates."""
    return JSONResponse(jsonable(data), status_code=status)


def with_freshness(data: dict[str, Any], sources: dict[str, datetime | date | str | None]) -> dict[str, Any]:
    """Add `data_as_of` with each source's last update time."""
    data["data_as_of"] = {k: (v.isoformat() if isinstance(v, date | datetime) else v) for k, v in sources.items()}
    return data


async def get_business(business_id: uuid.UUID) -> Business:
    async with read_session() as s:
        b = await s.get(Business, business_id)
    assert b is not None
    return b


async def first_business_id() -> uuid.UUID | None:
    async with read_session() as s:
        return (await s.execute(select(Business.id).order_by(Business.created_at))).scalars().first()
