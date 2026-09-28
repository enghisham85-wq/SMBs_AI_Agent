"""Self-calibration (FR-006): each agent tracks its own error rate.

When a metric breaks its threshold the agent is degraded: confidence thresholds go up, the
auto-approve limit goes down, it falls back to a safer method, and the owner is told the
suspected cause. Recovery is gradual: each consecutive healthy business day (metric within
threshold) moves settings 25 % of the way back; 4 healthy days in a row fully restore them.
An unhealthy day during recovery restarts the count from the current, partly restored values.
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


async def record(
    business_id: uuid.UUID, agent: str, metric: str, value: float, d: date, threshold: float,
    safe_method: str | None = None,
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
        if not healthy:
            if not row.degraded:
                state = "degraded"
                row.degraded_since = d
            else:
                state = "still_degraded"
            row.degraded = True
            row.healthy_streak = 0
            # Restart from the current (possibly partly restored) values, no worse than degraded.
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
        s.add(AgentCalibrationHistory(business_id=business_id, agent=agent, metric=metric, date=d, value=value,
                                      threshold=threshold, degraded=row.degraded,
                                      high_confidence_threshold=row.high_confidence_threshold))
        out = {"state": state, "value": value, "threshold": threshold, "degraded": row.degraded,
               "high": row.high_confidence_threshold, "auto_approve_factor": row.auto_approve_factor,
               "method_override": row.method_override}
    broker.publish("harness", {"kind": "calibration", "agent": agent, "metric": metric, **out})
    return out


async def auto_approve_factor(business_id: uuid.UUID, agent: str) -> float:
    """Multiplier applied to an agent's auto-approve limits (0 while degraded)."""
    from app.db.engine import read_session

    async with read_session() as s:
        rows = (await s.execute(select(AgentCalibration).where(
            AgentCalibration.business_id == business_id, AgentCalibration.agent == agent))).scalars().all()
    return min((r.auto_approve_factor for r in rows), default=1.0)
