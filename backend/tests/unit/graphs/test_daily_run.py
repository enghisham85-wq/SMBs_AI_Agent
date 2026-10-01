"""daily_run_graph: a step that runs past its budget is cancelled, recorded, and the day goes on."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select

from app.db.engine import read_session
from app.graphs import daily_run
from app.models.harness import AuditLogEntry


async def test_step_over_budget_is_recorded_and_the_rest_of_the_day_runs(business: dict[str, Any],
                                                                         monkeypatch: Any) -> None:
    ran: list[date] = []

    async def stuck(bid: uuid.UUID, d: date) -> None:
        await asyncio.sleep(30)

    async def digest(bid: uuid.UUID, d: date) -> None:
        ran.append(d)

    saved = {step: list(fns) for step, fns in daily_run._steps.items()}
    monkeypatch.setattr(daily_run, "STEP_BUDGET_S", 0.05)
    daily_run.clear_steps()
    daily_run.register_step("import_sales", "stuck", stuck)
    daily_run.register_step("digest", "digest", digest)
    daily_run.register()
    try:
        values = await daily_run.run_day(business["id"], date(2026, 10, 5))
    finally:
        for step, fns in saved.items():
            daily_run._steps[step][:] = fns

    assert ran == [date(2026, 10, 5)]
    assert values["done"] == list(daily_run.STEPS)
    [failure] = values["failures"]
    assert failure["step"] == "import_sales" and failure["name"] == "stuck"
    assert "longer than" in failure["error"]
    async with read_session() as s:
        events = [e.event for e in (await s.execute(select(AuditLogEntry).where(
            AuditLogEntry.business_id == business["id"]))).scalars()]
    assert "daily_step_failed" in events and "daily_run_completed" in events
