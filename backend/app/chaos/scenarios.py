"""The 8 Chaos mode fault injectors (FR-012, spec User Story 6).

Each injector changes data the way the fault would happen in real life, then runs the normal graph
that would see it: a supplier re-sends an invoice, a price list changes, a delivery arrives short.
There are no special detection paths; incidents, explanations and rule proposals come from the
agents themselves (research: "Injectors mutate data, then run the normal graphs"). Faults that live
in future days (demand spike, missing bank day) are written as FeedOverride rows and the business
clock is advanced until an agent notices.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.config import get_settings
from app.core import clock
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.models.books import PayableInvoice, ReceivableInvoice
from app.models.cash import PaymentReminder, PurchasingBudget, ShortfallPlan
from app.models.chaos import SCENARIOS, ChaosInjection
from app.models.events import FeedOverride
from app.models.finance_master import BankAccount, BankTransaction
from app.models.harness import ApprovalRequest, Incident
from app.models.master import Item, Supplier, SupplierPrice
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine
from app.models.tenancy import Business


class ChaosError(Exception):
    """The scenario cannot be injected into this business as it stands (missing data, bad parameter)."""


@dataclass
class Ctx:
    business: Business
    injection_id: uuid.UUID
    params: dict[str, Any]
    seq: int  # how many injections this business has had, to keep numbers and amounts unique
    affected: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def bid(self) -> uuid.UUID:
        return self.business.id

    @property
    def tag(self) -> str:
        return f"{self.seq:03d}{self.injection_id.hex[:4].upper()}"


Injector = Callable[[Ctx], Awaitable[None]]


@dataclass(frozen=True)
class Scenario:
    key: str
    number: int
    agent: str
    title_en: str
    title_ar: str
    expected_en: str  # what the audience should see
    expected_ar: str
    params: dict[str, Any]  # defaults, shown on the panel
    inject: Injector
    incident_types: tuple[str, ...]  # what the agent should open; other incidents on the same days are unrelated
    advances_clock: bool = False


REGISTRY: dict[str, Scenario] = {}


def scenario(key: str, number: int, agent: str, title: tuple[str, str], expected: tuple[str, str],
             params: dict[str, Any] | None = None, *, incidents: tuple[str, ...],
             advances_clock: bool = False) -> Callable[[Injector], Injector]:
    def deco(fn: Injector) -> Injector:
        REGISTRY[key] = Scenario(key, number, agent, title[0], title[1], expected[0], expected[1], params or {}, fn,
                                 incidents, advances_clock)
        return fn

    return deco


def ordered() -> list[Scenario]:
    return [REGISTRY[k] for k in SCENARIOS]


# ------------------------------------------------------------------ helpers
async def _item(bid: uuid.UUID, name: str) -> Item:
    async with read_session() as s:
        item = (await s.execute(select(Item).where(Item.business_id == bid, Item.name_en == name))).scalars().first()
    if item is None:
        raise ChaosError(f"no item named {name!r}")
    return item


async def _supplier(bid: uuid.UUID, name: str) -> Supplier:
    async with read_session() as s:
        sup = (await s.execute(select(Supplier).where(Supplier.business_id == bid, Supplier.name_en == name))).scalars().first()
    if sup is None:
        raise ChaosError(f"no supplier named {name!r}")
    return sup


async def _price(supplier_id: uuid.UUID, item_id: uuid.UUID) -> SupplierPrice:
    async with read_session() as s:
        p = (await s.execute(select(SupplierPrice).where(SupplierPrice.supplier_id == supplier_id,
                                                         SupplierPrice.item_id == item_id,
                                                         SupplierPrice.valid_from <= clock.today())
                             .order_by(SupplierPrice.valid_from.desc(), SupplierPrice.created_at.desc()).limit(1))).scalars().first()
    if p is None:
        raise ChaosError("the supplier has no price for this item")
    return p


def _sample(name: str) -> Any:
    from app.seed.invoices.generate import sample_specs

    return next(sp for sp in sample_specs(clock.today()) if sp.name == name)


def _render(spec: Any) -> tuple[bytes, str, str]:
    """Render an invoice file with its ground truth (read by the offline extractor, like the samples)."""
    from app.seed.invoices.generate import render_jpg, render_pdf, truth

    out = Path(get_settings().SAMPLE_INVOICES_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{spec.name}.{spec.fmt}"
    try:
        (render_pdf if spec.fmt == "pdf" else render_jpg)(spec, path)
    except FileNotFoundError as exc:
        raise ChaosError("no Arabic-capable font is installed, so invoices cannot be rendered") from exc
    truth_name = f"{spec.name}.truth.json"
    (out / truth_name).write_text(json.dumps(truth(spec), ensure_ascii=False, indent=2), encoding="utf-8")
    data = path.read_bytes()
    index_path = out / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    index[hashlib.sha256(data).hexdigest()] = truth_name
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return data, "application/pdf" if spec.fmt == "pdf" else "image/jpeg", path.name


async def _submit(ctx: Ctx, spec: Any) -> dict[str, Any]:
    from app.agents.accountant.intake import submit

    # Rendering (reportlab/Pillow) and its file writes are slow and blocking: keep them off the event loop.
    data, mime, name = await asyncio.to_thread(_render, spec)
    return await submit(ctx.bid, data, mime, name, "dashboard", None)


async def _override(ctx: Ctx, d: date, change: dict[str, Any]) -> None:
    """Merge a change into the demo feed override for date `d` (read by seed/feed.py when `d` is generated)."""
    async with write_session() as s:
        row = (await s.execute(select(FeedOverride).where(FeedOverride.business_id == ctx.bid,
                                                          FeedOverride.date == d))).scalar_one_or_none()
        if row is None:
            row = FeedOverride(business_id=ctx.bid, date=d, overrides={})
            s.add(row)
        merged = dict(row.overrides or {})
        for k, v in change.items():
            merged[k] = {**(merged.get(k) or {}), **v} if isinstance(v, dict) else v
        row.overrides = merged
        row.chaos_injection_id = ctx.injection_id
    ctx.affected.setdefault("feed_override_dates", []).append(d.isoformat())


async def linked_incidents(injection_id: uuid.UUID, types: tuple[str, ...] | None = None) -> list[Incident]:
    async with read_session() as s:
        q = select(Incident).where(Incident.chaos_injection_id == injection_id)
        if types:
            q = q.where(Incident.type.in_(types))
        return list((await s.execute(q.order_by(Incident.created_at))).scalars())


async def advance_until_detected(ctx: Ctx, max_days: int, types: tuple[str, ...]) -> None:
    """Run the normal daily work one business date at a time until an agent opens the expected incident."""
    from app.core import scheduler

    days = []
    for _ in range(max_days):
        days += [d.isoformat() for d in await scheduler.advance(ctx.bid, days=1)]
        if await linked_incidents(ctx.injection_id, types):
            break
    ctx.affected["days_run"] = days


def _qty_for(price: SupplierPrice, wanted: Decimal) -> Decimal:
    qty = max(wanted, price.min_order_qty)
    packs = (qty / price.pack_size).to_integral_value(rounding=ROUND_CEILING)
    return packs * price.pack_size


# ------------------------------------------------------------------ 1
@scenario("duplicate_invoice", 1, "accountant",
          ("Duplicate supplier invoice", "فاتورة مورد مكررة"),
          ("Second posting blocked; the payment is counted once in the cash forecast.",
           "منع الترحيل الثاني؛ الدفعة محسوبة مرة واحدة في التوقع النقدي."),
          {"supplier_sample": "bi_packaging"}, incidents=("duplicate_invoice",))
async def duplicate_invoice(ctx: Ctx) -> None:
    base = _sample(ctx.params.get("supplier_sample", "bi_packaging"))
    lines = [dataclasses.replace(base.lines[0], qty=base.lines[0].qty + ctx.seq), *base.lines[1:]]
    original = dataclasses.replace(base, name=f"chaos_{ctx.tag}_original", number=f"{base.number.split('-')[0]}-C{ctx.tag}",
                                   lines=lines, fmt="pdf", expected=[])
    first = await _submit(ctx, original)
    # The supplier sends the same invoice again, this time as a phone photo.
    second = await _submit(ctx, dataclasses.replace(original, name=f"chaos_{ctx.tag}_copy", fmt="jpg"))
    ctx.affected.update({"invoice_number": original.number, "original_document_id": first["document_id"],
                         "duplicate_document_id": second["document_id"]})
    async with read_session() as s:
        posted = (await s.execute(select(func.count()).select_from(PayableInvoice).where(
            PayableInvoice.business_id == ctx.bid, PayableInvoice.invoice_number == original.number,
            PayableInvoice.status == "posted"))).scalar_one()
    ctx.evidence.update({"original_outcome": first["outcome"], "duplicate_issues": second["issues"],
                         "posted_copies": posted})


# ------------------------------------------------------------------ 2
@scenario("price_spike", 2, "stock",
          ("Supplier price spike", "ارتفاع مفاجئ في سعر المورد"),
          ("Purchase order held, owner asked, another supplier offered.",
           "إيقاف أمر الشراء وسؤال المالك واقتراح مورد بديل."),
          {"item": "Milk", "increase_pct": 25}, incidents=("price_sanity",))
async def price_spike(ctx: Ctx) -> None:
    item = await _item(ctx.bid, ctx.params.get("item", "Milk"))
    if item.preferred_supplier_id is None:
        raise ChaosError(f"{item.name_en} has no preferred supplier")
    current = await _price(item.preferred_supplier_id, item.id)
    pct = Decimal(str(ctx.params.get("increase_pct", 25)))
    new_minor = int((Decimal(current.price.amount_minor) * (1 + pct / 100)).to_integral_value())
    async with write_session() as s:  # the supplier's new price list arrives
        s.add(SupplierPrice(business_id=ctx.bid, supplier_id=current.supplier_id, item_id=item.id, pack_size=current.pack_size,
                            unit=current.unit, min_order_qty=current.min_order_qty,
                            price=Money(new_minor, current.price.currency), valid_from=clock.today()))
    qty = _qty_for(current, current.min_order_qty)
    sup = await _supplier_by_id(current.supplier_id)
    inputs = {"supplier_id": str(current.supplier_id),
              "lines": [{"item_id": str(item.id), "qty": str(qty), "unit": current.unit, "pack_size": str(current.pack_size),
                         "unit_price_minor": new_minor}],
              "expected_date": (clock.today() + timedelta(days=max(1, sup.stated_lead_time_days))).isoformat(),
              "projected_stockout": None, "reason": f"{item.name_en} regular order", "is_critical": item.is_critical,
              "forecast_generated_on": None, "deferred": []}
    from app.harness.graph import run_action

    out = await run_action("draft_po", inputs, ctx.bid)
    async with read_session() as s:
        po = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.created_by_action_id == out["action_id"]))).scalars().first()
        asks = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.action_id == out["action_id"],
                                                              ApprovalRequest.status == "pending"))).scalars().all()
    ctx.affected.update({"item_id": str(item.id), "action_id": str(out["action_id"]), "po_id": str(po.id) if po else None,
                         "old_price_minor": current.price.amount_minor, "new_price_minor": new_minor})
    ctx.evidence.update({"po_status": po.status if po else None,
                         "alternative_offered": any(o["key"] == "alternative" for a in asks for o in a.options)})


async def _supplier_by_id(supplier_id: uuid.UUID) -> Supplier:
    async with read_session() as s:
        sup = await s.get(Supplier, supplier_id)
    assert sup is not None
    return sup


# ------------------------------------------------------------------ 3
def _misreadable_date(today: date) -> date:
    """A recent date whose day/month, printed the other way round, reads as a date in the future."""
    for k in range(1, 120):
        d = today - timedelta(days=k)
        if d.day > 12 or d.day == d.month:
            continue
        misread = date(d.year, d.day, d.month)  # printed MM/DD but read as DD/MM (or the reverse)
        if misread > today:
            return d
    raise ChaosError("no recent date can be misread as a future date; try again later in the year")


@scenario("date_format", 3, "accountant",
          ("Wrong date format on invoice", "صيغة تاريخ خاطئة في الفاتورة"),
          ("Impossible date detected and corrected; a supplier date-format rule is proposed (or applied).",
           "اكتشاف التاريخ المستحيل وتصحيحه؛ واقتراح قاعدة لصيغة تاريخ المورد (أو تطبيقها)."),
          {"supplier_sample": "en_produce"}, incidents=("date_format",))
async def date_format(ctx: Ctx) -> None:
    base = _sample(ctx.params.get("supplier_sample", "en_produce"))
    default = ctx.business.default_date_format
    true_date = _misreadable_date(clock.today())
    # The supplier prints the date in the other order from the country default.
    printed = (f"{true_date.month:02d}/{true_date.day:02d}/{true_date.year}" if default == "DMY"
               else f"{true_date.day:02d}/{true_date.month:02d}/{true_date.year}")
    lines = [dataclasses.replace(base.lines[0], qty=base.lines[0].qty + ctx.seq), *base.lines[1:]]
    spec = dataclasses.replace(base, name=f"chaos_{ctx.tag}_date", number=f"{base.number.split('-')[0]}-C{ctx.tag}",
                               invoice_date=true_date, date_printed=printed, date_format_observed="ambiguous",
                               lines=lines, expected=[])
    res = await _submit(ctx, spec)
    inv = None
    if res.get("invoice_id"):
        async with read_session() as s:
            inv = await s.get(PayableInvoice, uuid.UUID(res["invoice_id"]))
    ctx.affected.update({"document_id": res["document_id"], "invoice_id": res.get("invoice_id"), "printed_date": printed})
    ctx.evidence.update({"true_date": true_date.isoformat(), "read_as": inv.invoice_date.isoformat() if inv else None,
                         "outcome": res["outcome"], "issues": res["issues"]})


# ------------------------------------------------------------------ 4
@scenario("demand_spike", 4, "stock",
          ("Sudden demand spike", "ارتفاع مفاجئ في الطلب"),
          ("Forecast error detected; confidence lowered, method switched, safety stock raised, owner told.",
           "اكتشاف خطأ التوقع؛ خفض الثقة وتغيير الطريقة ورفع مخزون الأمان وإبلاغ المالك."),
          {"item": "Latte", "multiplier": 3, "days": 2}, incidents=("forecast_accuracy",), advances_clock=True)
async def demand_spike(ctx: Ctx) -> None:
    item = await _item(ctx.bid, ctx.params.get("item", "Latte"))
    if not item.is_sold:
        raise ChaosError(f"{item.name_en} is not a sold product")
    days = max(1, min(5, int(ctx.params.get("days", 2))))
    mult = float(ctx.params.get("multiplier", 3))
    for i in range(1, days + 1):  # e.g. an event nearby for the next two days
        await _override(ctx, clock.today() + timedelta(days=i), {"sales_multiplier": {str(item.id): mult}})
    await advance_until_detected(ctx, days + 2, ("forecast_accuracy",))
    after = await _item(ctx.bid, item.name_en)
    ctx.affected["item_id"] = str(item.id)
    ctx.evidence.update({"forecast_method_before": item.forecast_method, "forecast_method_after": after.forecast_method})


# ------------------------------------------------------------------ 5
@scenario("paid_before_reminder", 5, "cashflow",
          ("Customer pays before reminder", "العميل يدفع قبل التذكير"),
          ("The pre-send check sees the payment and cancels the reminder.",
           "فحص ما قبل الإرسال يرى الدفعة ويلغي التذكير."),
          {"customer": None}, incidents=("invoice_still_unpaid",))
async def paid_before_reminder(ctx: Ctx) -> None:
    from app.agents.cashflow.graphs import step_reminders

    today = clock.today()
    inv_id, used = await _free_invoice(ctx)
    if inv_id is None:
        inv_id, used = await _overdue_invoice(ctx), set()
    async with write_session() as s:
        inv = await s.get(ReceivableInvoice, inv_id)
        assert inv is not None
        level = min(lv for lv in (1, 2, 3) if lv not in used)
        owed = Money(inv.total.amount_minor - inv.amount_paid_minor, inv.total.currency)
        # A reminder is due this morning ...
        rem = PaymentReminder(business_id=ctx.bid, receivable_invoice_id=inv.id, level=level, scheduled_for=today,
                              status="scheduled", text_en=f"Reminder: invoice {inv.number} ({owed.to_display()}) is due.",
                              text_ar=f"تذكير: الفاتورة {inv.number} ({owed.to_display('ar')}) مستحقة.")
        s.add(rem)
        # ... and the customer's transfer landed in the bank overnight, not yet matched by the Accountant.
        bank = (await s.execute(select(BankAccount).where(BankAccount.business_id == ctx.bid,
                                                          BankAccount.is_cash_on_hand.is_(False)).limit(1))).scalars().first()
        if bank is None:
            raise ChaosError("the business has no bank account")
        s.add(BankTransaction(business_id=ctx.bid, account_id=bank.id, date=today, amount=owed,
                              description=f"Transfer from {inv.customer_name} {inv.number}",
                              import_batch_id=f"chaos:{ctx.tag}", meta={"chaos": True}))
        await s.flush()
        rid, inv_id, customer = rem.id, inv.id, inv.customer_name
    await step_reminders(ctx.bid, today)
    async with read_session() as s:
        status = (await s.get(PaymentReminder, rid)).status  # type: ignore[union-attr]
    ctx.affected.update({"reminder_id": str(rid), "receivable_invoice_id": str(inv_id), "customer": customer})
    ctx.evidence["reminder_status"] = status


async def _free_invoice(ctx: Ctx) -> tuple[uuid.UUID | None, set[int]]:
    """An open customer invoice with no reminder waiting and a reminder level left (its id, levels used)."""
    async with read_session() as s:
        q = select(ReceivableInvoice).where(ReceivableInvoice.business_id == ctx.bid,
                                            ReceivableInvoice.status.in_(("open", "partially_paid")))
        if ctx.params.get("customer"):
            q = q.where(ReceivableInvoice.customer_name == ctx.params["customer"])
        for cand in (await s.execute(q.order_by(ReceivableInvoice.due_date))).scalars():
            rows = (await s.execute(select(PaymentReminder).where(PaymentReminder.receivable_invoice_id == cand.id))).scalars().all()
            if not any(r.status in ("scheduled", "pending_approval") for r in rows) and len({r.level for r in rows}) < 3:
                return cand.id, {r.level for r in rows}
    return None, set()


async def _overdue_invoice(ctx: Ctx) -> uuid.UUID:
    """Every open invoice already has its reminders: bill a customer through the Accountant's normal action."""
    from app.harness.graph import run_action

    today = clock.today()
    out = await run_action("create_receivable", {
        "customer_name": str(ctx.params.get("customer") or "Zamalek Events"), "source": "manual",
        "invoice_date": (today - timedelta(days=20)).isoformat(), "due_date": (today - timedelta(days=5)).isoformat(),
        "lines": [{"description": "Catering order", "qty": 1, "unit_price": 2500 + ctx.seq}]}, ctx.bid)
    if out["outcome"] != "completed":
        raise ChaosError("could not create a customer invoice for the scenario")
    ctx.affected["created_invoice"] = out["result"]["invoice_id"]
    return uuid.UUID(out["result"]["invoice_id"])


# ------------------------------------------------------------------ 6
@scenario("missing_bank_day", 6, "cashflow",
          ("Missing bank feed day", "يوم مفقود في بيانات البنك"),
          ("Forecast marked low confidence; a statement is requested instead of guessing.",
           "وسم التوقع بأنه منخفض الثقة وطلب كشف حساب بدلاً من التخمين."),
          {"days_ahead": 1}, incidents=("bank_freshness",), advances_clock=True)
async def missing_bank_day(ctx: Ctx) -> None:
    from app.agents.cashflow.graphs import latest_run

    ahead = max(1, int(ctx.params.get("days_ahead", 1)))
    await _override(ctx, clock.today() + timedelta(days=ahead), {"skip_bank": True})
    await advance_until_detected(ctx, ahead + 2, ("bank_freshness",))
    run = await latest_run(ctx.bid)
    ctx.evidence["low_confidence_reason"] = run.low_confidence_reason if run else None


# ------------------------------------------------------------------ 7
@scenario("short_delivery", 7, "accountant",
          ("Short delivery vs full invoice", "توريد ناقص مقابل فاتورة كاملة"),
          ("The three-way match (order, delivery, invoice) holds the invoice.",
           "المطابقة الثلاثية (أمر الشراء والاستلام والفاتورة) توقف الفاتورة."),
          {"item": "Milk", "ordered": 40, "short_pct": 10}, incidents=("three_way_mismatch",))
async def short_delivery(ctx: Ctx) -> None:
    from app.agents.stock.graphs import record_delivery

    base = _sample("bi_dairy_full_qty")
    sup = await _supplier(ctx.bid, base.supplier_en)
    item = await _item(ctx.bid, ctx.params.get("item", "Milk"))
    price = await _price(sup.id, item.id)
    ordered = Decimal(str(ctx.params.get("ordered", 40)))
    short = max(Decimal(1), (ordered * Decimal(str(ctx.params.get("short_pct", 10))) / 100).to_integral_value())
    received = ordered - short
    async with write_session() as s:  # an order the owner approved earlier, already with the supplier
        po = PurchaseOrder(business_id=ctx.bid, number=f"PO-C{ctx.tag}", supplier_id=sup.id, status="sent",
                           total=price.price.times(ordered), expected_date=clock.today(), notes={"chaos": True},
                           sent_at=clock.clock_now())
        s.add(po)
        await s.flush()
        s.add(PurchaseOrderLine(business_id=ctx.bid, po_id=po.id, item_id=item.id, qty=ordered, unit=price.unit,
                                unit_price=price.price, line_total=price.price.times(ordered)))
        po_id = po.id
    # The delivery arrives short and is recorded by the Stock Agent's normal delivery graph.
    delivery = await record_delivery(ctx.bid, {"po_id": str(po_id), "lines": [{"item_id": str(item.id), "qty_received": str(received)}]})
    # The supplier invoices the full order.
    line = dataclasses.replace(base.lines[0], qty=ordered, unit_price=price.price.to_decimal())
    res = await _submit(ctx, dataclasses.replace(base, name=f"chaos_{ctx.tag}_full", number=f"AND-C{ctx.tag}",
                                                 invoice_date=clock.today(), lines=[line], expected=[]))
    status = None
    if res.get("invoice_id"):
        async with read_session() as s:
            status = (await s.get(PayableInvoice, uuid.UUID(res["invoice_id"]))).status  # type: ignore[union-attr]
    ctx.affected.update({"po_id": str(po_id), "delivery_id": delivery.get("delivery_id"), "document_id": res["document_id"],
                         "invoice_id": res.get("invoice_id"), "ordered": str(ordered), "received": str(received)})
    ctx.evidence.update({"delivery_discrepancies": delivery.get("discrepancies"), "invoice_status": status,
                         "issues": res["issues"]})


# ------------------------------------------------------------------ 8
@scenario("cash_crunch", 8, "cashflow",
          ("Cash crunch", "أزمة سيولة"),
          ("Shortfall predicted about 3 weeks ahead; purchasing budget tightened; ranked action plan presented.",
           "توقع عجز قبل نحو 3 أسابيع؛ تشديد ميزانية الشراء؛ وعرض خطة إجراءات مرتبة."),
          {"days_ahead": 21, "amount_minor": None, "description": "Equipment replacement"},
          incidents=("shortfall_predicted",))
async def cash_crunch(ctx: Ctx) -> None:
    """A large one-off payment ~3 weeks out becomes known (e.g. a quote accepted for new equipment).

    It is registered through the Cash-Flow Agent's normal obligation action, so the demo feed pays it
    when the day comes (a feed override as well would pay it twice).
    """
    from app.agents.cashflow import graphs as cash_graphs
    from app.agents.cashflow import projection
    from app.harness.graph import run_action

    today = clock.today()
    target = today + timedelta(days=max(7, int(ctx.params.get("days_ahead", 21))))
    amount = ctx.params.get("amount_minor")
    if not amount:
        async with read_session() as s:
            inp, _ = await projection.load_inputs(s, ctx.bid, today)
        expected = projection.project_all(inp)["expected"]
        closing = next((d.closing for d in expected.days if d.date == target), expected.days[-1].closing)
        # Enough to take the balance a full buffer below the minimum on that day.
        amount = max(0, closing - inp.buffer_minor) + max(inp.buffer_minor, 1)
    out = await run_action("save_obligation", {"fields": {
        "type": "other", "description": str(ctx.params.get("description") or "Equipment replacement"),
        "amount_minor": int(amount), "next_due_date": target.isoformat(), "recurrence": "once", "is_confirmed": True}}, ctx.bid)
    await cash_graphs.refresh(ctx.bid)
    async with read_session() as s:
        plan = (await s.execute(select(ShortfallPlan).where(ShortfallPlan.business_id == ctx.bid)
                                .order_by(ShortfallPlan.created_at.desc()).limit(1))).scalars().first()
        budget = (await s.execute(select(PurchasingBudget).where(PurchasingBudget.business_id == ctx.bid)
                                  .order_by(PurchasingBudget.created_at.desc()).limit(1))).scalars().first()
    ctx.affected.update({"obligation_id": (out.get("result") or {}).get("obligation_id"), "due": target.isoformat(),
                         "amount": Money(int(amount), ctx.business.currency)})
    ctx.evidence.update({"days_to_act": plan.days_to_act if plan else None, "gap_date": plan.gap_date.isoformat() if plan else None,
                         "budget_tightened": bool(budget and budget.tightened)})


async def injection_count(business_id: uuid.UUID) -> int:
    async with read_session() as s:
        return int((await s.execute(select(func.count()).select_from(ChaosInjection).where(
            ChaosInjection.business_id == business_id))).scalar_one())
