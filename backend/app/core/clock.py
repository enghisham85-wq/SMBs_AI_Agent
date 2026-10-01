"""BusinessClock: the only source of "now" (FR-012a, research R10).

In demo mode the date is simulated and moved forward by the presenter; outside demo mode it is
the real calendar date. The MVP runs one business per process, so the current clock state is
kept in-process and mirrored in the `business_clock` table.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

_state: dict[str, object] = {"mode": "real", "date": None}


class ClockBusyError(RuntimeError):
    """An advance is already running."""


class ClockError(ValueError):
    pass


def set_state(mode: str, current: date | None) -> None:
    _state["mode"] = mode
    _state["date"] = current


def is_simulated() -> bool:
    return _state["mode"] == "simulated" and _state["date"] is not None


def today() -> date:
    if is_simulated():
        d = _state["date"]
        assert isinstance(d, date)
        return d
    return date.today()


def clock_now() -> datetime:
    """Business-clock datetime: the simulated date with the current wall time of day."""
    if is_simulated():
        return datetime.combine(today(), datetime.now().time().replace(microsecond=0))
    return datetime.now().replace(microsecond=0)


def start_of_business_day(d: date | None = None) -> datetime:
    return datetime.combine(d or today(), time(8, 0))


async def load(business_id: uuid.UUID) -> None:
    """Load the persisted clock for a business into the in-process state."""
    from app.db.engine import read_session
    from app.models.clock import BusinessClock

    async with read_session() as s:
        row = (await s.execute(select(BusinessClock).where(BusinessClock.business_id == business_id))).scalar_one_or_none()
    if row is None:
        set_state("real", None)
    else:
        set_state(row.mode, row.current_date if row.mode == "simulated" else None)


async def clear_stale_advance(business_id: uuid.UUID) -> bool:
    """At startup no advance can be running, so a set `advancing` flag was left by a crash mid-advance;
    clear it, or every later advance would be refused as busy. Returns whether a flag was cleared."""
    from app.db.engine import read_session, write_session
    from app.models.clock import BusinessClock

    query = select(BusinessClock).where(BusinessClock.business_id == business_id, BusinessClock.advancing.is_(True))
    async with read_session() as s:
        if (await s.execute(query)).scalar_one_or_none() is None:
            return False
    async with write_session() as s:
        for row in (await s.execute(query)).scalars():
            row.advancing = False
    return True


DayCallback = Callable[[date], Awaitable[None]]


async def advance(
    business_id: uuid.UUID,
    per_day: DayCallback,
    *,
    days: int | None = None,
    to_date: date | None = None,
) -> list[date]:
    """Move the simulated clock forward, running `per_day` once for every date in order.

    Every date from `last_run_date + 1` up to the target is processed, so a jump never skips
    scheduled work (forecast checks, reminders, approval timeouts ...).
    """
    from app.db.engine import write_session
    from app.models.clock import BusinessClock

    if (days is None) == (to_date is None):
        raise ClockError("give exactly one of days or to_date")

    async with write_session() as s:
        row = (await s.execute(select(BusinessClock).where(BusinessClock.business_id == business_id))).scalar_one()
        if row.mode != "simulated":
            raise ClockError("the clock can only be advanced in demo (simulated) mode")
        if row.advancing:
            raise ClockBusyError("an advance is already running")
        current = row.current_date
        target = current + timedelta(days=days) if days is not None else to_date
        assert target is not None
        if target <= current:
            raise ClockError("target date must be after the current business date")
        row.advancing = True
        start = (row.last_run_date or current) + timedelta(days=1)

    processed: list[date] = []
    try:
        d = start
        while d <= target:
            async with write_session() as s:
                row = (await s.execute(select(BusinessClock).where(BusinessClock.business_id == business_id))).scalar_one()
                row.current_date = d
            set_state("simulated", d)
            await per_day(d)
            async with write_session() as s:
                row = (await s.execute(select(BusinessClock).where(BusinessClock.business_id == business_id))).scalar_one()
                row.last_run_date = d
            processed.append(d)
            d += timedelta(days=1)
    finally:
        async with write_session() as s:
            row = (await s.execute(select(BusinessClock).where(BusinessClock.business_id == business_id))).scalar_one()
            row.advancing = False
    return processed
