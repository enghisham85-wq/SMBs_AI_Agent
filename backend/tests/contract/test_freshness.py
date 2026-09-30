"""FR-046 contract (T122): every manager-level GET endpoint that returns figures states their freshness.

Walks the app's routes (from its OpenAPI document), so a new endpoint is covered without editing this test. Endpoints with path
parameters get an id taken from the matching list endpoint.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app import wiring
from app.config import get_settings
from app.core import scheduler
from app.core.freshness import has_figures, missing_freshness
from app.seed.sample_cafe import PASSWORDS, seed

START = date(2026, 10, 4)
SKIP = {"/api/v1/harness/stream", "/api/v1/chat/stream", "/api/v1/files/{file_id}", "/api/v1/openapi.json"}
# path -> (list endpoint, key of the list, field holding the id)
IDS: dict[str, tuple[str, str, str]] = {
    "/api/v1/documents/{doc_id}": ("/api/v1/documents", "documents", "id"),
    "/api/v1/receivables/{rec_id}": ("/api/v1/receivables", "receivables", "id"),
    "/api/v1/stock/items/{item_id}/forecast": ("/api/v1/stock/items", "items", "id"),
    "/api/v1/harness/actions/{action_id}": ("/api/v1/harness/actions", "actions", "id"),
}


@pytest.fixture
async def cafe(api: Any, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(get_settings(), "FILES_DIR", str(tmp_path / "files"))
    wiring.register_all()
    info = await seed(start_date=START, history_days=42)
    await scheduler.advance(info["business_id"], days=1)  # forecasts, reconciliation, approvals exist
    await api.login("owner", PASSWORDS["owner"])
    files = {f["name"]: f for f in json.loads((tmp_path / "samples" / "files.json").read_text())}
    f = files["en_coffee"]
    r = await api.client.post("/api/v1/documents", files={"file": (f["file"], (tmp_path / "samples" / f["file"]).read_bytes(), f["mime"])})
    assert r.status_code == 202, r.text
    await api.login("manager", PASSWORDS["manager"])
    return info


def _get_routes(app: Any) -> list[str]:
    return sorted(p for p, ops in app.openapi()["paths"].items() if "get" in ops and p not in SKIP)


async def test_every_manager_get_with_figures_has_data_as_of(api: Any, cafe: dict[str, Any]) -> None:
    checked, with_figures, missing = [], [], []
    for path in _get_routes(api.app):
        url = path
        if "{" in path:
            if path not in IDS:
                continue
            list_url, key, field = IDS[path]
            rows = (await api.client.get(list_url)).json().get(key) or []
            if not rows:
                continue
            url = path.split("{")[0] + str(rows[0][field]) + (path.split("}", 1)[1] if "}" in path else "")
        r = await api.client.get(url)
        if r.status_code in (401, 403, 404, 422):  # owner-only, or needs parameters
            continue
        body = r.json()
        if r.status_code == 500 and body.get("error", {}).get("code") == "missing_data_as_of":
            missing.append(url)
            continue
        assert r.status_code == 200, f"{url}: {r.status_code} {r.text[:300]}"
        checked.append(url)
        if missing_freshness(body):
            missing.append(url)
        if has_figures(body):
            with_figures.append(url)
    assert not missing, f"figures without data_as_of: {missing}"
    assert "/api/v1/home" in with_figures and "/api/v1/cash/forecast" in with_figures
    assert len(checked) >= 15


async def test_home_health_strip_decisions_and_alerts(api: Any, cafe: dict[str, Any]) -> None:
    body = (await api.client.get("/api/v1/home")).json()
    h = body["health"]
    assert h["stock"]["items"] == h["stock"]["ok"] + h["stock"]["warnings"]
    assert h["cash"]["lowest"]["date"] and h["cash"]["lowest"]["balance"]["currency"] == "EGP"
    assert 0 <= h["books"]["percent_reconciled"] <= 100 and h["books"]["review_count"] >= 0
    for part in ("stock", "cash", "books"):
        assert h[part]["data_as_of"]
    assert set(body["data_as_of"]) >= {"stock", "forecast", "bank"}
    urg = [d["urgency"] for d in body["decisions"]]
    assert urg == sorted(urg, reverse=True) and all(d["kind"] != "alert" for d in body["decisions"])
    assert [a["urgency"] for a in body["alerts"]] == sorted((a["urgency"] for a in body["alerts"]), reverse=True)
    assert all(d["required_role"] != "owner" for d in body["decisions"])  # the caller is a manager
    await api.login("staff", PASSWORDS["staff"])
    assert (await api.client.get("/api/v1/home")).status_code == 403


def test_guard_flags_figures_without_freshness() -> None:
    money = {"amount_minor": 100, "currency": "EGP", "decimals": 2}
    assert missing_freshness({"rows": [{"total": money}]})
    assert not missing_freshness({"rows": [{"total": money}], "data_as_of": {"books": None}})
    assert not missing_freshness({"users": [{"name": "x"}]})
