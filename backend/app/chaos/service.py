"""Chaos mode service (T116): inject a scenario, then report what the agents did (SC-001, SC-011).

Only available in demo mode. While a scenario runs, incidents it causes are linked to the injection
(app.chaos.context), and the live harness stream is watched to time each stage: injection ->
detection -> explanation -> correction -> proposed rule.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any

from sqlalchemy import select

from app.chaos import context
from app.chaos.scenarios import (
    REGISTRY,
    ChaosError,
    Ctx,
    Scenario,
    injection_count,
    linked_incidents,
    ordered,
)
from app.config import get_settings
from app.core import clock
from app.db.engine import read_session, write_session
from app.graphs.streaming import broker
from app.harness.audit import add_audit, jsonable
from app.models.chaos import ChaosInjection
from app.models.harness import ApprovalRequest, LearnedRule
from app.models.tenancy import Business

log = logging.getLogger(__name__)
_lock = asyncio.Lock()  # one scenario at a time: they share the business clock


class ChaosDisabledError(Exception):
    pass


class ChaosBusyError(Exception):
    pass


def scenarios() -> list[dict[str, Any]]:
    return [{"key": sc.key, "number": sc.number, "agent": sc.agent, "title_en": sc.title_en, "title_ar": sc.title_ar,
             "expected_en": sc.expected_en, "expected_ar": sc.expected_ar, "parameters": sc.params,
             "advances_clock": sc.advances_clock} for sc in ordered()]


class _Recorder:
    """Stamps harness and chat stream messages with seconds since the injection started."""

    def __init__(self) -> None:
        self.start = time.monotonic()
        self.seen: list[tuple[float, str, dict[str, Any]]] = []
        self._queues = {ch: broker.subscribe(ch) for ch in ("harness", "chat")}
        self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while True:
            for ch, q in self._queues.items():
                while not q.empty():
                    self.seen.append((time.monotonic() - self.start, ch, q.get_nowait()))
            await asyncio.sleep(0.02)

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    async def stop(self) -> None:
        await asyncio.sleep(0)  # let the last messages land
        for ch, q in self._queues.items():
            while not q.empty():
                self.seen.append((self.elapsed(), ch, q.get_nowait()))
            broker.unsubscribe(ch, q)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    def first(self, pred: Any) -> float | None:
        return next((t for t, ch, msg in self.seen if pred(ch, msg)), None)


async def inject(business_id: uuid.UUID, scenario: str, params: dict[str, Any] | None, user_id: uuid.UUID | None) -> ChaosInjection:
    if not get_settings().DEMO_MODE:
        raise ChaosDisabledError("Chaos mode is only available in demo mode")
    sc = REGISTRY.get(scenario)
    if sc is None:
        raise ChaosError(f"unknown scenario {scenario!r}")
    if _lock.locked():
        raise ChaosBusyError("another scenario is running")
    async with _lock:
        merged = {**sc.params, **{k: v for k, v in (params or {}).items() if k in sc.params}}
        async with read_session() as s:
            business = await s.get(Business, business_id)
        assert business is not None
        seq = await injection_count(business_id) + 1
        async with write_session() as s:
            row = ChaosInjection(business_id=business_id, scenario=scenario, injected_by=user_id, parameters=jsonable(merged),
                                 status="running")
            s.add(row)
            await s.flush()
            inj_id = row.id
            add_audit(s, "chaos_injected", business_id=business_id, user_id=user_id,
                      inputs={"injection_id": inj_id, "scenario": scenario, "parameters": merged})
        broker.publish("harness", {"kind": "chaos.injected", "business_id": str(business_id), "injection_id": inj_id,
                                   "scenario": scenario})
        ctx = Ctx(business=business, injection_id=inj_id, params=merged, seq=seq)
        rec = _Recorder()
        context.set_active(inj_id)
        error = None
        try:
            await sc.inject(ctx)
        except ChaosError as exc:
            error = str(exc)
        except Exception as exc:  # a failed injection is reported, never left "running"
            log.exception("chaos scenario %s failed", scenario)
            error = f"{type(exc).__name__}: {exc}"
        finally:
            context.set_active(None)
            await rec.stop()
        outcome = await _outcome(inj_id, rec, ctx, sc)
        async with write_session() as s:
            done = await s.get(ChaosInjection, inj_id)
            assert done is not None
            done.status = "failed" if error else "completed"
            done.error = error[:500] if error else None
            done.affected_refs = jsonable(ctx.affected)
            done.detected = outcome["detected"]
            done.incident_id = uuid.UUID(outcome["incident_id"]) if outcome["incident_id"] else None
            done.rule_id = uuid.UUID(outcome["rule_id"]) if outcome["rule_id"] else None
            done.elapsed_seconds = outcome["elapsed_seconds"]
            done.outcome = jsonable(outcome)
        broker.publish("harness", {"kind": "chaos.completed", "business_id": str(business_id), "injection_id": inj_id,
                                   "scenario": scenario, "status": "failed" if error else "completed", "error": error,
                                   "outcome": outcome})
        return await get(business_id, inj_id)  # type: ignore[return-value]


async def _outcome(inj_id: uuid.UUID, rec: _Recorder, ctx: Ctx, sc: Scenario) -> dict[str, Any]:
    # Only the incidents this fault should cause count; a clock advance can surface unrelated ones.
    incidents = await linked_incidents(inj_id, sc.incident_types)
    inc_ids = {str(i.id) for i in incidents}
    async with read_session() as s:
        rules = list((await s.execute(select(LearnedRule).where(LearnedRule.source_incident_id.in_([i.id for i in incidents]))
                                      .order_by(LearnedRule.created_at))).scalars()) if incidents else []
    t_detect = rec.first(lambda ch, m: ch == "harness" and m.get("kind") == "incident.opened" and str(m.get("incident_id")) in inc_ids)
    if t_detect is None and incidents:
        t_detect = 0.0  # re-triggered an incident that was already open
    sent = [(t, str(msg["request"]["id"])) for t, ch, msg in rec.seen
            if ch == "chat" and isinstance(msg.get("request"), dict) and msg["request"].get("id")]
    async with read_session() as s:
        requests = list((await s.execute(select(ApprovalRequest).where(
            ApprovalRequest.id.in_([uuid.UUID(r) for _, r in sent])))).scalars()) if sent else []
    # Owner messages about this fault: they point at the incident, its action or a record the injector touched.
    known = set(inc_ids) | {str(i.action_id) for i in incidents if i.action_id}
    known |= {str(v) for i in incidents for v in i.refs.values() if isinstance(v, str | int) and v}
    known |= {str(v) for v in ctx.affected.values() if isinstance(v, str) and v}
    by_id = {str(r.id): r for r in requests}

    def related(r: ApprovalRequest) -> bool:
        return str(r.action_id) in known or any(str(v) in known for v in (r.context or {}).values())

    first_sent: dict[str, float] = {}
    for t, r in sent:
        first_sent.setdefault(r, t)
    about = [(t, by_id[r]) for r, t in first_sent.items() if r in by_id and related(by_id[r])]
    # The explanation is a message sent once the fault was detected (earlier ones, e.g. about the
    # original invoice, explain nothing).
    after = [(t, m) for t, m in about if t_detect is not None and t >= t_detect]
    messages = [m for _, m in after]
    primary = incidents[0] if incidents else None
    rule = rules[0] if rules else None

    t_explain = after[0][0] if after else None
    t_rule = rec.first(lambda ch, m: ch == "harness" and m.get("kind") == "rule.proposed"
                       and rule is not None and str(m.get("rule_id")) == str(rule.id))
    elapsed = round(t_rule if t_rule is not None else rec.elapsed(), 3)
    explanation_en = messages[0].text_en if messages else (primary.root_cause if primary else None)
    explanation_ar = messages[0].text_ar if messages else None
    timeline = [{"stage": "injected", "at_seconds": 0.0, "text_en": sc.title_en}]
    if primary is not None:
        timeline.append({"stage": "detected", "at_seconds": _r(t_detect), "text_en": f"{primary.detected_by}: {primary.summary}"})
        if explanation_en:
            timeline.append({"stage": "explained", "at_seconds": _r(t_explain if t_explain is not None else t_detect),
                             "text_en": explanation_en})
        if primary.action_taken:
            timeline.append({"stage": "corrected", "at_seconds": _r(t_detect), "text_en": primary.action_taken})
    if rule is not None:
        timeline.append({"stage": "rule_proposed", "at_seconds": _r(t_rule), "text_en": rule.rule_text_en})
    return {
        "detected": primary is not None,
        "incident_id": str(primary.id) if primary else None,
        "incident_ids": sorted(inc_ids),
        "detection_method": primary.detected_by if primary else None,
        "incident_summary": primary.summary if primary else None,
        "root_cause": primary.root_cause if primary else None,
        "explanation_en": explanation_en,
        "explanation_ar": explanation_ar,
        "correction": primary.action_taken if primary else None,
        "rule_id": str(rule.id) if rule else None,
        "rule_text_en": rule.rule_text_en if rule else None,
        "rule_text_ar": rule.rule_text_ar if rule else None,
        "rule_status": rule.status if rule else None,
        "owner_messages": [{"id": str(m.id), "kind": m.kind, "text_en": m.text_en, "text_ar": m.text_ar} for m in messages],
        # Every question to the owner about the affected records, detected or not (an applied rule asks none).
        "questions_asked": sum(1 for _, m in about if m.kind in ("question", "approval")),
        "elapsed_seconds": elapsed,
        "within_two_minutes": elapsed < 120,
        "business_date": clock.today().isoformat(),
        "timeline": timeline,
        "evidence": ctx.evidence,
    }


def _r(t: float | None) -> float | None:
    return round(t, 3) if t is not None else None


def to_json(row: ChaosInjection) -> dict[str, Any]:
    sc = REGISTRY.get(row.scenario)
    return {"id": row.id, "scenario": row.scenario, "number": sc.number if sc else None,
            "title_en": sc.title_en if sc else row.scenario, "title_ar": sc.title_ar if sc else row.scenario,
            "status": row.status, "injected_by": row.injected_by, "injected_at": row.injected_at,
            "parameters": row.parameters, "affected_refs": row.affected_refs, "detected": row.detected,
            "incident_id": row.incident_id, "rule_id": row.rule_id, "elapsed_seconds": row.elapsed_seconds,
            "outcome": row.outcome, "error": row.error}


async def get(business_id: uuid.UUID, injection_id: uuid.UUID) -> ChaosInjection | None:
    async with read_session() as s:
        row = await s.get(ChaosInjection, injection_id)
    return row if row is not None and row.business_id == business_id else None


async def recent(business_id: uuid.UUID, limit: int = 20) -> list[ChaosInjection]:
    async with read_session() as s:
        return list((await s.execute(select(ChaosInjection).where(ChaosInjection.business_id == business_id)
                                     .order_by(ChaosInjection.created_at.desc()).limit(limit))).scalars())
