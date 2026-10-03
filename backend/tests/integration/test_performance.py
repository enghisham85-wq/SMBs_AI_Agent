"""Performance targets: one simulated day of jobs < 10 s excluding LLM
calls (tests run the LLM offline), and the dashboard's main views respond in < 2 s on the sample cafe."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app import wiring
from app.config import get_settings
from app.core import scheduler
from app.seed.sample_cafe import PASSWORDS, seed

START = date(2026, 10, 4)
VIEWS = ["/api/v1/home", "/api/v1/stock/items", "/api/v1/purchase-orders", "/api/v1/cash/position",
         "/api/v1/cash/forecast", "/api/v1/cash/forecast?horizon=13w", "/api/v1/cash/shortfall-plan",
         "/api/v1/reconciliation", "/api/v1/review-queue", "/api/v1/reports/pnl", "/api/v1/harness/actions",
         "/api/v1/harness/incidents", "/api/v1/harness/calibration", "/api/v1/approvals"]


@pytest.fixture
async def cafe(api: Any, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(get_settings(), "FILES_DIR", str(tmp_path / "files"))
    wiring.register_all()
    return await seed(start_date=START, history_days=90)  # the full 3-month sample


async def test_one_day_advance_under_10_seconds(cafe: dict[str, Any]) -> None:
    await scheduler.advance(cafe["business_id"], days=1)  # first day builds forecasts from scratch
    t0 = time.perf_counter()
    await scheduler.advance(cafe["business_id"], days=1)
    elapsed = time.perf_counter() - t0
    assert elapsed < 10, f"one simulated day took {elapsed:.1f} s"


async def test_main_views_respond_under_2_seconds(api: Any, cafe: dict[str, Any]) -> None:
    await scheduler.advance(cafe["business_id"], days=1)
    await api.login("owner", PASSWORDS["owner"])
    slow = {}
    for url in VIEWS:
        t0 = time.perf_counter()
        r = await api.client.get(url)
        elapsed = time.perf_counter() - t0
        assert r.status_code == 200, f"{url}: {r.status_code}"
        if elapsed >= 2:
            slow[url] = round(elapsed, 2)
    assert slow == {}, f"views slower than 2 s: {slow}"
