"""Moves the business clock and runs each day's work (FR-012a).

Demo mode: the presenter advances the simulated clock; every skipped date runs daily_run_graph
in order. Real mode: a background loop runs the same graph once per real day.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date

from sqlalchemy import select

from app.core import clock
from app.db.engine import read_session, write_session
from app.graphs.daily_run import run_day
from app.models.clock import BusinessClock

log = logging.getLogger(__name__)


async def advance(business_id: uuid.UUID, *, days: int | None = None, to_date: date | None = None) -> list[date]:
    async def per_day(d: date) -> None:
        await run_day(business_id, d)

    return await clock.advance(business_id, per_day, days=days, to_date=to_date)


async def real_time_loop(business_id: uuid.UUID, interval_seconds: int = 3600) -> None:
    """Outside demo mode: run the daily graph once for each real day not yet processed."""
    while True:
        try:
            today = date.today()
            async with read_session() as s:
                row = (await s.execute(select(BusinessClock).where(BusinessClock.business_id == business_id))).scalar_one_or_none()
            if row is not None and row.mode == "real" and (row.last_run_date is None or row.last_run_date < today):
                await run_day(business_id, today)
                async with write_session() as s:
                    r = (await s.execute(select(BusinessClock).where(BusinessClock.business_id == business_id))).scalar_one()
                    r.last_run_date = today
                    r.current_date = today
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("real-time daily loop failed")
        await asyncio.sleep(interval_seconds)
