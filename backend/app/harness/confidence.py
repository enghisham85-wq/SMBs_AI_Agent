"""Three-band confidence routing. Middle-band items are acted on and flagged for the daily digest."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy import select

from app.db.engine import read_session, write_session
from app.harness.audit import add_audit
from app.models.harness import AgentCalibration

Band = Literal["act", "act_flag", "ask"]
DEFAULT_HIGH = 0.90
DEFAULT_LOW = 0.60


async def thresholds(business_id: uuid.UUID, agent: str) -> tuple[float, float]:
    """(high, low) for an agent. Calibration can only make them stricter."""
    from app.core import settings_store

    cfg = await settings_store.get_all(business_id)
    high = float(cfg.get("confidence_high", DEFAULT_HIGH))
    low = float(cfg.get("confidence_low", DEFAULT_LOW))
    async with read_session() as s:
        rows = (await s.execute(select(AgentCalibration).where(
            AgentCalibration.business_id == business_id, AgentCalibration.agent == agent))).scalars().all()
    for r in rows:
        if r.degraded or r.restore_progress < 1.0:
            high = max(high, r.high_confidence_threshold)
            low = max(low, r.low_confidence_threshold)
    return high, low


def band_for(confidence: float, high: float, low: float) -> Band:
    if confidence >= high:
        return "act"
    if confidence >= low:
        return "act_flag"
    return "ask"


async def band(business_id: uuid.UUID, agent: str, confidence: float) -> Band:
    high, low = await thresholds(business_id, agent)
    return band_for(confidence, high, low)


async def flag(business_id: uuid.UUID, agent: str, text_en: str, text_ar: str, confidence: float,
               refs: dict[str, Any] | None = None, action_id: uuid.UUID | None = None) -> None:
    """Record a middle-band item for the daily digest."""
    async with write_session() as s:
        add_audit(s, "act_flag", business_id=business_id, agent=agent, action_id=action_id,
                  inputs={"text_en": text_en, "text_ar": text_ar, "confidence": confidence, "refs": refs or {}})


async def route(business_id: uuid.UUID, agent: str, confidence: float, text_en: str, text_ar: str,
                refs: dict[str, Any] | None = None, action_id: uuid.UUID | None = None) -> Band:
    b = await band(business_id, agent, confidence)
    if b == "act_flag":
        await flag(business_id, agent, text_en, text_ar, confidence, refs, action_id)
    return b
