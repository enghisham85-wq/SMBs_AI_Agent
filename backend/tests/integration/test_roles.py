"""Roles on every screen, chat action and approval.

- staff record deliveries and waste, and never see money (403 on money endpoints, no money fields)
- a manager approves purchase orders but cannot approve rules, change settings or manage users
- a Telegram answer from a staff user is refused
- every refusal is written to the audit log
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

from app import wiring
from app.approvals import service as approvals
from app.approvals import telegram_bot
from app.core import clock
from app.core.freshness import has_figures
from app.db.engine import read_session, write_session
from app.harness import rules
from app.harness.graph import run_action
from app.models.harness import ApprovalRequest, AuditLogEntry
from app.models.master import Item, SupplierPrice
from app.models.purchasing import PurchaseOrder
from app.models.tenancy import User
from app.seed.sample_cafe import PASSWORDS, seed

START = date(2026, 10, 4)
MONEY_GETS = ["/api/v1/home", "/api/v1/cash/position", "/api/v1/cash/forecast", "/api/v1/cash/receivables",
              "/api/v1/cash/payables", "/api/v1/reconciliation", "/api/v1/review-queue", "/api/v1/documents",
              "/api/v1/receivables", "/api/v1/reports/pnl", "/api/v1/vat/summary", "/api/v1/suppliers",
              "/api/v1/obligations", "/api/v1/harness/actions", "/api/v1/audit-log", "/api/v1/settings"]
STAFF_GETS = ["/api/v1/stock/items", "/api/v1/stock/products", "/api/v1/purchase-orders", "/api/v1/approvals"]


@pytest.fixture
async def cafe(api: Any, tmp_path: Any, monkeypatch: Any) -> dict[str, Any]:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(get_settings(), "FILES_DIR", str(tmp_path / "files"))
    wiring.register_all()
    return await seed(start_date=START, history_days=42)


async def _denials(bid: uuid.UUID) -> int:
    async with read_session() as s:
        return int((await s.execute(select(func.count()).select_from(AuditLogEntry).where(
            AuditLogEntry.business_id == bid, AuditLogEntry.event == "permission_denied"))).scalar_one())


async def _user(bid: uuid.UUID, role: str) -> User:
    async with read_session() as s:
        return (await s.execute(select(User).where(User.business_id == bid, User.role == role))).scalar_one()


async def _po_approval(bid: uuid.UUID) -> ApprovalRequest:
    """A purchase order waiting for approval (drafted and sent through the harness)."""
    async with read_session() as s:
        milk = (await s.execute(select(Item).where(Item.business_id == bid, Item.name_en == "Milk"))).scalar_one()
        price = (await s.execute(select(SupplierPrice).where(SupplierPrice.item_id == milk.id))).scalars().first()
    assert price is not None
    drafted = await run_action("draft_po", {
        "supplier_id": str(milk.preferred_supplier_id), "expected_date": (clock.today() + timedelta(days=1)).isoformat(),
        "lines": [{"item_id": str(milk.id), "qty": "40", "unit": "L", "pack_size": "1",
                   "unit_price_minor": price.price.amount_minor}],
        "reason": "role test", "is_critical": True, "projected_stockout": (clock.today() + timedelta(days=3)).isoformat()}, bid)
    assert drafted["outcome"] == "completed", drafted
    sent = await run_action("send_po", {"po_id": drafted["result"]["po_id"]}, bid, parent_action_id=drafted["action_id"])
    assert sent["interrupted"]
    async with read_session() as s:
        return (await s.execute(select(ApprovalRequest).where(ApprovalRequest.action_id == sent["action_id"],
                                                              ApprovalRequest.status == "pending"))).scalar_one()


async def test_staff_record_deliveries_and_waste_but_never_see_money(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    req = await _po_approval(bid)
    owner = await _user(bid, "owner")
    assert (await approvals.resolve(str(req.id), "approve", owner, "dashboard")).status == "resolved"  # type: ignore[arg-type]
    await api.login("staff", PASSWORDS["staff"])
    before = await _denials(bid)

    # Allowed: stock lists without money, deliveries and waste.
    for url in STAFF_GETS:
        r = await api.client.get(url)
        assert r.status_code == 200, url
        assert not has_figures(r.json()), f"{url} shows money to staff"
        text = json.dumps(r.json())
        assert '"unit_cost"' not in text and '"sale_price"' not in text and '"total"' not in text, url
    items = (await api.client.get("/api/v1/stock/items")).json()["items"]
    milk = next(i for i in items if i["name_en"] == "Milk")
    r = await api.client.post("/api/v1/stock/waste", json={"item_id": milk["id"], "qty": "1", "reason": "spilled"})
    assert r.status_code == 201, r.text
    po_id = str(req.context["po_id"])
    r = await api.client.post(f"/api/v1/purchase-orders/{po_id}/deliveries",
                              data={"lines": json.dumps([{"item_id": milk["id"], "qty_received": "40"}])})
    assert r.status_code == 201, r.text
    async with read_session() as s:
        assert (await s.get(PurchaseOrder, uuid.UUID(po_id))).status == "received"  # type: ignore[union-attr]

    # Refused: every money view, and the owner/manager actions.
    refused = 0
    for url in MONEY_GETS:
        r = await api.client.get(url)
        assert r.status_code == 403, f"{url}: {r.status_code}"
        refused += 1
    for method, url, body in (("post", "/api/v1/stock/counts", {"counts": []}),
                              ("patch", "/api/v1/settings", {"values": {"price_change_pct": 20}}),
                              ("post", "/api/v1/users", {"username": "x", "password": "long-enough-1", "role": "staff"})):
        r = await getattr(api.client, method)(url, json=body)
        assert r.status_code == 403, f"{method} {url}: {r.status_code}"
        refused += 1
    assert await _denials(bid) - before == refused


async def test_manager_approves_orders_but_not_rules_settings_or_users(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    req = await _po_approval(bid)
    rid = await rules.propose(business_id=bid, agent="stock", kind="policy", rule_text_en="Ask before orders",
                              rule_text_ar="اسأل قبل الطلبات", trigger={"action_type": "send_po", "require_approval": True},
                              source_incident_id=None)
    await api.login("manager", PASSWORDS["manager"])
    r = await api.client.post(f"/api/v1/approvals/{req.id}/resolve", json={"option_key": "approve"})
    assert r.status_code == 200, r.text
    before = await _denials(bid)
    staff = await _user(bid, "staff")
    for method, url, body in (("post", f"/api/v1/harness/rules/{rid}/approve", None),
                              ("patch", f"/api/v1/harness/rules/{rid}", {"rule_text_en": "changed"}),
                              ("patch", "/api/v1/settings", {"values": {"price_change_pct": 20}}),
                              ("patch", "/api/v1/business", {"vat_rate_percent": 10}),
                              ("post", "/api/v1/users", {"username": "y", "password": "long-enough-1", "role": "staff"}),
                              ("patch", f"/api/v1/users/{staff.id}", {"role": "owner"}),
                              ("get", "/api/v1/audit-log", None)):
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(api.client, method)(url, **kwargs)
        assert r.status_code == 403, f"{method} {url}: {r.status_code} {r.text[:200]}"
    assert await _denials(bid) - before == 7
    async with read_session() as s:
        assert (await s.get(User, staff.id)).role == "staff"  # type: ignore[union-attr]


async def test_telegram_answer_from_staff_is_refused_and_logged(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    req = await _po_approval(bid)
    async with write_session() as s:
        staff = (await s.execute(select(User).where(User.business_id == bid, User.role == "staff"))).scalar_one()
        staff.telegram_chat_id = "777"
    answers: list[tuple[str, bool]] = []

    async def answer(text: str = "", show_alert: bool = False) -> None:
        answers.append((text, show_alert))

    update = SimpleNamespace(callback_query=SimpleNamespace(
        id=f"cb-{uuid.uuid4().hex}", data=f"ar:{req.request_token}:approve", answer=answer,
        message=SimpleNamespace(chat=SimpleNamespace(id=777))))
    before = await _denials(bid)
    await telegram_bot.on_callback(update, None)
    assert answers and answers[0][1] is True  # shown as an alert: not allowed
    async with read_session() as s:
        row = await s.get(ApprovalRequest, req.id)
    assert row is not None and row.status == "pending"
    assert await _denials(bid) - before == 1
