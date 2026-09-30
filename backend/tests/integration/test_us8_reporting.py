"""US8 (T123-T126): VAT summary with its independent check and period reminder, 13-week scenarios,
supplier scorecard, and the language stored on the user."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app import wiring
from app.agents.accountant import posting, vat
from app.config import get_settings
from app.core import clock
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.models.books import PayableInvoice
from app.models.finance_master import BankAccount, BankTransaction
from app.models.harness import Action, ApprovalRequest
from app.models.master import Item, Supplier, SupplierPrice
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine
from app.seed.sample_cafe import PASSWORDS, seed

START = date(2026, 10, 4)


@pytest.fixture
async def cafe(api: Any, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(get_settings(), "FILES_DIR", str(tmp_path / "files"))
    wiring.register_all()
    info = await seed(start_date=START, history_days=42)
    info["samples"] = tmp_path / "samples"
    await api.login("owner", PASSWORDS["owner"])
    return info


async def _submit(api: Any, cafe: dict[str, Any], name: str) -> dict[str, Any]:
    files = {f["name"]: f for f in json.loads((cafe["samples"] / "files.json").read_text())}
    f = files[name]
    r = await api.client.post("/api/v1/documents", files={"file": (f["file"], (cafe["samples"] / f["file"]).read_bytes(), f["mime"])})
    assert r.status_code == 202, r.text
    return r.json()


async def _invoice(invoice_id: str) -> PayableInvoice:
    async with read_session() as s:
        inv = await s.get(PayableInvoice, uuid.UUID(invoice_id))
    assert inv is not None
    return inv


# ------------------------------------------------------------------ T123 VAT summary
async def test_vat_summary_totals_match_posted_invoices_and_flag_missing_vat_number(api: Any, cafe: dict[str, Any]) -> None:
    ok = await _submit(api, cafe, "en_coffee")
    bad = await _submit(api, cafe, "en_produce_missing_vat_number")
    assert ok["outcome"] == "posted"
    if bad["issues"]:  # e.g. which account the lemons go to: answer it like the owner would
        async with read_session() as s:
            q = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.status == "pending",
                                                               ApprovalRequest.graph_thread_id == f"document:{bad['document_id']}"))).scalar_one()
        r = await api.client.post(f"/api/v1/approvals/{q.id}/resolve", json={"option_key": q.options[0]["key"]})
        assert r.status_code == 200, r.text
    assert (await _invoice(bad["invoice_id"])).status == "posted"
    inv_ok, inv_bad = await _invoice(ok["invoice_id"]), await _invoice(bad["invoice_id"])
    bodies = {}
    for inv in (inv_ok, inv_bad):
        period = vat.period_for(inv.invoice_date, "monthly")
        body = bodies.get(period) or (await api.client.get(f"/api/v1/vat/summary?period={period}")).json()
        bodies[period] = body
        assert body["status"] == "ready" and body["review"]["agrees"] is True
        assert inv.invoice_number in {p["number"] for p in body["purchases"]}
        assert body["input_vat"]["amount_minor"] == sum(p["vat"]["amount_minor"] for p in body["purchases"])
        assert body["input_vat"]["amount_minor"] == body["invoice_input_vat"]["amount_minor"]
        assert body["net_payable"]["amount_minor"] == body["output_vat"]["amount_minor"] - body["input_vat"]["amount_minor"]
        assert body["data_as_of"]["books"]
    flags = {p["number"]: p["flags"] for b in bodies.values() for p in b["purchases"]}
    assert "missing_vat_number" in flags[inv_bad.invoice_number] and flags[inv_ok.invoice_number] == []
    # Asking again with unchanged books reuses the reviewed summary.
    period, body = next(iter(bodies.items()))
    again = (await api.client.get(f"/api/v1/vat/summary?period={period}")).json()
    assert again["action_id"] == body["action_id"]
    assert (await api.client.get("/api/v1/vat/summary?period=2026-13")).status_code == 422
    q = (await api.client.get(f"/api/v1/vat/summary?period={inv_ok.invoice_date.year}-Q4")).json()
    assert q["from"].endswith("-10-01") and q["to"].endswith("-12-31")


async def test_vat_disagreement_escalates_before_the_summary_is_ready(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    d = clock.today()
    async with write_session() as s:  # VAT input in the ledger with no supplier invoice behind it
        b_currency = "EGP"
        await posting.post(s, bid, posting.Draft(d, "manual", "t", "stray VAT").dr(posting.VAT_IN, 5000).cr(posting.AP, 5000),
                           b_currency, created_by="test")
    period = vat.period_for(d, "monthly")
    body = (await api.client.get(f"/api/v1/vat/summary?period={period}")).json()
    assert body["status"] == "needs_owner" and body["review"]["agrees"] is False
    async with read_session() as s:
        action = await s.get(Action, uuid.UUID(body["action_id"]))
        alerts = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.action_id == action.id))).scalars().all()  # type: ignore[union-attr]
    assert action is not None and action.stage == "escalated" and alerts


async def test_reminder_before_the_vat_period_closes(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    day = date(2026, 10, 22)  # 9 days before the October period ends
    clock.set_state("simulated", day)
    async with write_session() as s:
        bank = (await s.execute(select(BankAccount).where(BankAccount.business_id == bid,
                                                          BankAccount.is_cash_on_hand.is_(False)))).scalars().first()
        assert bank is not None
        for i in range(2):
            s.add(BankTransaction(business_id=bid, account_id=bank.id, date=day - timedelta(days=i), amount=Money(-150_00 - i, "EGP"),
                                  description=f"Card payment {i}", import_batch_id="t", meta={}))
    out = await vat.step_period_reminder(bid, day)
    assert out is not None and out["days_left"] == 9 and out["unmatched_payments"] >= 2
    async with read_session() as s:
        alert = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.business_id == bid,
                                                               ApprovalRequest.kind == "alert"))).scalars().all()
    assert any(a.text_en.startswith("VAT period 2026-10 ends in 9 days.") and "no invoice" in a.text_en for a in alert)
    assert await vat.step_period_reminder(bid, date(2026, 10, 5)) is None  # too early in the period


# ------------------------------------------------------------------ T124 13 weeks
async def test_13_week_forecast_has_three_scenarios(api: Any, cafe: dict[str, Any]) -> None:
    body = (await api.client.get("/api/v1/cash/forecast?horizon=13w")).json()
    assert set(body["scenarios"]) == {"expected", "pessimistic", "optimistic"}
    for weeks in body["scenarios"].values():
        assert len(weeks) == 13
        assert all((date.fromisoformat(b["week_start"]) - date.fromisoformat(a["week_start"])).days == 7
                   for a, b in zip(weeks, weeks[1:], strict=False))
    last = {sc: w[-1]["closing"]["amount_minor"] for sc, w in body["scenarios"].items()}
    assert last["pessimistic"] <= last["expected"] <= last["optimistic"]
    assert body["weeks"] == body["scenarios"]["expected"] and body["data_as_of"]


# ------------------------------------------------------------------ T125 scorecard
async def test_supplier_scorecard(api: Any, cafe: dict[str, Any]) -> None:
    from app.agents.stock.graphs import record_delivery

    bid = cafe["business_id"]
    today = clock.today()
    async with write_session() as s:
        milk = (await s.execute(select(Item).where(Item.business_id == bid, Item.name_en == "Milk"))).scalar_one()
        sup = await s.get(Supplier, milk.preferred_supplier_id)
        assert sup is not None
        price = (await s.execute(select(SupplierPrice).where(SupplierPrice.item_id == milk.id,
                                                             SupplierPrice.supplier_id == sup.id))).scalars().first()
        assert price is not None
        s.add(SupplierPrice(business_id=bid, supplier_id=sup.id, item_id=milk.id, unit=price.unit, pack_size=price.pack_size,
                            min_order_qty=price.min_order_qty, price=Money(price.price.amount_minor * 11 // 10, "EGP"),
                            valid_from=today))
        po = PurchaseOrder(business_id=bid, number="PO-SC1", supplier_id=sup.id, status="sent", total=price.price.times(40),
                           expected_date=today - timedelta(days=1), notes={}, sent_at=clock.clock_now() - timedelta(days=3))
        s.add(po)
        await s.flush()
        s.add(PurchaseOrderLine(business_id=bid, po_id=po.id, item_id=milk.id, qty=40, unit="L", unit_price=price.price,
                                line_total=price.price.times(40)))
        sup_id, po_id, stated = sup.id, po.id, sup.stated_lead_time_days
    await record_delivery(bid, {"po_id": str(po_id), "lines": [{"item_id": str(milk.id), "qty_received": "36"}]})
    body = (await api.client.get(f"/api/v1/suppliers/{sup_id}/scorecard")).json()
    assert body["lead_time"]["stated_days"] == stated and body["lead_time"]["observed_days"] is not None
    assert body["deliveries"]["count"] >= 1
    rec = body["deliveries"]["recent"][0]
    assert rec["po"] == "PO-SC1" and rec["lead_days"] == 3 and rec["on_time"] is False and rec["complete"] is False
    milk_prices = next(p for p in body["prices"] if p["name_en"] == "Milk")
    assert milk_prices["latest_change_pct"] == pytest.approx(10.0, abs=0.2)
    assert 0 <= body["reliability_score"] <= 1 and body["data_as_of"]
    assert (await api.client.get(f"/api/v1/suppliers/{uuid.uuid4()}/scorecard")).status_code == 404


# ------------------------------------------------------------------ T126 language on the user
async def test_language_is_stored_on_the_user(api: Any, cafe: dict[str, Any]) -> None:
    assert (await api.client.patch("/api/v1/me", json={"language": "ar"})).status_code == 200
    assert (await api.client.get("/api/v1/me")).json()["user"]["language"] == "ar"
    assert (await api.client.patch("/api/v1/me", json={"language": "fr"})).status_code == 422
