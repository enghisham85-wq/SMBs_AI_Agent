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


# A day's work normally takes seconds to minutes; past this it is treated as hung and retried.
RUN_DAY_TIMEOUT_S = 30 * 60
# The owner is told once a day once this many attempts in a row have failed (the loop retries hourly).
ALERT_AFTER_FAILURES = 3


async def _report_stuck(business_id: uuid.UUID, today: date, failures: int) -> None:
    from app.approvals import service as approvals
    from app.harness.audit import audit

    try:
        await audit("daily_run_failing", business_id=business_id, inputs={"date": today},
                    outputs={"consecutive_failures": failures})
        await approvals.post_alert(
            business_id=business_id, agent="harness", urgency=2, dedupe_key=f"daily_run_failing:{today.isoformat()}",
            text_en=f"Today's automatic checks have failed {failures} times in a row and will keep retrying every hour.",
            text_ar=f"فشلت المراجعات التلقائية لليوم {failures} مرات متتالية، وستتم إعادة المحاولة كل ساعة.",
            context={"date": today.isoformat(), "consecutive_failures": failures})
    except Exception:  # reporting must never take the loop down
        log.exception("could not report the failing daily run")


async def real_time_loop(business_id: uuid.UUID, interval_seconds: int = 3600) -> None:
    """Outside demo mode: run the daily graph once for each real day not yet processed.

    A failed day is retried on the next tick with a fresh graph thread, so all of its steps run again.
    """
    failures = 0
    reported_for: date | None = None
    while True:
        today = date.today()
        try:
            async with read_session() as s:
                row = (await s.execute(select(BusinessClock).where(BusinessClock.business_id == business_id))).scalar_one_or_none()
            if row is not None and row.mode == "real" and (row.last_run_date is None or row.last_run_date < today):
                async with asyncio.timeout(RUN_DAY_TIMEOUT_S):
                    await run_day(business_id, today)
                async with write_session() as s:
                    r = (await s.execute(select(BusinessClock).where(BusinessClock.business_id == business_id))).scalar_one()
                    r.last_run_date = today
                    r.current_date = today
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:  # TimeoutError included
            failures += 1
            log.exception("real-time daily run failed for %s (%d in a row)", today, failures)
            if failures >= ALERT_AFTER_FAILURES and reported_for != today:
                reported_for = today
                await _report_stuck(business_id, today, failures)
        await asyncio.sleep(interval_seconds)
