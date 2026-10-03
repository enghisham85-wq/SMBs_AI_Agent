"""Cash flow: 30-day cash projection, shortfall plan, reminders, stale bank data, no double counting."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.agents.cashflow import graphs as cash_graphs
from app.agents.cashflow import payables as pay
from app.agents.cashflow import projection
from app.config import get_settings
from app.core import clock, settings_store
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.graph import run_action
from app.models.books import PayableInvoice, ReceivableInvoice
from app.models.cash import CashForecast, CashForecastRun, PaymentReminder, PlanAction, ShortfallPlan
from app.models.events import Event
from app.models.finance_master import BankAccount, BankTransaction
from app.models.harness import Action, ApprovalRequest, AuditLogEntry, Incident
from app.models.master import Supplier
from app.models.purchasing import PurchaseOrder
from app.seed.sample_cafe import PASSWORDS, seed

START = date(2026, 10, 4)  # the sample cafe's rent (1st) and salaries (28th) fall in the same week


@pytest.fixture
async def cafe(api: Any, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(get_settings(), "FILES_DIR", str(tmp_path / "files"))
    info = await seed(start_date=START, history_days=42)
    await api.login("owner", PASSWORDS["owner"])
    return info


async def _forecast(bid: uuid.UUID) -> CashForecastRun:
    await cash_graphs.step_cash_forecast(bid, clock.today())
    run = await cash_graphs.latest_run(bid, clock.today())
    assert run is not None and run.generated_on == clock.today()
    return run


async def _receivable(bid: uuid.UUID, customer: str) -> ReceivableInvoice:
    async with read_session() as s:
        return (await s.execute(select(ReceivableInvoice).where(ReceivableInvoice.business_id == bid,
                                                                ReceivableInvoice.customer_name == customer))).scalar_one()


async def _pay(bid: uuid.UUID, inv: ReceivableInvoice, reconcile: bool = True) -> None:
    """The customer's transfer lands in the bank; with `reconcile` the Accountant matches it."""
    async with write_session() as s:
        bank = (await s.execute(select(BankAccount).where(BankAccount.business_id == bid,
                                                          BankAccount.is_cash_on_hand.is_(False)))).scalar_one()
        txn = BankTransaction(business_id=bid, account_id=bank.id, date=clock.today(),
                              amount=Money(inv.total.amount_minor - inv.amount_paid_minor, inv.total.currency),
                              description=f"Transfer from {inv.customer_name} {inv.number}", import_batch_id="t", meta={})
        s.add(txn)
        await s.flush()
        tid = txn.id
    if reconcile:
        out = await run_action("apply_bank_match", {"txn_id": str(tid), "source": "manual", "confidence": 1.0,
                                                    "candidate": {"type": "customer_payment", "ref": str(inv.id),
                                                                  "name": inv.customer_name}}, bid)
        assert out["outcome"] == "completed"


async def _reminder(bid: uuid.UUID, inv: ReceivableInvoice, level: int) -> uuid.UUID:
    async with write_session() as s:
        r = PaymentReminder(business_id=bid, receivable_invoice_id=inv.id, level=level, scheduled_for=clock.today(),
                            status="scheduled", text_en=f"Invoice {inv.number} {inv.total.to_display()}", text_ar="تذكير")
        s.add(r)
        await s.flush()
        return r.id


async def _status(rid: uuid.UUID) -> PaymentReminder:
    async with read_session() as s:
        r = await s.get(PaymentReminder, rid)
    assert r is not None
    return r


# ------------------------------------------------------------------ 30-day projection
async def test_30_day_projection_with_lowest_point(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    run = await _forecast(bid)
    async with read_session() as s:
        rows = (await s.execute(select(CashForecast).where(CashForecast.run_id == run.id,
                                                           CashForecast.scenario == "expected"))).scalars().all()
    assert len(rows) == 30
    low = min(rows, key=lambda r: (r.closing.amount_minor, r.date))
    assert run.lowest_balance == low.closing and run.lowest_date == low.date
    # every day: opening + inflows - outflows = closing, and days chain
    rows = sorted(rows, key=lambda r: r.date)
    for a, b in zip(rows, rows[1:], strict=False):
        assert b.opening == a.closing
    assert all(r.opening.amount_minor + r.inflows.amount_minor - r.outflows.amount_minor == r.closing.amount_minor for r in rows)
    kinds = {f["kind"] for f in run.flows}
    assert {"sales", "obligation", "receivable", "planned_purchases"} <= kinds

    body = (await api.client.get("/api/v1/cash/forecast")).json()
    assert len(body["series"]) == 30 and body["lowest"]["date"] == low.date.isoformat()
    assert body["buffer"]["decimals"] == 2 and body["data_as_of"]["bank"]
    assert body["confidence"]["low"] is False
    pos = (await api.client.get("/api/v1/cash/position")).json()
    assert pos["cash_on_hand"]["amount_minor"] > 0 and len(pos["accounts"]) == 2
    assert "committed_7d" in pos and pos["data_as_of"]["bank"]
    weeks = (await api.client.get("/api/v1/cash/forecast?horizon=13w&scenario=pessimistic")).json()
    assert 13 <= len(weeks["weeks"]) <= 14


# ------------------------------------------------------------------ shortfall plan
async def test_shortfall_flagged_14_days_ahead_with_ranked_simulated_plan(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    run = await _forecast(bid)
    await cash_graphs.step_shortfall_plan(bid, clock.today())
    async with read_session() as s:
        plan = (await s.execute(select(ShortfallPlan).where(ShortfallPlan.business_id == bid))).scalar_one()
        acts = (await s.execute(select(PlanAction).where(PlanAction.plan_id == plan.id).order_by(PlanAction.rank))).scalars().all()
        ev = (await s.execute(select(Event).where(Event.type == "shortfall.predicted"))).scalars().all()
        q = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.business_id == bid, ApprovalRequest.agent == "cashflow",
                                                           ApprovalRequest.status == "pending"))).scalars().all()
    # rent + salaries week: salaries on the 28th, rent on the 1st
    assert plan.gap_date >= date(2026, 10, 28)
    assert plan.days_to_act >= 14
    assert plan.gap_amount.amount_minor == 5_000_000 - run.lowest_balance.amount_minor > 0
    assert [a.rank for a in acts] == list(range(1, len(acts) + 1))
    assert acts[-1].type == "financing" and all(a.type != "financing" for a in acts[:-1])
    assert len(acts) >= 3
    for a in acts:
        assert a.impact.amount_minor > 0
        assert a.simulated_lowest_balance.amount_minor > run.lowest_balance.amount_minor
        assert a.risk in ("low", "medium", "high") and a.simulated_series
    assert len(ev) == 1 and ev[0].payload["plan_id"] == str(plan.id)
    assert len(q) == 1 and "Heads up" in q[0].text_en and len(q[0].options) <= 4

    body = (await api.client.get("/api/v1/cash/shortfall-plan")).json()["plan"]
    assert body["days_to_act"] == plan.days_to_act and body["actions"][0]["impact"]["amount_minor"] > 0
    sim = (await api.client.post(f"/api/v1/cash/shortfall-plan/actions/{acts[0].id}/simulate")).json()
    assert len(sim["simulated"]) == len(sim["base"]) == 30
    assert min(x["closing"]["amount_minor"] for x in sim["simulated"]) == acts[0].simulated_lowest_balance.amount_minor

    # The owner picks the top action: it is accepted and later forecasts apply it.
    from app.approvals import service as approvals
    from app.models.tenancy import User

    async with read_session() as s:
        owner = (await s.execute(select(User).where(User.business_id == bid, User.role == "owner"))).scalar_one()
    res = await approvals.resolve(str(q[0].id), q[0].options[0]["key"], owner, "dashboard")
    assert res.status == "resolved"
    async with read_session() as s:
        accepted = await s.get(PlanAction, uuid.UUID(q[0].options[0]["key"].split(":", 1)[1]))
    assert accepted is not None and accepted.status == "accepted"
    rerun = await _forecast(bid)
    assert rerun.lowest_balance.amount_minor > run.lowest_balance.amount_minor


async def test_no_recommendation_pays_beyond_terms(api: Any, cafe: dict[str, Any]) -> None:
    today = clock.today()
    terms = pay.Terms(today - timedelta(days=5), today + timedelta(days=9), None, None)
    discounted = pay.Terms(today - timedelta(days=2), today + timedelta(days=28), pay.Decimal("2"), 10)
    for t in (terms, discounted):
        for tight in (False, True):
            rec = pay.recommend(t, 100_000, today, tight=tight)
            assert rec.pay_on <= t.due_date and pay.within_terms(rec, t)
    assert pay.recommend(discounted, 100_000, today, tight=False).method == "early_discount"
    assert pay.recommend(terms, 100_000, today, tight=True).method == "end_of_terms"
    # A posted supplier invoice through the API
    bid = cafe["business_id"]
    async with write_session() as s:
        sup = (await s.execute(select(Supplier).where(Supplier.business_id == bid).limit(1))).scalars().first()
        assert sup is not None
        s.add(PayableInvoice(business_id=bid, supplier_id=sup.id, invoice_number="S-1", normalised_number="s1",
                             invoice_date=today - timedelta(days=3), due_date=today + timedelta(days=11), lines=[],
                             subtotal=Money(100000, "EGP"), vat_amount=Money(14000, "EGP"), total=Money(114000, "EGP"),
                             status="posted"))
    await _forecast(bid)
    await cash_graphs.step_shortfall_plan(bid, clock.today())
    body = (await api.client.get("/api/v1/cash/payables")).json()
    assert body["payables"] and all(p["within_terms"] and p["pay_on"] <= p["due_date"] for p in body["payables"])
    async with read_session() as s:
        delays = (await s.execute(select(PlanAction).where(PlanAction.type == "delay_payable"))).scalars().all()
    for a in delays:
        assert all(adj["move_to"] <= (today + timedelta(days=11)).isoformat() for adj in a.adjustments)


# ------------------------------------------------------------------ payment reminders and their approval rule
async def test_reminder_waits_for_approval_then_cancelled_when_paid(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    await cash_graphs.step_reminders(bid, clock.today())
    inv = await _receivable(bid, "Al Mazaya Offices")  # overdue
    async with read_session() as s:
        rem = (await s.execute(select(PaymentReminder).where(PaymentReminder.receivable_invoice_id == inv.id))).scalar_one()
        req = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.agent == "cashflow",
                                                             ApprovalRequest.status == "pending"))).scalars().all()
    assert rem.level == 1 and rem.status == "pending_approval"  # default: every reminder needs approval
    assert any(r.context.get("reminder_id") == str(rem.id) for r in req)
    await _pay(bid, inv)  # payment arrives while the request is waiting
    r = await _status(rem.id)
    assert r.status == "cancelled_paid" and r.sent_at is None
    async with read_session() as s:
        withdrawn = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.status == "superseded"))).scalars().all()
        alerts = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.kind == "alert",
                                                                ApprovalRequest.agent == "cashflow"))).scalars().all()
    assert withdrawn
    assert any(f"paid invoice {inv.number}" in a.text_en and "cancelled the reminder" in a.text_en for a in alerts)


async def test_reminder_for_just_paid_invoice_is_cancelled_before_sending(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    inv = await _receivable(bid, "Zamalek Events")
    rid = await _reminder(bid, inv, 1)
    await _pay(bid, inv, reconcile=False)  # in the bank, not yet reconciled
    out = await run_action("send_reminder", {"reminder_id": str(rid)}, bid)
    assert out["outcome"] == "cancelled" and not out["interrupted"]
    assert (await _status(rid)).status == "cancelled_paid"


@pytest.mark.parametrize("mode", ["off", "polite_only"])
async def test_reminder_auto_approve_rule(api: Any, cafe: dict[str, Any], mode: str) -> None:
    bid = cafe["business_id"]
    async with write_session() as s:
        await settings_store.put(s, bid, "reminder_auto_approve", mode)
    polite = await _reminder(bid, await _receivable(bid, "Al Mazaya Offices"), 1)
    firm = await _reminder(bid, await _receivable(bid, "Nile Tech Hub"), 2)
    out1 = await run_action("send_reminder", {"reminder_id": str(polite)}, bid)
    out2 = await run_action("send_reminder", {"reminder_id": str(firm)}, bid)
    assert out2["interrupted"] and (await _status(firm)).status == "pending_approval"  # level 2 always waits
    if mode == "off":
        assert out1["interrupted"] and (await _status(polite)).status == "pending_approval"
    else:
        r = await _status(polite)
        assert out1["outcome"] == "completed" and r.status == "sent" and r.auto_approved
        async with read_session() as s:
            audited = (await s.execute(select(AuditLogEntry).where(AuditLogEntry.action_id == out1["action_id"],
                                                                   AuditLogEntry.event == "auto_approved"))).scalars().all()
        assert audited
    # a paid invoice is cancelled in both modes
    zam = await _receivable(bid, "Zamalek Events")
    rid = await _reminder(bid, zam, 1)
    await _pay(bid, zam)
    out = await run_action("send_reminder", {"reminder_id": str(rid)}, bid)
    assert out["outcome"] == "cancelled" and (await _status(rid)).status == "cancelled_paid"


# ------------------------------------------------------------------ stale bank data
async def test_stale_bank_data_marks_low_confidence_and_asks_for_statement(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    later = date(2026, 10, 7)  # Sun-Wed without bank data: 4 business days
    clock.set_state("simulated", later)
    run = await _forecast(bid)
    assert run.low_confidence_reason and "business days old" in run.low_confidence_reason
    body = (await api.client.get("/api/v1/cash/forecast")).json()
    assert body["confidence"]["low"] is True
    async with read_session() as s:
        inc = (await s.execute(select(Incident).where(Incident.business_id == bid, Incident.type == "bank_freshness"))).scalar_one()
        alert = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.kind == "alert",
                                                               ApprovalRequest.agent == "cashflow"))).scalars().all()
    assert inc.status in ("open", "investigating") and any("fresh statement" in a.text_en for a in alert)
    # The owner uploads a statement for the missing days: the forecast is confident again.
    csv = "date,description,amount,reference\n" + "".join(
        f"{(START + timedelta(days=i)).strftime('%d/%m/%Y')},Card settlement,1000.00,r{i}\n" for i in range(4))
    r = await api.client.post("/api/v1/bank/statements", files={"file": ("stmt.csv", csv.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 4
    again = await api.client.post("/api/v1/bank/statements", files={"file": ("stmt.csv", csv.encode(), "text/csv")})
    assert again.json()["imported"] == 0 and again.json()["skipped_duplicates"] == 4
    run2 = await cash_graphs.latest_run(bid, later)
    assert run2 is not None and run2.low_confidence_reason is None
    async with read_session() as s:
        assert (await s.get(Incident, inc.id)).status == "resolved"  # type: ignore[union-attr]
    bad = await api.client.post("/api/v1/bank/statements",
                                files={"file": ("x.csv", b"date,description,amount,currency\n07/10/2026,x,10,USD\n", "text/csv")})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "currency_mismatch"


# ------------------------------------------------------------------ each payable counted once
async def test_po_and_invoice_counted_once(api: Any, cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    today = clock.today()
    async with write_session() as s:
        sup = (await s.execute(select(Supplier).where(Supplier.business_id == bid).limit(1))).scalars().first()
        assert sup is not None
        po = PurchaseOrder(business_id=bid, number="PO-9001", supplier_id=sup.id, status="sent",
                           total=Money(500000, "EGP"), expected_date=today + timedelta(days=2))
        other = PurchaseOrder(business_id=bid, number="PO-9002", supplier_id=sup.id, status="sent",
                              total=Money(300000, "EGP"), expected_date=today + timedelta(days=2))
        s.add_all([po, other])
        await s.flush()
        s.add(PayableInvoice(business_id=bid, supplier_id=sup.id, invoice_number="F-77", normalised_number="f77",
                             invoice_date=today, due_date=today + timedelta(days=10), lines=[], purchase_order_id=po.id,
                             subtotal=Money(500000, "EGP"), vat_amount=Money(0, "EGP"), total=Money(500000, "EGP"),
                             status="posted"))
        po_id, other_id = str(po.id), str(other.id)
    run = await _forecast(bid)
    keys = [f["key"] for f in run.flows]
    assert f"po:{po_id}" not in keys and f"po:{other_id}" in keys
    assert sum(1 for f in run.flows if f["kind"] == "payable" and f["label"].endswith("F-77")) == 1
    check = next(c for c in run.checks if c["name"] == "double_counting")
    assert check["passed"] and any(d["id"] == po_id for d in check["details"]["deduped"])


async def test_staff_cannot_see_cash(api: Any, cafe: dict[str, Any]) -> None:
    await api.login("staff", PASSWORDS["staff"])
    r = await api.client.get("/api/v1/cash/forecast")
    assert r.status_code == 403
    async with read_session() as s:
        denied = (await s.execute(select(AuditLogEntry).where(AuditLogEntry.event == "permission_denied"))).scalars().all()
    assert denied


async def test_obligations_owner_only(api: Any, cafe: dict[str, Any]) -> None:
    body = {"type": "subscription", "description": "Accounting software", "amount_minor": 150000,
            "next_due_date": (clock.today() + timedelta(days=5)).isoformat(), "recurrence": "monthly"}
    r = await api.client.post("/api/v1/obligations", json=body)
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    r = await api.client.patch(f"/api/v1/obligations/{oid}", json={"amount_minor": 160000})
    assert r.json()["amount"]["amount_minor"] == 160000
    run = await cash_graphs.latest_run(cafe["business_id"], clock.today())
    assert run is not None and any(f["key"].startswith(f"obligation:{oid}") for f in run.flows)
    await api.login("manager", PASSWORDS["manager"])
    assert (await api.client.get("/api/v1/obligations")).status_code == 200
    assert (await api.client.post("/api/v1/obligations", json=body)).status_code == 403


async def test_daily_run_saves_forecast_and_actions_go_through_harness(api: Any, cafe: dict[str, Any]) -> None:
    from app.core import scheduler

    bid = cafe["business_id"]
    await scheduler.advance(bid, days=2)
    async with read_session() as s:
        runs = (await s.execute(select(CashForecastRun).where(CashForecastRun.business_id == bid))).scalars().all()
        acts = {a.type for a in (await s.execute(select(Action).where(Action.agent == "cashflow"))).scalars()}
    assert {r.generated_on for r in runs} == {START, START + timedelta(days=1)}
    assert {"save_forecast_run", "publish_budget"} <= acts
    body = (await api.client.get("/api/v1/cash/forecast")).json()
    assert body["saved"] and body["generated_on"] == (START + timedelta(days=1)).isoformat()


def test_projection_occurrences_clamp_month_end() -> None:
    ob = projection.ObligationIn("x", "rent", "r", 1, date(2026, 1, 31), "monthly")
    assert projection.occurrences(ob, date(2026, 2, 1), date(2026, 3, 31)) == [date(2026, 2, 28), date(2026, 3, 31)]
