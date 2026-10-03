"""Daily cash projection from the day after the last bank data. Only `load_inputs` touches the DB."""

from __future__ import annotations

import calendar as pycal
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.cashflow import payables as pay
from app.agents.cashflow import position

SETTLEMENT_LAG = {"cash": 0, "card": 1, "transfer": 1, "credit": 30}
SCENARIOS = ("expected", "pessimistic", "optimistic")
FALLBACK_BAND = Decimal("0.15")
PATTERN_WEEKS = 4
LATE_DELAY_DAYS = 21


@dataclass(frozen=True)
class Receivable:
    id: str
    number: str
    customer: str
    outstanding_minor: int
    due_date: date
    late_score: float  # 0 always on time ... 1 always late


@dataclass(frozen=True)
class Payable:
    id: str
    label: str
    outstanding_minor: int
    terms: pay.Terms
    po_id: str | None = None
    supplier_id: str | None = None
    number: str = ""


@dataclass(frozen=True)
class OpenPO:
    id: str
    number: str
    label: str
    total_minor: int
    terms: pay.Terms  # virtual terms: invoice on the expected delivery date
    is_critical: bool
    status: str


@dataclass(frozen=True)
class ObligationIn:
    id: str
    type: str
    description: str
    amount_minor: int
    anchor: date
    recurrence: str


@dataclass
class Inputs:
    today: date
    start: date  # first projected day (day after the last bank data)
    end: date
    currency: str
    opening_minor: int
    buffer_minor: int
    # revenue per day: (low, expected, high) in minor units
    sales: dict[date, tuple[int, int, int]] = field(default_factory=dict)
    actual_sales: dict[date, dict[str, int]] = field(default_factory=dict)  # days with sales but no bank data yet
    method_share: dict[str, Decimal] = field(default_factory=lambda: {"cash": Decimal(1)})
    card_fee_percent: Decimal = Decimal(2)
    receivables: list[Receivable] = field(default_factory=list)
    payables: list[Payable] = field(default_factory=list)
    pos: list[OpenPO] = field(default_factory=list)
    obligations: list[ObligationIn] = field(default_factory=list)
    regular_spend: dict[int, int] = field(default_factory=dict)  # iso weekday -> usual outflow (minor)
    history_daily_sales: list[int] = field(default_factory=list)
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    tight: bool = False
    low_confidence: bool = False
    sales_source: str = "forecast"


@dataclass(frozen=True)
class Flow:
    date: date
    amount_minor: int  # + in, - out
    kind: str  # sales | card_fee | receivable | payable | po | planned_purchases | obligation | financing
    key: str
    label: str
    ref: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"date": self.date.isoformat(), "amount_minor": self.amount_minor, "kind": self.kind, "key": self.key,
                "label": self.label, "ref": self.ref}


@dataclass
class Day:
    date: date
    opening: int
    inflows: int
    outflows: int
    closing: int
    below_buffer: bool
    confidence: float


@dataclass
class Projection:
    scenario: str
    days: list[Day]
    flows: list[Flow]
    deduped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def lowest(self) -> Day:
        return min(self.days, key=lambda d: (d.closing, d.date))

    @property
    def first_below(self) -> Day | None:
        return next((d for d in self.days if d.below_buffer), None)

    def closing_on(self, d: date) -> int | None:
        return next((x.closing for x in self.days if x.date == d), None)

    def series(self) -> list[dict[str, Any]]:
        return [{"date": d.date.isoformat(), "closing_minor": d.closing} for d in self.days]


def occurrences(ob: ObligationIn, start: date, end: date) -> list[date]:
    """Due dates of an obligation in [start, end]. Monthly dates clamp to the month's last day."""
    if ob.recurrence == "once":
        return [ob.anchor] if start <= ob.anchor <= end else []
    step = {"monthly": 1, "quarterly": 3, "annual": 12}[ob.recurrence]
    out = []
    y, m = ob.anchor.year, ob.anchor.month
    while True:
        d = date(y, m, min(ob.anchor.day, pycal.monthrange(y, m)[1]))
        if d > end:
            break
        if d >= start:
            out.append(d)
        m += step
        while m > 12:
            y, m = y + 1, m - 12
    return out


def _receivable_flows(r: Receivable, scenario: str, start: date) -> list[Flow]:
    p_on_time = max(0.05, min(1.0, 1 - r.late_score))
    overdue = r.due_date < start
    amt = r.outstanding_minor
    key = f"receivable:{r.id}"
    label = f"{r.customer} {r.number}"
    if scenario == "optimistic":
        return [Flow(max(r.due_date, start + timedelta(days=2 if overdue else 0)), amt, "receivable", key, label, r.id)]
    if scenario == "pessimistic":
        late = overdue or r.late_score > 0.2
        when = (max(r.due_date, start) + timedelta(days=30)) if late else r.due_date + timedelta(days=7)
        return [Flow(max(when, start), amt, "receivable", key, label, r.id)]
    if overdue:
        p_on_time *= 0.5  # already late: less likely to pay soon
        first = start + timedelta(days=3)
    else:
        first = r.due_date
    on_time = int(round(amt * p_on_time))
    out = [Flow(first, on_time, "receivable", key + ":a", label, r.id)] if on_time else []
    if amt - on_time:
        out.append(Flow(max(r.due_date, start) + timedelta(days=LATE_DELAY_DAYS), amt - on_time, "receivable",
                        key + ":b", label + " (late share)", r.id))
    return out


def _sales_flows(inp: Inputs, scenario: str) -> list[Flow]:
    idx = {"pessimistic": 0, "expected": 1, "optimistic": 2}[scenario]
    out: list[Flow] = []
    fee_pct = inp.card_fee_percent
    per_day: dict[date, dict[str, int]] = {}
    for d, by in inp.actual_sales.items():
        per_day[d] = dict(by)
    for d, triple in inp.sales.items():
        if d in per_day:
            continue
        total = triple[idx]
        split: dict[str, int] = {}
        remaining = total
        methods = sorted(inp.method_share.items())
        for i, (method, share) in enumerate(methods):
            part = remaining if i == len(methods) - 1 else int(Decimal(total) * share)
            split[method] = part
            remaining -= part
        per_day[d] = split
    for d, split in sorted(per_day.items()):
        for method, minor in split.items():
            if minor <= 0:
                continue
            when = d + timedelta(days=SETTLEMENT_LAG.get(method, 0))
            if when < inp.start:
                continue
            out.append(Flow(when, minor, "sales", f"sales:{method}:{d.isoformat()}", f"{method} sales {d.isoformat()}"))
            if method == "card":
                fee = int((Decimal(minor) * fee_pct / 100).to_integral_value())
                out.append(Flow(when, -fee, "card_fee", f"card_fee:{d.isoformat()}", f"card fee {d.isoformat()}"))
    return out


def build_flows(inp: Inputs, scenario: str) -> tuple[list[Flow], list[dict[str, Any]]]:
    """All cash movements for one scenario, plus the list of PO commitments dropped as already invoiced."""
    flows = _sales_flows(inp, scenario)
    for r in inp.receivables:
        flows.extend(_receivable_flows(r, scenario, inp.start))
    invoiced_pos = {p.po_id for p in inp.payables if p.po_id}
    seen_payables: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for p in inp.payables:
        if p.id in seen_payables:
            deduped.append({"kind": "payable", "id": p.id, "reason": "listed twice"})
            continue
        seen_payables.add(p.id)
        rec = pay.recommend(p.terms, p.outstanding_minor, inp.today, tight=inp.tight)
        amount = p.outstanding_minor - rec.discount_minor
        flows.append(Flow(max(rec.pay_on, inp.start), -amount, "payable", f"payable:{p.id}", p.label, p.id))
    for po in inp.pos:
        if po.id in invoiced_pos:
            deduped.append({"kind": "po", "id": po.id, "number": po.number, "reason": "invoice already counted"})
            continue
        rec = pay.recommend(po.terms, po.total_minor, inp.today, tight=inp.tight)
        flows.append(Flow(max(rec.pay_on, inp.start), -po.total_minor, "po", f"po:{po.id}", po.label, po.id))
    for ob in inp.obligations:
        for d in occurrences(ob, inp.start, inp.end):
            flows.append(Flow(d, -ob.amount_minor, "obligation", f"obligation:{ob.id}:{d.isoformat()}",
                              f"{ob.type}: {ob.description}", ob.id))
    flows.extend(_planned_purchases(inp, flows))
    return apply_adjustments(flows, inp.adjustments), deduped


def _week(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _planned_purchases(inp: Inputs, known: list[Flow]) -> list[Flow]:
    """The usual weekly spending pattern, less supplier orders and invoices already counted that week."""
    pattern_total = sum(inp.regular_spend.values())
    if pattern_total <= 0:
        return []
    committed: dict[date, int] = defaultdict(int)
    for f in known:
        if f.kind in ("payable", "po"):
            committed[_week(f.date)] += -f.amount_minor
    out = []
    d = inp.start
    while d <= inp.end:
        usual = inp.regular_spend.get(d.isoweekday(), 0)
        if usual > 0:
            wk = _week(d)
            days_in_window = [wk + timedelta(days=i) for i in range(7) if inp.start <= wk + timedelta(days=i) <= inp.end]
            window_usual = sum(inp.regular_spend.get(x.isoweekday(), 0) for x in days_in_window)
            left = max(0, window_usual - committed[wk])
            amount = int(usual * left / window_usual) if window_usual else 0
            if amount > 0:
                out.append(Flow(d, -amount, "planned_purchases", f"planned:{d.isoformat()}",
                                "usual supplier and other spending"))
        d += timedelta(days=1)
    return out


def _matches(adj: dict[str, Any], f: Flow) -> bool:
    if "key" in adj and f.key != adj["key"]:
        return False
    if "prefix" in adj and not f.key.startswith(adj["prefix"]):
        return False
    if "until" in adj and f.date > date.fromisoformat(adj["until"]):
        return False
    return "key" in adj or "prefix" in adj


def apply_adjustments(flows: list[Flow], adjustments: list[dict[str, Any]]) -> list[Flow]:
    """Replay plan actions on the flows: move a flow, shift it, scale it, or add a new one."""
    out = list(flows)
    for adj in adjustments:
        if "add" in adj:
            a = adj["add"]
            out.append(Flow(date.fromisoformat(a["date"]), int(a["amount_minor"]), a.get("kind", "financing"),
                            a.get("key", "adjustment"), a.get("label", "adjustment")))
            continue
        new = []
        for f in out:
            if _matches(adj, f):
                if "move_to" in adj:
                    f = replace(f, date=date.fromisoformat(adj["move_to"]))
                if "shift_days" in adj:
                    f = replace(f, date=f.date + timedelta(days=int(adj["shift_days"])))
                if "scale" in adj:
                    f = replace(f, amount_minor=int(f.amount_minor * Decimal(str(adj["scale"]))))
            new.append(f)
        out = new
    return out


def simulate(inp: Inputs, flows: list[Flow], scenario: str = "expected") -> Projection:
    by_day: dict[date, list[Flow]] = defaultdict(list)
    for f in flows:
        by_day[f.date].append(f)
    days: list[Day] = []
    bal = inp.opening_minor
    base_conf = 0.6 if inp.low_confidence else 0.9
    d = inp.start
    i = 0
    while d <= inp.end:
        ins = sum(f.amount_minor for f in by_day.get(d, []) if f.amount_minor > 0)
        outs = -sum(f.amount_minor for f in by_day.get(d, []) if f.amount_minor < 0)
        closing = bal + ins - outs
        days.append(Day(d, bal, ins, outs, closing, closing < inp.buffer_minor, round(max(0.3, base_conf - 0.01 * i), 2)))
        bal = closing
        d += timedelta(days=1)
        i += 1
    in_range = [f for f in flows if inp.start <= f.date <= inp.end]
    return Projection(scenario, days, sorted(in_range, key=lambda f: (f.date, f.kind, f.key)))


def project(inp: Inputs, scenario: str = "expected", extra_adjustments: list[dict[str, Any]] | None = None) -> Projection:
    if extra_adjustments:
        inp = replace(inp, adjustments=[*inp.adjustments, *extra_adjustments])
    flows, deduped = build_flows(inp, scenario)
    proj = simulate(inp, flows, scenario)
    proj.deduped = deduped
    return proj


def project_all(inp: Inputs) -> dict[str, Projection]:
    """All three scenarios. When the pessimistic case breaches the buffer, payables move to end of terms."""
    out = {sc: project(inp, sc) for sc in SCENARIOS}
    if not inp.tight and out["pessimistic"].first_below is not None:
        tight = replace(inp, tight=True)
        out = {sc: project(tight, sc) for sc in SCENARIOS}
        inp.tight = True
    return out


@dataclass
class Week:
    week_start: date
    inflows: int = 0
    outflows: int = 0
    closing: int = 0  # balance at the end of the week's last day
    below_buffer: bool = False


def weekly(p: Projection, weeks: int = 13) -> list[Week]:
    """13-week view: the daily projection summed into 7-day buckets."""
    out: list[Week] = []
    for d in p.days:
        if not out or (d.date - out[-1].week_start).days >= 7:
            if len(out) == weeks:
                break
            out.append(Week(d.date))
        w = out[-1]
        w.inflows += d.inflows
        w.outflows += d.outflows
        w.closing = d.closing
        w.below_buffer = w.below_buffer or d.below_buffer
    return out


async def _sales_inputs(s: AsyncSession, business_id: uuid.UUID, start: date, end: date,
                        today: date) -> tuple[dict[date, tuple[int, int, int]], dict[date, dict[str, int]],
                                              dict[str, Decimal], list[int], str]:
    from app.agents.stock import demand
    from app.models.finance_master import Sale
    from app.models.master import Item

    hist_start = today - timedelta(days=56)
    sales = (await s.execute(select(Sale).where(Sale.business_id == business_id, Sale.date >= hist_start,
                                                Sale.date <= today))).scalars().all()
    daily: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in sales:
        daily[r.date][r.payment_method] += r.amount_total.amount_minor
    recent = [d for d in daily if d > today - timedelta(days=28)]
    share_tot: dict[str, int] = defaultdict(int)
    for d in recent:
        for m, v in daily[d].items():
            share_tot[m] += v
    total = sum(share_tot.values())
    method_share = ({m: Decimal(v) / Decimal(total) for m, v in share_tot.items()} if total else {"cash": Decimal(1)})
    history = [sum(daily[d].values()) for d in sorted(daily) if d < start]
    # sales already recorded but not yet in the bank
    actual = {d: dict(daily[d]) for d in daily if start - timedelta(days=1) <= d <= today}
    # Card and transfer sales of the last bank day settle on the first projected day.
    prices = {i.id: i.sale_price.amount_minor for i in (await s.execute(select(Item).where(
        Item.business_id == business_id, Item.is_sold.is_(True)))).scalars()}
    out: dict[date, tuple[int, int, int]] = {}
    source = "fallback"
    gen = await demand.latest_generation(s, business_id, today)
    if gen is not None and (today - gen).days <= 3:
        from app.models.stock_ops import DemandForecast

        rows = (await s.execute(select(DemandForecast).where(DemandForecast.business_id == business_id,
                                                             DemandForecast.generated_on == gen,
                                                             DemandForecast.item_id.in_(list(prices))))).scalars().all()
        acc: dict[date, list[Decimal]] = defaultdict(lambda: [Decimal(0)] * 3)
        for fc in rows:
            p = prices[fc.item_id]
            a = acc[fc.forecast_date]
            a[0] += fc.low * p
            a[1] += fc.expected * p
            a[2] += fc.high * p
        out = {d: (int(a[0]), int(a[1]), int(a[2])) for d, a in acc.items() if d > today and d <= end}
        source = "forecast"
    # Same-weekday average of the last 4 weeks where the stored forecast does not reach.
    by_wd: dict[int, list[int]] = defaultdict(list)
    for d in sorted(daily):
        if d > today - timedelta(days=28):
            by_wd[d.isoweekday()].append(sum(daily[d].values()))
    d = today + timedelta(days=1)
    while d <= end:
        if d not in out and by_wd.get(d.isoweekday()):
            vals = by_wd[d.isoweekday()]
            avg = Decimal(sum(vals)) / len(vals)
            out[d] = (int(avg * (1 - FALLBACK_BAND)), int(avg), int(avg * (1 + FALLBACK_BAND)))
        d += timedelta(days=1)
    return out, actual, method_share, history, source


def _txn_kind(t: Any) -> str:
    return str(t.matched_type or (t.meta or {}).get("kind") or "")


async def _regular_spend(s: AsyncSession, business_id: uuid.UUID, last_bank_date: date,
                         obligations: list[ObligationIn]) -> dict[int, int]:
    """Usual outflows by weekday over the last 4 weeks, leaving out obligations, card fees and own transfers."""
    from app.models.finance_master import BankTransaction

    since = last_bank_date - timedelta(days=PATTERN_WEEKS * 7 - 1)
    txns = (await s.execute(select(BankTransaction).where(BankTransaction.business_id == business_id,
                                                          BankTransaction.date >= since,
                                                          BankTransaction.date <= last_bank_date))).scalars().all()
    if not txns:
        return {}
    days_seen = (last_bank_date - min(t.date for t in txns)).days + 1
    weeks = max(1.0, days_seen / 7)
    ob_amounts = {o.amount_minor for o in obligations}
    excluded = {"obligation", "card_fee", "transfer", "cash_deposit_out", "cash_deposit_in"}
    out: dict[int, int] = defaultdict(int)
    for t in txns:
        amt = t.amount.amount_minor
        if amt >= 0 or _txn_kind(t) in excluded or -amt in ob_amounts:
            continue
        out[t.date.isoweekday()] += -amt
    return {wd: int(v / weeks) for wd, v in out.items()}


def late_score(paid: list[Any], stored: float) -> float:
    """Share of the customer's settled invoices paid more than 3 days late (the stored score when no history)."""
    rows = [r for r in paid if r.paid_on is not None]
    if not rows:
        return stored
    late = sum(1 for r in rows if r.paid_on is not None and (r.paid_on - r.due_date).days > 3)
    return round((late / len(rows) + stored) / 2 if stored else late / len(rows), 3)


async def load_inputs(s: AsyncSession, business_id: uuid.UUID, today: date, horizon: int = 30) -> tuple[Inputs, position.Freshness]:
    from app.core import settings_store
    from app.core.country_profiles import calendar_for
    from app.models.books import PayableInvoice, ReceivableInvoice
    from app.models.cash import PlanAction, ShortfallPlan
    from app.models.finance_master import Obligation
    from app.models.master import Supplier
    from app.models.purchasing import PurchaseOrder
    from app.models.tenancy import Business

    business = await s.get(Business, business_id)
    assert business is not None
    cfg = await settings_store.get_all(business_id, s)
    cal = calendar_for(business)
    fresh = await position.freshness(s, business_id, today, cal, int(cfg["stale_bank_days"]))
    last = fresh.last_bank_date or today
    balances = await position.account_balances(s, business_id, last)
    start = last + timedelta(days=1)
    end = today + timedelta(days=horizon)
    sales, actual, share, history, source = await _sales_inputs(s, business_id, start, end, today)

    open_invoices = [r for r in (await s.execute(select(ReceivableInvoice).where(
        ReceivableInvoice.business_id == business_id,
        ReceivableInvoice.status.in_(("open", "partially_paid"))))).scalars()
        if r.total.amount_minor - r.amount_paid_minor > 0]
    # all owing customers' history in one query
    paid_by_customer: dict[str, list[Any]] = {}
    customers = {r.customer_name for r in open_invoices}
    if customers:
        for p in (await s.execute(select(ReceivableInvoice).where(ReceivableInvoice.business_id == business_id,
                                                                  ReceivableInvoice.customer_name.in_(customers),
                                                                  ReceivableInvoice.status == "paid"))).scalars():
            paid_by_customer.setdefault(p.customer_name, []).append(p)
    receivables = [
        Receivable(str(r.id), r.number, r.customer_name, r.total.amount_minor - r.amount_paid_minor, r.due_date,
                   late_score(paid_by_customer.get(r.customer_name, []), r.late_payment_history_score))
        for r in open_invoices
    ]

    suppliers = {sp.id: sp for sp in (await s.execute(select(Supplier).where(Supplier.business_id == business_id))).scalars()}

    def terms_for(sup: Supplier | None, invoice_date: date, due: date | None) -> pay.Terms:
        days = sup.payment_terms_days if sup else 30
        return pay.Terms(invoice_date, due or invoice_date + timedelta(days=days),
                         sup.early_payment_discount_percent if sup else None, sup.early_payment_days if sup else None)

    payables = []
    for inv in (await s.execute(select(PayableInvoice).where(PayableInvoice.business_id == business_id,
                                                             PayableInvoice.status.in_(("posted", "held"))))).scalars():
        owed = inv.total.amount_minor - inv.amount_paid_minor
        if owed <= 0:
            continue
        if inv.status == "held" and inv.purchase_order_id:
            continue  # invoice.held: keep counting the order's amount until the owner resolves it
        sup = suppliers.get(inv.supplier_id) if inv.supplier_id else None
        payables.append(Payable(str(inv.id), f"{sup.name_en if sup else 'supplier'} {inv.invoice_number}", owed,
                                terms_for(sup, inv.invoice_date, inv.due_date),
                                str(inv.purchase_order_id) if inv.purchase_order_id else None,
                                str(inv.supplier_id) if inv.supplier_id else None, inv.invoice_number))
    pos = []
    for po in (await s.execute(select(PurchaseOrder).where(
            PurchaseOrder.business_id == business_id,
            PurchaseOrder.status.in_(("draft", "pending_approval", "approved", "sent", "partially_received", "received"))))).scalars():
        sup = suppliers.get(po.supplier_id)
        po_terms = terms_for(sup, po.expected_date, None)
        if po.status == "received" and po_terms.due_date < today:
            continue  # delivered and past its terms without an invoice: assume it was settled
        pos.append(OpenPO(str(po.id), po.number, f"{sup.name_en if sup else 'supplier'} {po.number}", po.total.amount_minor,
                          po_terms, po.is_critical_order, po.status))
    obligations = [ObligationIn(str(o.id), o.type, o.description, o.amount.amount_minor, o.next_due_date, o.recurrence)
                   for o in (await s.execute(select(Obligation).where(Obligation.business_id == business_id,
                                                                      Obligation.is_confirmed.is_(True)))).scalars()]
    regular = await _regular_spend(s, business_id, last, obligations)
    # Plan actions the owner accepted keep applying until their flows are gone.
    accepted = (await s.execute(select(PlanAction).join(ShortfallPlan, ShortfallPlan.id == PlanAction.plan_id).where(
        PlanAction.business_id == business_id, PlanAction.status == "accepted"))).scalars().all()
    adjustments = [a for pa in accepted for a in (pa.adjustments or []) if "add" not in a]
    from app.agents.accountant.matching import CARD_FEE_PERCENT

    inp = Inputs(today=today, start=start, end=end, currency=business.currency,
                 opening_minor=sum(b.balance_minor for b in balances), buffer_minor=business.min_cash_buffer.amount_minor,
                 sales=sales, actual_sales=actual, method_share=share, card_fee_percent=Decimal(str(CARD_FEE_PERCENT)),
                 receivables=receivables, payables=payables, pos=pos, obligations=obligations, regular_spend=regular,
                 history_daily_sales=history, adjustments=adjustments, low_confidence=fresh.low_confidence,
                 sales_source=source)
    return inp, fresh
