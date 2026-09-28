"""Append-only audit log writer (FR-009)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import write_session
from app.models.harness import AuditLogEntry


def _jsonable(value: Any) -> Any:
    from datetime import date, datetime
    from decimal import Decimal

    from app.db.types import Money

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if isinstance(value, Money):
        return value.to_json()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def add_audit(
    session: AsyncSession,
    event: str,
    *,
    business_id: uuid.UUID | None,
    agent: str | None = None,
    user_id: uuid.UUID | None = None,
    action_id: uuid.UUID | None = None,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    verification_result: str | None = None,
) -> None:
    """Add an audit entry inside the caller's transaction."""
    session.add(
        AuditLogEntry(
            business_id=business_id,
            event=event,
            agent=agent,
            user_id=user_id,
            action_id=action_id,
            inputs=_jsonable(inputs or {}),
            outputs=_jsonable(outputs or {}),
            verification_result=verification_result,
        )
    )


async def audit(event: str, **kwargs: Any) -> None:
    """Write an audit entry in its own transaction."""
    async with write_session() as s:
        add_audit(s, event, **kwargs)


jsonable = _jsonable
