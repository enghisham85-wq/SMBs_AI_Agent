"""Self-calibration: each agent tracks its own error rate.

A breached threshold degrades the agent (stricter confidence, lower auto-approve limit). Each healthy
day then moves 25% of the way back, so four in a row fully restore it.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select

from app.db.engine import write_session
from app.graphs.streaming import broker
from app.models.harness import AgentCalibration, AgentCalibrationHistory

NORMAL_HIGH = 0.90
NORMAL_LOW = 0.60
DEGRADED_HIGH = 0.97
DEGRADED_LOW = 0.80
DEGRADED_AUTO_FACTOR = 0.0
RESTORE_STEP = 0.25


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _apply_progress(row: AgentCalibration) -> None:
    t = row.restore_progress
    row.high_confidence_threshold = round(_lerp(DEGRADED_HIGH, NORMAL_HIGH, t), 4)
    row.low_confidence_threshold = round(_lerp(DEGRADED_LOW, NORMAL_LOW, t), 4)
    row.auto_approve_factor = round(_lerp(DEGRADED_AUTO_FACTOR, 1.0, t), 4)


METRIC_NAMES = {
    "forecast_mape": "forecast error", "forecast_variance": "cash forecast variance",
    "action_failure_rate": "share of actions that failed verification or were escalated",
}


async def record(
    business_id: uuid.UUID, agent: str, metric: str, value: float, d: date, threshold: float,
    safe_method: str | None = None, *, cause: str | None = None, open_incident: bool = True,
) -> dict[str, object]:
    """Record one daily value. Returns {"state": ok|degraded|recovering|restored, ...}."""
    async with write_session() as s:
        row = (await s.execute(select(AgentCalibration).where(
            AgentCalibration.business_id == business_id, AgentCalibration.agent == agent,
            AgentCalibration.metric == metric))).scalar_one_or_none()
        if row is None:
            row = AgentCalibration(business_id=business_id, agent=agent, metric=metric, threshold=threshold,
                                   high_confidence_threshold=NORMAL_HIGH, low_confidence_threshold=NORMAL_LOW,
                                   restore_progress=1.0, healthy_streak=0, degraded=False, auto_approve_factor=1.0,
                                   window_days=7)
            s.add(row)
        row.threshold = threshold
        row.current_value = value
        healthy = value <= threshold
        state = "ok"
        today_row = (await s.execute(select(AgentCalibrationHistory).where(
            AgentCalibrationHistory.business_id == business_id, AgentCalibrationHistory.agent == agent,
            AgentCalibrationHistory.metric == metric, AgentCalibrationHistory.date == d))).scalars().first()
        if today_row is not None and (row.degraded or healthy):
            # one step per business day; a repeat just updates today's value
            today_row.value = value
            return {"state": "degraded" if row.degraded else "ok", "value": value, "threshold": threshold,
                    "degraded": row.degraded, "high": row.high_confidence_threshold,
                    "auto_approve_factor": row.auto_approve_factor, "method_override": row.method_override}
        if not healthy:
            if not row.degraded:
                state = "degraded"
                row.degraded_since = d
            else:
                state = "still_degraded"
            row.degraded = True
            row.healthy_streak = 0
            # restart from the partly restored values
            row.restore_progress = 0.0 if state == "degraded" else row.restore_progress
            row.method_override = safe_method
            _apply_progress(row)
        elif row.degraded:
            row.healthy_streak += 1
            row.restore_progress = min(1.0, row.restore_progress + RESTORE_STEP)
            _apply_progress(row)
            state = "recovering"
            if row.restore_progress >= 1.0:
                row.degraded = False
                row.degraded_since = None
                row.method_override = None
                row.healthy_streak = 0
                state = "restored"
        if today_row is None:
            s.add(AgentCalibrationHistory(business_id=business_id, agent=agent, metric=metric, date=d, value=value,
                                          threshold=threshold, degraded=row.degraded,
                                          high_confidence_threshold=row.high_confidence_threshold))
        else:
            today_row.value, today_row.degraded = value, row.degraded
            today_row.high_confidence_threshold = row.high_confidence_threshold
        out = {"state": state, "value": value, "threshold": threshold, "degraded": row.degraded,
               "high": row.high_confidence_threshold, "auto_approve_factor": row.auto_approve_factor,
               "method_override": row.method_override}
    broker.publish("harness", {"kind": "calibration", "agent": agent, "metric": metric, "business_id": str(business_id),
                               **out})
    if state == "degraded" and open_incident:
        await _degraded_incident(business_id, agent, metric, value, threshold, d, cause, safe_method)
    return out


async def _degraded_incident(business_id: uuid.UUID, agent: str, metric: str, value: float, threshold: float, d: date,
                             cause: str | None, safe_method: str | None) -> None:
    from app.approvals import service as approvals
    from app.core.i18n import AGENT_NAMES
    from app.harness.incidents import open_incident as open_inc

    name = METRIC_NAMES.get(metric, metric)
    suspected = cause or "recent data differs from the pattern the agent learned"
    summary = (f"{AGENT_NAMES[agent]['en']}: {name} is {value:.0%}, above its {threshold:.0%} limit. "
               f"Suspected cause: {suspected}.")
    taken = "raised confidence thresholds, turned off auto-approval" + (f", switched to {safe_method}" if safe_method else "")
    key = f"calibration:{agent}:{metric}:{d.isoformat()}"
    inc = await open_inc(business_id=business_id, agent=agent, type="calibration_degraded", detected_by="self_calibration",
                         summary=summary, refs={"metric": metric, "value": value, "threshold": threshold,
                                                "suspected_cause": suspected}, dedupe_key=key, action_taken=taken)
    await approvals.post_alert(
        business_id=business_id, agent=agent, urgency=2, dedupe_key=key, context={"incident_id": str(inc)},
        text_en=f"{summary} I {taken} until accuracy recovers.",
        text_ar=f"{AGENT_NAMES[agent]['ar']}: تجاوز مؤشر {metric} الحد ({value:.0%} مقابل {threshold:.0%}). "
                f"السبب المحتمل: {suspected}. رفعت حدود الثقة وأوقفت الموافقة التلقائية حتى تتحسن الدقة.")


async def day_failure_rate(business_id: uuid.UUID, agent: str, d: date) -> tuple[float, int]:
    """(share of the day's finished actions that escalated or rolled back, number finished)."""
    from datetime import datetime, time, timedelta

    from app.db.engine import read_session
    from app.models.harness import Action, AuditLogEntry

    start, end = datetime.combine(d, time.min), datetime.combine(d + timedelta(days=1), time.min)
    async with read_session() as s:
        acts = (await s.execute(select(Action).where(Action.business_id == business_id, Action.agent == agent,
                                                     Action.created_at >= start, Action.created_at < end,
                                                     Action.dry_run.is_(False)))).scalars().all()
        ids = [a.id for a in acts]
        rolled = set((await s.execute(select(AuditLogEntry.action_id).where(
            AuditLogEntry.action_id.in_(ids), AuditLogEntry.event == "stage:rollback"))).scalars()) if ids else set()
    done = [a for a in acts if a.stage in ("completed", "escalated", "rejected", "failed")]
    bad = [a for a in done if a.stage in ("escalated", "failed") or a.id in rolled]
    return (len(bad) / len(done) if done else 0.0), len(done)


MIN_ACTIONS = 5


async def observe(business_id: uuid.UUID, agent: str, d: date) -> dict[str, object] | None:
    """Degrade on the spot if the failure rate breaks its limit. Recovery only happens at day close."""
    from app.core import settings_store
    from app.db.engine import read_session

    rate, n = await day_failure_rate(business_id, agent, d)
    if n < MIN_ACTIONS:
        return None
    threshold = float(await settings_store.get(business_id, "action_failure_threshold"))
    async with read_session() as s:
        row = (await s.execute(select(AgentCalibration).where(
            AgentCalibration.business_id == business_id, AgentCalibration.agent == agent,
            AgentCalibration.metric == "action_failure_rate"))).scalar_one_or_none()
    if rate <= threshold or (row is not None and row.degraded):
        return None
    return await record(business_id, agent, "action_failure_rate", rate, d, threshold,
                        cause="several of its actions failed verification today")


async def close_day(business_id: uuid.UUID, d: date) -> dict[str, object]:
    from app.core import settings_store

    threshold = float(await settings_store.get(business_id, "action_failure_threshold"))
    out: dict[str, object] = {}
    for agent in ("stock", "cashflow", "accountant"):
        rate, n = await day_failure_rate(business_id, agent, d)
        if n == 0:
            continue
        out[agent] = await record(business_id, agent, "action_failure_rate", rate if n >= MIN_ACTIONS else 0.0, d,
                                  threshold, cause="several of its actions failed verification today")
    return out


async def _after_action(spec: object, state: dict[str, object]) -> None:
    """FINALIZE hook."""
    from app.core import clock

    agent = getattr(spec, "agent", None)
    if agent in ("stock", "cashflow", "accountant") and not state.get("dry_run"):
        await observe(uuid.UUID(str(state["business_id"])), str(agent), clock.today())


def register() -> None:
    from app.harness.graph import FINALIZE_HOOKS

    if _after_action not in FINALIZE_HOOKS:
        FINALIZE_HOOKS.append(_after_action)  # type: ignore[arg-type]


async def auto_approve_factor(business_id: uuid.UUID, agent: str) -> float:
    """Multiplier applied to an agent's auto-approve limits (0 while degraded)."""
    from app.db.engine import read_session

    async with read_session() as s:
        rows = (await s.execute(select(AgentCalibration).where(
            AgentCalibration.business_id == business_id, AgentCalibration.agent == agent))).scalars().all()
    return min((r.auto_approve_factor for r in rows), default=1.0)
