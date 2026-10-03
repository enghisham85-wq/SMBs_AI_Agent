"""Customer invoices and Books endpoints."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.agents.accountant import posting
from app.config import get_settings
from app.core import clock
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.graph import run_action
from app.models.books import JournalLine, ReceivableInvoice
from app.models.cash import PaymentReminder
from app.models.finance_master import BankAccount, BankTransaction
from app.seed.sample_cafe import PASSWORDS, seed

START = date(2026, 10, 4)


@pytest.fixture
async def cafe(api: Any, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(get_settings(), "FILES_DIR", str(tmp_path / "files"))
    info = await seed(start_date=START, history_days=30)
    await api.login("owner", PASSWORDS["owner"])
    return info


def _body(**over: Any) -> dict[str, Any]:
    today = clock.today()
    body = {"customer_name": "Delta Offices", "invoice_date": today.isoformat(),
            "due_date": (today + timedelta(days=14)).isoformat(),
            "lines": [{"description": "Coffee for meeting", "qty": 10, "unit_price": 100}]}
    body.update(over)
    return body


async def test_create_posts_balanced_entry_with_vat(api: Any, cafe: dict[str, Any]) -> None:
    r = await api.client.post("/api/v1/receivables", json=_body())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["number"].startswith("INV-")
    assert data["total"]["display"] == "EGP 1,140.00"  # 1,000 + 14 % VAT
    async with read_session() as s:
        rec = await s.get(ReceivableInvoice, uuid.UUID(data["id"]))
        lines = (await s.execute(select(JournalLine).where(JournalLine.entry_id == rec.journal_entry_id))).scalars().all()
        accts = await posting.accounts(s, cafe["business_id"])
    assert sum(x.debit_minor for x in lines) == sum(x.credit_minor for x in lines)
    ar = sum(x.debit_minor for x in lines if x.account_id == accts[posting.AR].id)
    assert ar == rec.total.amount_minor
    listed = (await api.client.get("/api/v1/receivables?status=open")).json()["receivables"]
    assert any(x["id"] == data["id"] for x in listed)
    # Cash-Flow picks the invoice up on its next forecast.
    from app.agents.cashflow import graphs as cash_graphs

    await cash_graphs.step_cash_forecast(cafe["business_id"], clock.today())
    run = await cash_graphs.latest_run(cafe["business_id"], clock.today())
    # Payment may be split across dates (on time / late) but adds up to the invoice.
    parts = [f for f in run.flows if f["kind"] == "receivable" and f["ref"] == data["id"]]  # type: ignore[union-attr]
    assert parts and sum(f["amount_minor"] for f in parts) == rec.total.amount_minor


async def test_duplicate_number_and_bad_dates_are_rejected(api: Any, cafe: dict[str, Any]) -> None:
    assert (await api.client.post("/api/v1/receivables", json=_body(number="X-1"))).status_code == 201
    r = await api.client.post("/api/v1/receivables", json=_body(number="X-1"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_number"
    today = clock.today()
    r = await api.client.post("/api/v1/receivables", json=_body(due_date=(today - timedelta(days=1)).isoformat()))
    assert r.status_code == 422 and r.json()["error"]["code"] == "due_before_invoice"


async def test_bank_payment_marks_invoice_paid(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    data = (await api.client.post("/api/v1/receivables", json=_body())).json()
    async with write_session() as s:  # a reminder is waiting to go out
        reminder = PaymentReminder(business_id=bid, receivable_invoice_id=uuid.UUID(data["id"]), level=1,
                                   scheduled_for=clock.today() + timedelta(days=1), status="scheduled",
                                   text_en="Reminder", text_ar="تذكير")
        s.add(reminder)
        await s.flush()
        reminder_id = reminder.id
    async with write_session() as s:
        bank = (await s.execute(select(BankAccount).where(BankAccount.business_id == bid,
                                                          BankAccount.is_cash_on_hand.is_(False)))).scalar_one()
        txn = BankTransaction(business_id=bid, account_id=bank.id, date=clock.today(), amount=Money(114000, "EGP"),
                              description="Transfer from Delta Offices", import_batch_id="t", meta={})
        s.add(txn)
        await s.flush()
        tid = txn.id
    out = await run_action("apply_bank_match", {"txn_id": str(tid), "source": "manual", "confidence": 1.0,
                                                "candidate": {"type": "customer_payment", "ref": data["id"], "name": "Delta"}}, bid)
    assert out["outcome"] == "completed"
    detail = (await api.client.get(f"/api/v1/receivables/{data['id']}")).json()
    assert detail["status"] == "paid" and detail["payments"]
    async with read_session() as s:
        assert (await s.get(PaymentReminder, reminder_id)).status == "cancelled_paid"  # type: ignore[union-attr]


async def test_void_paid_invoice_is_refused_and_unpaid_is_voided(api: Any, cafe: dict[str, Any]) -> None:
    unpaid = (await api.client.post("/api/v1/receivables", json=_body())).json()
    r = await api.client.post(f"/api/v1/receivables/{unpaid['id']}/void", json={"reason": "entered twice"})
    assert r.status_code == 200
    async with read_session() as s:
        rec = await s.get(ReceivableInvoice, uuid.UUID(unpaid["id"]))
    assert rec.status == "void"
    paid = (await api.client.post("/api/v1/receivables", json=_body())).json()
    async with write_session() as s:
        rec = await s.get(ReceivableInvoice, uuid.UUID(paid["id"]))
        rec.amount_paid_minor, rec.status = rec.total.amount_minor, "paid"
    r = await api.client.post(f"/api/v1/receivables/{paid['id']}/void", json={"reason": "customer disputed"})
    assert r.status_code == 409


async def test_staff_are_refused(api: Any, cafe: dict[str, Any]) -> None:
    await api.client.post("/api/v1/auth/logout")
    await api.login("staff", PASSWORDS["staff"])
    assert (await api.client.post("/api/v1/receivables", json=_body())).status_code == 403
    assert (await api.client.get("/api/v1/documents")).status_code == 403


async def test_books_endpoints(api: Any, cafe: dict[str, Any]) -> None:
    samples = (await api.client.get("/api/v1/documents/samples")).json()["samples"]
    assert len(samples) == 13
    r = await api.client.post("/api/v1/documents/samples/en_coffee")
    assert r.status_code == 202 and r.json()["outcome"] == "posted"
    docs = (await api.client.get("/api/v1/documents")).json()["documents"]
    detail = (await api.client.get(f"/api/v1/documents/{docs[0]['id']}")).json()
    assert detail["invoice"]["status"] == "posted" and detail["extractions"]
    names = {c["name"] for c in detail["checks"]}
    passed = {c["name"] for c in detail["checks"] if c["passed"]}
    assert {"extraction_arithmetic", "date_sanity", "duplicate_invoice"} <= passed <= names
    f = await api.client.get(detail["document"]["file_url"])
    assert f.status_code == 200 and f.headers["content-type"] == "application/pdf"
    rq = (await api.client.get("/api/v1/review-queue")).json()
    assert set(rq) == {"questions", "held_invoices", "suggested_matches", "data_as_of"}
    rec = (await api.client.get("/api/v1/reconciliation")).json()
    assert "percent_matched" in rec and "data_as_of" in rec
    pnl = (await api.client.get(f"/api/v1/reports/pnl?from={START - timedelta(days=5)}&to={clock.today()}")).json()
    assert "net_profit_minor" in pnl
    bs = (await api.client.get("/api/v1/reports/balance-sheet")).json()
    assert bs["balanced"] is True
