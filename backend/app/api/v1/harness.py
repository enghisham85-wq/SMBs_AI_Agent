"""Harness endpoints (T103): live actions and stream, incidents, learned rules, calibration, audit log."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.api.common import J
from app.approvals import service as approvals
from app.core import clock
from app.core.auth import CurrentUser, RequireManager, RequireOwner
from app.core.errors import AppError, not_found
from app.db.engine import read_session
from app.graphs.streaming import broker
from app.harness import confidence, rules
from app.models.harness import (
    Action,
    AgentCalibration,
    AgentCalibrationHistory,
    ApprovalRequest,
    AuditLogEntry,
    CheckResult,
    Incident,
    LearnedRule,
)

router = APIRouter(tags=["harness"])
FINAL_STAGES = ("completed", "escalated", "failed", "rejected")
AGENTS = ("stock", "cashflow", "accountant")


def _action_out(a: Action) -> dict[str, Any]:
    return {"id": a.id, "agent": a.agent, "type": a.type, "stage": a.stage, "risk_class": a.risk_class,
            "attempt": a.attempt, "created_at": a.created_at, "updated_at": a.updated_at, "incident_id": a.incident_id,
            "parent_action_id": a.parent_action_id, "dry_run": a.dry_run}


# ------------------------------------------------------------------ actions
@router.get("/harness/actions")
async def list_actions(live: bool = False, agent: str | None = None, limit: int = 100,
                       user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        q = select(Action).where(Action.business_id == user.business_id)
        if live:
            q = q.where(Action.stage.not_in(FINAL_STAGES))
        if agent:
            q = q.where(Action.agent == agent)
        rows = (await s.execute(q.order_by(Action.created_at.desc()).limit(max(1, min(limit, 500))))).scalars().all()
    return J({"actions": [_action_out(a) for a in rows]})


@router.get("/harness/actions/{action_id}")
async def action_detail(action_id: uuid.UUID, user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        a = await s.get(Action, action_id)
        if a is None or a.business_id != user.business_id:
            raise not_found("Action")
        checks = (await s.execute(select(CheckResult).where(CheckResult.action_id == action_id))).scalars().all()
        trail = (await s.execute(select(AuditLogEntry).where(AuditLogEntry.action_id == action_id)
                                 .order_by(AuditLogEntry.business_time, AuditLogEntry.wall_time))).scalars().all()
        reqs = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.action_id == action_id)
                                .order_by(ApprovalRequest.created_at))).scalars().all()
        children = (await s.execute(select(Action).where(Action.parent_action_id == action_id))).scalars().all()
    return J({"action": {**_action_out(a), "plan": a.plan, "inputs": a.inputs, "result": a.result,
                         "verifier_verdict": a.verifier_verdict},
              "checks": [{"name": c.check_name, "passed": c.passed, "details": c.details,
                          "learned_rule_id": c.learned_rule_id} for c in checks],
              "audit": [{"event": e.event, "at": e.business_time, "wall_time": e.wall_time, "agent": e.agent,
                         "user_id": e.user_id, "inputs": e.inputs, "outputs": e.outputs,
                         "verification": e.verification_result} for e in trail],
              "approvals": [approvals.to_dict(r) for r in reqs],
              "children": [_action_out(c) for c in children]})


@router.get("/harness/stream")
async def harness_stream(request: Request, user: CurrentUser = RequireManager) -> EventSourceResponse:
    queue = broker.subscribe("harness")
    bid = str(user.business_id)

    async def gen() -> AsyncIterator[dict[str, str]]:
        try:
            yield {"event": "hello", "data": "{}"}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                if msg.get("business_id") not in (None, bid):
                    continue
                if "business_id" not in msg and "kind" not in msg:
                    continue  # raw graph updates carry no business id; the stage messages do
                yield {"event": str(msg.get("kind", "message")), "data": json.dumps(msg, ensure_ascii=False, default=str)}
        finally:
            broker.unsubscribe("harness", queue)

    return EventSourceResponse(gen())


# ------------------------------------------------------------------ incidents
@router.get("/harness/incidents")
async def list_incidents(status: str | None = None, agent: str | None = None, limit: int = 200,
                         user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        q = select(Incident).where(Incident.business_id == user.business_id)
        if status:
            q = q.where(Incident.status == status)
        if agent:
            q = q.where(Incident.agent == agent)
        rows = (await s.execute(q.order_by(Incident.created_at.desc()).limit(max(1, min(limit, 500))))).scalars().all()
        ids = [r.id for r in rows]
        proposed = (await s.execute(select(LearnedRule).where(LearnedRule.source_incident_id.in_(ids)))).scalars().all() if ids else []
    by_inc: dict[uuid.UUID, list[LearnedRule]] = {}
    for r in proposed:
        by_inc.setdefault(r.source_incident_id, []).append(r)  # type: ignore[arg-type]
    return J({"incidents": [{
        "id": i.id, "agent": i.agent, "type": i.type, "detected_by": i.detected_by, "summary": i.summary,
        "action_taken": i.action_taken, "root_cause": i.root_cause, "category": i.category, "status": i.status,
        "refs": i.refs, "action_id": i.action_id, "chaos_injection_id": i.chaos_injection_id, "created_at": i.created_at,
        "resolved_at": i.resolved_at,
        "rules": [{"id": r.id, "status": r.status, "text_en": r.rule_text_en} for r in by_inc.get(i.id, [])]} for i in rows]})


# ------------------------------------------------------------------ learned rules
def _rule_out(r: LearnedRule) -> dict[str, Any]:
    return {"id": r.id, "agent": r.agent, "kind": r.kind, "rule_text_en": r.rule_text_en, "rule_text_ar": r.rule_text_ar,
            "trigger": r.trigger, "status": r.status, "version": r.version, "previous_version_id": r.previous_version_id,
            "source_incident_id": r.source_incident_id, "approved_by": r.approved_by, "times_applied": r.times_applied,
            "times_overridden": r.times_overridden, "status_changed_at": r.status_changed_at, "created_at": r.created_at}


@router.get("/harness/rules")
async def list_rules(status: str | None = None, user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        q = select(LearnedRule).where(LearnedRule.business_id == user.business_id)
        if status:
            q = q.where(LearnedRule.status == status)
        rows = (await s.execute(q.order_by(LearnedRule.created_at.desc()))).scalars().all()
    return J({"rules": [_rule_out(r) for r in rows]})


def _rule_error(exc: rules.RuleError) -> AppError:
    if exc.code == "not_found":
        return not_found("Rule")
    return AppError(409 if exc.code == "invalid_state" else 422, exc.code, message_en=str(exc),
                    message_ar="لا يمكن تنفيذ هذا الإجراء على القاعدة.")


async def _rule_action(fn: Any, rule_id: uuid.UUID, user: CurrentUser) -> Response:
    try:
        rule = await fn(rule_id, user.business_id, user.id)
    except rules.RuleError as exc:
        raise _rule_error(exc) from exc
    return J(_rule_out(rule))


@router.post("/harness/rules/{rule_id}/approve")
async def approve_rule(rule_id: uuid.UUID, user: CurrentUser = RequireOwner) -> Response:
    return await _rule_action(rules.approve, rule_id, user)


@router.post("/harness/rules/{rule_id}/reject")
async def reject_rule(rule_id: uuid.UUID, user: CurrentUser = RequireOwner) -> Response:
    return await _rule_action(rules.reject, rule_id, user)


@router.post("/harness/rules/{rule_id}/deactivate")
async def deactivate_rule(rule_id: uuid.UUID, user: CurrentUser = RequireOwner) -> Response:
    return await _rule_action(rules.deactivate, rule_id, user)


class RuleEdit(BaseModel):
    rule_text_en: str | None = Field(None, min_length=3, max_length=500)
    rule_text_ar: str | None = Field(None, min_length=1, max_length=500)
    trigger: dict[str, Any] | None = None


@router.patch("/harness/rules/{rule_id}")
async def edit_rule(rule_id: uuid.UUID, body: RuleEdit, user: CurrentUser = RequireOwner) -> Response:
    try:
        rule = await rules.edit(rule_id, user.business_id, user.id, rule_text_en=body.rule_text_en,
                                rule_text_ar=body.rule_text_ar, trigger=body.trigger)
    except rules.RuleError as exc:
        raise _rule_error(exc) from exc
    return J(_rule_out(rule))


# ------------------------------------------------------------------ calibration
@router.get("/harness/calibration")
async def calibration_view(days: int = 30, user: CurrentUser = RequireManager) -> Response:
    since = clock.today() - timedelta(days=max(1, min(days, 365)))
    async with read_session() as s:
        rows = (await s.execute(select(AgentCalibration).where(AgentCalibration.business_id == user.business_id))).scalars().all()
        hist = (await s.execute(select(AgentCalibrationHistory).where(
            AgentCalibrationHistory.business_id == user.business_id, AgentCalibrationHistory.date >= since)
            .order_by(AgentCalibrationHistory.date))).scalars().all()
    agents = []
    for agent in AGENTS:
        high, low = await confidence.thresholds(user.business_id, agent)
        mine = [r for r in rows if r.agent == agent]
        agents.append({"agent": agent, "high_confidence_threshold": high, "low_confidence_threshold": low,
                       "auto_approve_factor": min((r.auto_approve_factor for r in mine), default=1.0),
                       "degraded": any(r.degraded for r in mine),
                       "metrics": [{"metric": r.metric, "current_value": r.current_value, "threshold": r.threshold,
                                    "degraded": r.degraded, "degraded_since": r.degraded_since,
                                    "restore_progress": r.restore_progress, "healthy_streak": r.healthy_streak,
                                    "method_override": r.method_override} for r in mine]})
    return J({"agents": agents,
              "history": [{"agent": h.agent, "metric": h.metric, "date": h.date, "value": h.value, "threshold": h.threshold,
                           "degraded": h.degraded, "high_confidence_threshold": h.high_confidence_threshold} for h in hist],
              "data_as_of": {"clock": clock.clock_now().isoformat()}})


# ------------------------------------------------------------------ audit log and digest
@router.get("/audit-log")
async def audit_log(event: str | None = None, action_id: uuid.UUID | None = None, agent: str | None = None,
                    limit: int = 200, offset: int = 0, user: CurrentUser = RequireOwner) -> Response:
    async with read_session() as s:
        q = select(AuditLogEntry).where(AuditLogEntry.business_id == user.business_id)
        if event:
            q = q.where(AuditLogEntry.event == event)
        if action_id:
            q = q.where(AuditLogEntry.action_id == action_id)
        if agent:
            q = q.where(AuditLogEntry.agent == agent)
        rows = (await s.execute(q.order_by(AuditLogEntry.business_time.desc(), AuditLogEntry.wall_time.desc())
                                .offset(max(0, offset)).limit(max(1, min(limit, 1000))))).scalars().all()
    return J({"entries": [{"id": e.id, "event": e.event, "at": e.business_time, "wall_time": e.wall_time, "agent": e.agent,
                           "user_id": e.user_id, "action_id": e.action_id, "inputs": e.inputs, "outputs": e.outputs,
                           "verification": e.verification_result} for e in rows]})


@router.get("/harness/digest")
async def digest_today(user: CurrentUser = RequireManager) -> Response:
    from app.harness import digest

    return J(await digest.collect(user.business_id, clock.today()))
