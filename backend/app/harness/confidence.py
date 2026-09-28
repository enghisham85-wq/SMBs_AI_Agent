"""Three-band confidence routing (FR-005). Thresholds come from AgentCalibration when present."""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy import select

from app.db.engine import read_session
from app.models.harness import AgentCalibration

Band = Literal["act", "act_flag", "ask"]
DEFAULT_HIGH = 0.90
DEFAULT_LOW = 0.60


async def thresholds(business_id: uuid.UUID, agent: str) -> tuple[float, float]:
    """(high, low) for an agent: the strictest across its calibration metrics."""
    async with read_session() as s:
        rows = (
            await s.execute(
                select(AgentCalibration).where(
                    AgentCalibration.business_id == business_id, AgentCalibration.agent == agent
                )
            )
        ).scalars().all()
    if not rows:
        return DEFAULT_HIGH, DEFAULT_LOW
    return max(r.high_confidence_threshold for r in rows), max(r.low_confidence_threshold for r in rows)


def band_for(confidence: float, high: float, low: float) -> Band:
    if confidence >= high:
        return "act"
    if confidence >= low:
        return "act_flag"
    return "ask"


async def band(business_id: uuid.UUID, agent: str, confidence: float) -> Band:
    high, low = await thresholds(business_id, agent)
    return band_for(confidence, high, low)
