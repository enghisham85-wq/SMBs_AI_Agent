"""Cash endpoints (T095): position, forecast, shortfall plan, receivables/payables, bank statements, obligations."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, File, Form, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agents.accountant import bank_import
from app.agents.accountant.receivables import ageing_bucket
from app.agents.cashflow import graphs as cash_graphs
from app.agents.cashflow import payables as pay
from app.agents.cashflow import position, projection
from app.api.common import J, get_business, with_freshness
from app.core import clock
from app.core.auth import CurrentUser, RequireManager, RequireOwner
from app.core.errors import AppError, not_found
from app.db.engine import read_session
from app.db.types import Money
from app.harness.graph import run_action
from app.models.books import ReceivableInvoice
from app.models.cash import (
    CashForecast,
    CashForecastRun,
    PaymentPromise,
    PaymentReminder,
    PlanAction,
    ShortfallPlan,
)
from app.models.finance_master import Obligation

router = APIRouter(tags=["cash"])
COMMITTED_KINDS = ("payable", "po", "obligation")


def _m(minor: int, cur: str) -> Money:
    return Money(int(minor), cur)


async def _latest_run(bid: uuid.UUID) -> CashForecastRun | None:
    return await cash_graphs.latest_run(bid, clock.today())


# ------------------------------------------------------------------ position
@router.get("/cash/position")
async def cash_position(user: CurrentUser = RequireManager) -> Response:
    b = await get_business(user.business_id)
    today = clock.today()
    async with read_session() as s:
        balances = await position.account_balances(s, user.business_id, today)
        run = await _latest_run(user.business_id)
        if run is None or run.generated_on != today:
            inp, fresh = await projection.load_inputs(s, user.business_id, today)
            flows = [f.to_json() for f in projection.project(inp).flows]
            as_of = fresh.bank_data_as_of
        else:
            flows, as_of = run.flows, run.bank_data_as_of
    week_end = today + timedelta(days=7)
    committed = [f for f in flows if f["kind"] in COMMITTED_KINDS and f["amount_minor"] < 0
                 and today < date.fromisoformat(f["date"]) <= week_end]
    summ = position.summary(balances)
    return J(with_freshness({
        "accounts": [{"id": a.account_id, "name": a.name, "is_cash_on_hand": a.is_cash_on_hand,
                      "balance": _m(a.balance_minor, b.currency), "as_of": a.as_of} for a in balances],
        "total": _m(summ["total_minor"], b.currency), "cash_on_hand": _m(summ["cash_on_hand_minor"], b.currency),
        "committed_7d": {"total": _m(-sum(f["amount_minor"] for f in committed), b.currency),
                         "items": [{"date": f["date"], "kind": f["kind"], "label": f["label"],
                                    "amount": _m(-f["amount_minor"], b.currency)} for f in committed]},
    }, {"bank": as_of}))


# ------------------------------------------------------------------ forecast
def _series_rows(rows: list[CashForecast]) -> list[dict[str, Any]]:
    return [{"date": r.date, "opening": r.opening, "inflows": r.inflows, "outflows": r.outflows, "closing": r.closing,
             "below_buffer": r.below_buffer, "confidence": r.confidence} for r in sorted(rows, key=lambda x: x.date)]


def _proj_rows(p: projection.Projection, cur: str) -> list[dict[str, Any]]:
    return [{"date": d.date, "opening": _m(d.opening, cur), "inflows": _m(d.inflows, cur), "outflows": _m(d.outflows, cur),
             "closing": _m(d.closing, cur), "below_buffer": d.below_buffer, "confidence": d.confidence} for d in p.days]


@router.get("/cash/forecast")
async def cash_forecast(horizon: Literal["30d", "13w"] = "30d", scenario: str | None = None,
                        user: CurrentUser = RequireManager) -> Response:
    b = await get_business(user.business_id)
    cur = b.currency
    today = clock.today()
    if scenario is not None and scenario not in projection.SCENARIOS:
        raise AppError(422, "invalid_scenario", message_en="Scenario must be expected, pessimistic or optimistic.")
    if horizon == "13w":
        async with read_session() as s:
            inp, fresh = await projection.load_inputs(s, user.business_id, today, horizon=91)
        projs = projection.project_all(inp)
        sc = scenario or "expected"
        all_weeks = {name: [{"week_start": w.week_start, "inflows": _m(w.inflows, cur), "outflows": _m(w.outflows, cur),
                             "closing": _m(w.closing, cur), "below_buffer": w.below_buffer}
                            for w in projection.weekly(p)] for name, p in projs.items()}
        low = projs[sc].lowest
        return J(with_freshness({"horizon": "13w", "scenario": sc, "weeks": all_weeks[sc], "scenarios": all_weeks,
                                 "buffer": b.min_cash_buffer,
                                 "lowest": {"date": low.date, "balance": _m(low.closing, cur)},
                                 "confidence": {"low": fresh.low_confidence, "reason": fresh.reason_en()}},
                                {"bank": fresh.bank_data_as_of}))
    run = await _latest_run(user.business_id)
    if run is None:  # nothing saved yet: compute without saving
        async with read_session() as s:
            inp, fresh = await projection.load_inputs(s, user.business_id, today)
        projs = projection.project_all(inp)
        sc = scenario or "expected"
        p = projs[sc]
        low, first = p.lowest, p.first_below
        return J(with_freshness({
            "horizon": "30d", "scenario": sc, "primary_scenario": "expected", "generated_on": today, "saved": False,
            "series": _proj_rows(p, cur), "buffer": b.min_cash_buffer, "opening": _m(inp.opening_minor, cur),
            "lowest": {"date": low.date, "balance": _m(low.closing, cur)},
            "first_below_buffer": first.date if first else None,
            "confidence": {"low": fresh.low_confidence, "reason": fresh.reason_en()}, "checks": []},
            {"bank": fresh.bank_data_as_of, "forecast": None}))
    sc = scenario or run.primary_scenario
    async with read_session() as s:
        rows = (await s.execute(select(CashForecast).where(CashForecast.run_id == run.id,
                                                           CashForecast.scenario == sc))).scalars().all()
    series = _series_rows(list(rows))
    low_row = min(series, key=lambda r: (r["closing"].amount_minor, r["date"])) if series else None
    first_date = next((r["date"] for r in series if r["below_buffer"]), None)
    return J(with_freshness({
        "horizon": "30d", "scenario": sc, "primary_scenario": run.primary_scenario, "generated_on": run.generated_on,
        "saved": True, "run_id": run.id, "series": series, "buffer": run.buffer, "opening": run.opening_balance,
        "lowest": {"date": low_row["date"], "balance": low_row["closing"]} if low_row else None,
        "first_below_buffer": first_date,
        "confidence": {"low": run.low_confidence_reason is not None, "reason": run.low_confidence_reason},
        "checks": run.checks, "flows": run.flows}, {"bank": run.bank_data_as_of, "forecast": run.created_at}))


# ------------------------------------------------------------------ shortfall plan
async def _current_plan(bid: uuid.UUID) -> ShortfallPlan | None:
    async with read_session() as s:
        return (await s.execute(select(ShortfallPlan).where(
            ShortfallPlan.business_id == bid, ShortfallPlan.status.in_(("proposed", "presented", "accepted")))
            .order_by(ShortfallPlan.created_at.desc()).limit(1))).scalars().first()


def _action_out(a: PlanAction) -> dict[str, Any]:
    return {"id": a.id, "rank": a.rank, "type": a.type, "target_type": a.target_type, "target_ref": a.target_ref,
            "description_en": a.description_en, "description_ar": a.description_ar, "impact": a.impact, "risk": a.risk,
            "simulated_lowest": {"date": a.simulated_lowest_date, "balance": a.simulated_lowest_balance},
            "status": a.status}


@router.get("/cash/shortfall-plan")
async def shortfall_plan(user: CurrentUser = RequireManager) -> Response:
    p = await _current_plan(user.business_id)
    if p is None:
        return J(with_freshness({"plan": None}, {"forecast": None}))
    async with read_session() as s:
        acts = (await s.execute(select(PlanAction).where(PlanAction.plan_id == p.id).order_by(PlanAction.rank))).scalars().all()
        run = await s.get(CashForecastRun, p.forecast_run_id)
    return J(with_freshness({"plan": {
        "id": p.id, "status": p.status, "gap": p.gap_amount, "gap_date": p.gap_date, "days_to_act": p.days_to_act,
        "lowest": {"date": p.lowest_date, "balance": p.lowest_balance}, "combined_lowest": p.combined_lowest_balance,
        "generated_on": run.generated_on if run else None, "actions": [_action_out(a) for a in acts]}},
        {"forecast": run.created_at if run else None, "bank": run.bank_data_as_of if run else None}))


@router.post("/cash/shortfall-plan/actions/{action_id}/simulate")
async def simulate_action(action_id: uuid.UUID, user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        a = await s.get(PlanAction, action_id)
        if a is None or a.business_id != user.business_id:
            raise not_found("Plan action")
        plan = await s.get(ShortfallPlan, a.plan_id)
        assert plan is not None
        run = await s.get(CashForecastRun, plan.forecast_run_id)
        assert run is not None
        base = (await s.execute(select(CashForecast).where(CashForecast.run_id == run.id,
                                                           CashForecast.scenario == run.primary_scenario))).scalars().all()
    cur = run.buffer.currency
    return J({"action": _action_out(a), "buffer": run.buffer,
              "base": [{"date": r.date, "closing": r.closing} for r in sorted(base, key=lambda r: r.date)],
              "simulated": [{"date": x["date"], "closing": _m(x["closing_minor"], cur)} for x in a.simulated_series]})


# ------------------------------------------------------------------ receivables and payables
@router.get("/cash/receivables")
async def cash_receivables(user: CurrentUser = RequireManager) -> Response:
    today = clock.today()
    async with read_session() as s:
        rows = (await s.execute(select(ReceivableInvoice).where(ReceivableInvoice.business_id == user.business_id,
                                                                ReceivableInvoice.status.in_(("open", "partially_paid")))
                                .order_by(ReceivableInvoice.due_date))).scalars().all()
        ids = [r.id for r in rows]
        rems = (await s.execute(select(PaymentReminder).where(PaymentReminder.receivable_invoice_id.in_(ids)))).scalars().all() if ids else []
        proms = (await s.execute(select(PaymentPromise).where(PaymentPromise.receivable_invoice_id.in_(ids)))).scalars().all() if ids else []
    b = await get_business(user.business_id)
    buckets: dict[str, int] = {"current": 0, "1-30": 0, "31-60": 0, "60+": 0}
    out = []
    for r in rows:
        owed = r.total.amount_minor - r.amount_paid_minor
        bucket = ageing_bucket(r, today)
        buckets[bucket] = buckets.get(bucket, 0) + owed
        mine = sorted((x for x in rems if x.receivable_invoice_id == r.id), key=lambda x: x.level)
        out.append({"id": r.id, "number": r.number, "customer": r.customer_name, "due_date": r.due_date,
                    "days_overdue": max(0, (today - r.due_date).days), "bucket": bucket, "outstanding": _m(owed, b.currency),
                    "reminders": [{"id": x.id, "level": x.level, "status": x.status, "scheduled_for": x.scheduled_for,
                                   "sent_at": x.sent_at, "cancel_reason": x.cancel_reason} for x in mine],
                    "promises": [{"promised_date": p.promised_date, "amount": p.amount, "outcome": p.outcome}
                                 for p in proms if p.receivable_invoice_id == r.id]})
    return J(with_freshness({"ageing": {k: _m(v, b.currency) for k, v in buckets.items()}, "receivables": out},
                            {"books": clock.clock_now()}))


@router.get("/cash/payables")
async def cash_payables(user: CurrentUser = RequireManager) -> Response:
    today = clock.today()
    async with read_session() as s:
        inp, fresh = await projection.load_inputs(s, user.business_id, today)
    projection.project_all(inp)  # decides whether cash is tight
    cur = inp.currency
    items = []
    for p in inp.payables:
        rec = pay.recommend(p.terms, p.outstanding_minor, today, tight=inp.tight)
        items.append({"id": p.id, "label": p.label, "number": p.number, "outstanding": _m(p.outstanding_minor, cur),
                      "invoice_date": p.terms.invoice_date, "due_date": p.terms.due_date, "pay_on": rec.pay_on,
                      "method": rec.method, "discount": _m(rec.discount_minor, cur), "reason_en": rec.reason_en,
                      "reason_ar": rec.reason_ar, "within_terms": pay.within_terms(rec, p.terms),
                      "days_overdue": max(0, (today - p.terms.due_date).days)})
    invoiced = {p.po_id for p in inp.payables if p.po_id}
    pos = [{"id": po.id, "number": po.number, "label": po.label, "total": _m(po.total_minor, cur), "status": po.status,
            "expected_payment": pay.recommend(po.terms, po.total_minor, today, tight=inp.tight).pay_on,
            "counted": po.id not in invoiced} for po in inp.pos]
    return J(with_freshness({"tight": inp.tight, "payables": sorted(items, key=lambda x: x["pay_on"]), "open_orders": pos},
                            {"books": clock.clock_now(), "bank": fresh.bank_data_as_of}))


# ------------------------------------------------------------------ bank statements
@router.post("/bank/statements")
async def upload_statement(file: UploadFile = File(...), account_id: uuid.UUID | None = Form(None),
                           mapping: str | None = Form(None), user: CurrentUser = RequireManager) -> Response:
    try:
        inputs, parsed = await bank_import.prepare(user.business_id, await file.read(), account_id,
                                                   json.loads(mapping) if mapping else None)
    except bank_import.StatementError as exc:
        raise AppError(422, exc.code, message_en=exc.message_en, message_ar=exc.message_ar) from exc
    except json.JSONDecodeError as exc:
        raise AppError(422, "invalid_mapping", message_en="The column mapping is not valid JSON.") from exc
    if not parsed.rows:
        return J({"batch_id": inputs["batch_id"], "imported": 0, "skipped_duplicates": 0, "errors": parsed.errors}, 422)
    out = await run_action("import_bank_statement", inputs, user.business_id)
    if out["outcome"] != "completed":
        raise AppError(422, "not_imported", message_en="The statement could not be imported.", message_ar="تعذر استيراد كشف الحساب.")
    res = out["result"] or {}
    await cash_graphs.refresh(user.business_id)
    return J({"batch_id": inputs["batch_id"], "imported": res.get("imported", 0),
              "skipped_duplicates": res.get("skipped_duplicates", 0), "errors": parsed.errors, "dates": res.get("dates", [])})


# ------------------------------------------------------------------ obligations
OB_TYPES = Literal["rent", "salary", "loan", "tax", "utility", "subscription", "other"]
RECURRENCES = Literal["monthly", "quarterly", "annual", "once"]


class ObligationIn(BaseModel):
    type: OB_TYPES
    description: str = Field(min_length=1, max_length=200)
    amount_minor: int = Field(gt=0, le=10**15)
    next_due_date: date
    recurrence: RECURRENCES
    is_confirmed: bool = True


class ObligationPatch(BaseModel):
    type: OB_TYPES | None = None
    description: str | None = Field(None, min_length=1, max_length=200)
    amount_minor: int | None = Field(None, gt=0, le=10**15)
    next_due_date: date | None = None
    recurrence: RECURRENCES | None = None
    is_confirmed: bool | None = None


def _ob_out(o: Obligation) -> dict[str, Any]:
    return {"id": o.id, "type": o.type, "description": o.description, "amount": o.amount, "next_due_date": o.next_due_date,
            "recurrence": o.recurrence, "is_confirmed": o.is_confirmed, "last_seen_transaction_id": o.last_seen_transaction_id}


@router.get("/obligations")
async def list_obligations(user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        rows = (await s.execute(select(Obligation).where(Obligation.business_id == user.business_id)
                                .order_by(Obligation.next_due_date))).scalars().all()
    return J(with_freshness({"obligations": [_ob_out(o) for o in rows]},
                            {"obligations": max((o.updated_at for o in rows), default=None)}))


async def _save_obligation(bid: uuid.UUID, fields: dict[str, Any], ob_id: uuid.UUID | None = None) -> Obligation:
    inputs: dict[str, Any] = {"fields": {k: (v.isoformat() if isinstance(v, date) else v) for k, v in fields.items()}}
    if ob_id:
        inputs["obligation_id"] = str(ob_id)
    out = await run_action("save_obligation", inputs, bid)
    if out["outcome"] != "completed":
        raise AppError(422, "not_saved", message_en="The payment could not be saved.", message_ar="تعذر حفظ الدفعة.")
    async with read_session() as s:
        ob = await s.get(Obligation, uuid.UUID(out["result"]["obligation_id"]))
    assert ob is not None
    await cash_graphs.refresh(bid)
    return ob


@router.post("/obligations", status_code=201)
async def create_obligation(body: ObligationIn, user: CurrentUser = RequireOwner) -> Response:
    ob = await _save_obligation(user.business_id, body.model_dump())
    return J(_ob_out(ob), 201)


@router.patch("/obligations/{ob_id}")
async def update_obligation(ob_id: uuid.UUID, body: ObligationPatch, user: CurrentUser = RequireOwner) -> Response:
    async with read_session() as s:
        ob = await s.get(Obligation, ob_id)
    if ob is None or ob.business_id != user.business_id:
        raise not_found("Obligation")
    fields = body.model_dump(exclude_none=True)
    if not fields:
        return J(_ob_out(ob))
    return J(_ob_out(await _save_obligation(user.business_id, fields, ob_id)))
