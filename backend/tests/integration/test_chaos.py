"""US6 acceptance (T113): each of the 8 Chaos scenarios is detected, explained, corrected and followed by
a proposed rule, in under 2 minutes (SC-001, SC-011); an approved date-format rule is then applied
without asking the owner."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app import wiring
from app.config import get_settings
from app.models.chaos import SCENARIOS
from app.seed.sample_cafe import PASSWORDS, seed

START = date(2026, 10, 4)


@pytest.fixture
async def cafe(api: Any, db: None, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(get_settings(), "FILES_DIR", str(tmp_path / "files"))
    wiring.register_all()
    info = await seed(start_date=START, history_days=42)
    await api.login("owner", PASSWORDS["owner"])
    return info


async def _inject(api: Any, scenario: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    r = await api.client.post("/api/v1/chaos/inject", json={"scenario": scenario, "params": params})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "completed", body["error"]
    return body


EVIDENCE = {
    "duplicate_invoice": lambda e: e["posted_copies"] == 1 and "duplicate" in e["duplicate_issues"],
    "price_spike": lambda e: e["po_status"] == "on_hold" and e["alternative_offered"],
    "date_format": lambda e: e["read_as"] == e["true_date"],
    "demand_spike": lambda e: e["forecast_method_after"] == "same_weekday_avg",
    "paid_before_reminder": lambda e: e["reminder_status"] == "cancelled_paid",
    "missing_bank_day": lambda e: bool(e["low_confidence_reason"]),
    "short_delivery": lambda e: e["invoice_status"] == "held" and "three_way" in e["issues"],
    "cash_crunch": lambda e: e["budget_tightened"] and 14 <= e["days_to_act"] <= 21,
}


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_scenario_detected_explained_corrected_and_rule_proposed(api: Any, cafe: dict[str, Any], scenario: str) -> None:
    body = await _inject(api, scenario)
    out = body["outcome"]
    assert body["detected"] is True and out["detected"] is True
    assert body["incident_id"] and out["detection_method"]
    assert out["explanation_en"] and out["correction"]
    assert body["rule_id"] and out["rule_text_en"] and out["rule_status"] == "proposed"
    assert body["elapsed_seconds"] is not None and body["elapsed_seconds"] < 120
    assert EVIDENCE[scenario](out["evidence"]), out["evidence"]
    stages = [t["stage"] for t in out["timeline"]]
    assert stages[0] == "injected" and {"detected", "explained", "corrected", "rule_proposed"} <= set(stages)

    got = (await api.client.get(f"/api/v1/chaos/injections/{body['id']}")).json()
    assert got["rule_id"] == body["rule_id"] and got["detected"] is True
    incidents = (await api.client.get("/api/v1/harness/incidents")).json()["incidents"]
    assert any(i["id"] == body["incident_id"] for i in incidents)


async def test_approved_date_rule_is_applied_without_asking(api: Any, cafe: dict[str, Any]) -> None:
    first = await _inject(api, "date_format")
    r = await api.client.post(f"/api/v1/harness/rules/{first['rule_id']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "active"

    again = await _inject(api, "date_format")
    out = again["outcome"]
    assert out["questions_asked"] == 0  # the supplier's format is known now: no question, no correction
    assert out["evidence"]["outcome"] == "posted" and out["evidence"]["read_as"] == out["evidence"]["true_date"]
    assert again["detected"] is False and again["rule_id"] is None


async def test_scenarios_listed_and_owner_only(api: Any, cafe: dict[str, Any]) -> None:
    body = (await api.client.get("/api/v1/chaos/scenarios")).json()
    assert [s["key"] for s in body["scenarios"]] == list(SCENARIOS)
    assert all(s["title_en"] and s["title_ar"] for s in body["scenarios"])
    assert (await api.client.post("/api/v1/chaos/inject", json={"scenario": "nope"})).status_code == 422
    await api.login("manager", PASSWORDS["manager"])
    assert (await api.client.get("/api/v1/chaos/scenarios")).status_code == 403
    assert (await api.client.post("/api/v1/chaos/inject", json={"scenario": "price_spike"})).status_code == 403


async def test_inject_refused_outside_demo_mode(api: Any, cafe: dict[str, Any], monkeypatch: Any) -> None:
    monkeypatch.setattr(get_settings(), "DEMO_MODE", False)
    r = await api.client.post("/api/v1/chaos/inject", json={"scenario": "price_spike"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "chaos_disabled"
