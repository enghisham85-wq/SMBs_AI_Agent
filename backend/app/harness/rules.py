"""Learned rules (FR-008). Proposals are created here; approval, editing and application are extended in US4."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.core.clock import clock_now
from app.db.engine import read_session, write_session
from app.graphs.streaming import broker
from app.models.harness import LearnedRule


async def propose(*, business_id: uuid.UUID, agent: str, kind: str, rule_text_en: str, rule_text_ar: str,
                  trigger: dict[str, Any], source_incident_id: uuid.UUID | None) -> uuid.UUID:
    async with write_session() as s:
        rule = LearnedRule(business_id=business_id, agent=agent, kind=kind, rule_text_en=rule_text_en,
                           rule_text_ar=rule_text_ar, trigger=trigger, source_incident_id=source_incident_id,
                           proposed_by=agent, status="proposed", status_changed_at=clock_now())
        s.add(rule)
        await s.flush()
        rid = rule.id
    broker.publish("harness", {"kind": "rule.proposed", "rule_id": rid, "text": rule_text_en})
    return rid


async def active_rules(business_id: uuid.UUID, agent: str | None = None, kind: str | None = None) -> list[LearnedRule]:
    async with read_session() as s:
        q = select(LearnedRule).where(LearnedRule.business_id == business_id, LearnedRule.status == "active")
        if agent:
            q = q.where(LearnedRule.agent == agent)
        if kind:
            q = q.where(LearnedRule.kind == kind)
        return list((await s.execute(q)).scalars())


async def recent_for_trigger(business_id: uuid.UUID, kind: str, match: dict[str, Any], statuses: tuple[str, ...],
                             within_days: int | None = None) -> list[LearnedRule]:
    """Rules of a kind whose trigger contains all key/values of `match`, optionally changed recently."""
    async with read_session() as s:
        rows = (await s.execute(select(LearnedRule).where(LearnedRule.business_id == business_id, LearnedRule.kind == kind,
                                                          LearnedRule.status.in_(statuses)))).scalars().all()
    cutoff = clock_now() - timedelta(days=within_days) if within_days else None
    return [r for r in rows if all(str(r.trigger.get(k)) == str(v) for k, v in match.items())
            and (cutoff is None or (r.status_changed_at or r.created_at) >= cutoff)]


async def mark_applied(rule_id: uuid.UUID) -> None:
    async with write_session() as s:
        r = await s.get(LearnedRule, rule_id)
        if r is not None:
            r.times_applied += 1
