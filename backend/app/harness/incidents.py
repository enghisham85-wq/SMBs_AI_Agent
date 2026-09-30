"""Incident records (FR-007)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import clock_now
from app.core.events import publish
from app.db.engine import write_session
from app.graphs.streaming import broker
from app.harness.audit import jsonable
from app.models.harness import Incident


def add_incident(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    agent: str,
    type: str,
    detected_by: str,
    summary: str,
    refs: dict[str, Any] | None = None,
    action_id: uuid.UUID | None = None,
    action_taken: str = "",
    chaos_injection_id: uuid.UUID | None = None,
    dedupe_key: str | None = None,
) -> Incident:
    inc = Incident(
        business_id=business_id,
        agent=agent,
        type=type,
        detected_by=detected_by,
        summary=summary,
        refs=jsonable(refs or {}),
        action_id=action_id,
        action_taken=action_taken,
        chaos_injection_id=chaos_injection_id,
        dedupe_key=dedupe_key,
    )
    session.add(inc)
    return inc


async def open_incident(**kwargs: Any) -> uuid.UUID:
    """Open an incident unless an open one with the same dedupe_key exists. Returns its id."""
    dedupe = kwargs.get("dedupe_key")
    async with write_session() as s:
        if dedupe:
            existing = (
                await s.execute(
                    select(Incident).where(
                        Incident.business_id == kwargs["business_id"],
                        Incident.dedupe_key == dedupe,
                        Incident.status.in_(("open", "investigating")),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing.id
        if kwargs.get("chaos_injection_id") is None:
            from app.chaos.context import current_injection

            kwargs["chaos_injection_id"] = current_injection()
        inc = add_incident(s, **kwargs)
        await s.flush()
        publish(
            s,
            "incident.opened",
            {"incident_id": inc.id, "agent": inc.agent, "type": inc.type, "summary": inc.summary},
            producer="harness",
            business_id=inc.business_id,
            action_id=inc.action_id,
        )
        inc_id = inc.id
    broker.publish("harness", {"kind": "incident.opened", "incident_id": inc_id, "summary": kwargs["summary"]})
    return inc_id


async def resolve_incident(
    incident_id: uuid.UUID, *, root_cause: str | None = None, category: str | None = None, action_taken: str | None = None
) -> None:
    async with write_session() as s:
        inc = await s.get(Incident, incident_id)
        if inc is None:
            return
        inc.status = "resolved"
        inc.resolved_at = clock_now()
        if root_cause:
            inc.root_cause = root_cause
        if category:
            inc.category = category
        if action_taken:
            inc.action_taken = action_taken
        publish(
            s,
            "incident.resolved",
            {"incident_id": inc.id, "agent": inc.agent, "type": inc.type, "summary": inc.summary},
            producer="harness",
            business_id=inc.business_id,
        )
