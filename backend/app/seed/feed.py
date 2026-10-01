"""Simulated daily data feed for demo mode (T044, research R10 "Demo data feed").

For each business date the clock moves into, it generates that day's sales and bank activity from
the same seasonal model used to create the 3-month history. The random seed is derived from
business id + date, so the same date always produces the same data, and re-running a date is a
no-op. Chaos scenarios change future days through FeedOverride rows.
"""

from __future__ import annotations

import calendar as pycal
import hashlib
import random
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import settings_store
from app.core.country_profiles import Calendar, calendar_for
from app.db.engine import write_session
from app.db.types import Money
from app.models.events import FeedOverride
from app.models.finance_master import BankAccount, BankBalanceSnapshot, BankTransaction, Obligation, Sale
from app.models.master import Item, RecipeLine, SupplierPrice
from app.models.tenancy import Business

PAYMENT_SPLIT = (("cash", Decimal("0.55")), ("card", Decimal("0.40")), ("transfer", Decimal("0.05")))
CARD_FEE_PERCENT = Decimal("2")
CASH_FLOAT = Decimal("3000")

# Extension points: stories add real payables / receivables payments to the simulated bank.
BankSource = Callable[[AsyncSession, Business, date], Awaitable[list[dict[str, Any]]]]
BANK_SOURCES: list[BankSource] = []
# When a payables source is registered, the weekly consumption-based supplier payment stops.
state: dict[str, bool] = {"payables_source": False}


def register_bank_source(fn: BankSource, *, replaces_supplier_payments: bool = False) -> None:
    if fn not in BANK_SOURCES:
        BANK_SOURCES.append(fn)
    if replaces_supplier_payments:
        state["payables_source"] = True


def rng_for(business_id: uuid.UUID, d: date, stream: str) -> random.Random:
    digest = hashlib.sha256(f"{business_id}:{d.isoformat()}:{stream}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def demand_factor(cal: Calendar, d: date, kind: str) -> float:
    f = 1.0
    if cal.is_weekend(d):
        f *= 1.30
    elif cal.is_weekend(d + timedelta(days=1)):
        f *= 1.12  # the evening before the weekend is busy
    if cal.is_holiday(d):
        f *= 1.25
    if cal.is_ramadan(d):
        f *= 1.15 if kind == "drink" else 0.85  # busy after iftar for drinks, less food by day
    return f


@dataclass
class DayFeed:
    sales: list[dict[str, Any]] = field(default_factory=list)
    bank: list[dict[str, Any]] = field(default_factory=list)


async def _feed_config(s: AsyncSession, business_id: uuid.UUID) -> dict[str, Any]:
    cfg = await settings_store.get_all(business_id, s)
    return dict(cfg.get("demo_feed") or {})


async def _override(s: AsyncSession, business_id: uuid.UUID, d: date) -> dict[str, Any]:
    row = (await s.execute(select(FeedOverride).where(FeedOverride.business_id == business_id,
                                                      FeedOverride.date == d))).scalar_one_or_none()
    return dict(row.overrides) if row else {}


def generate_sales(business: Business, products: list[Item], cfg: dict[str, Any], d: date,
                   overrides: dict[str, Any]) -> list[dict[str, Any]]:
    cal = calendar_for(business)
    rng = rng_for(business.id, d, "sales")
    multipliers = {str(k): float(v) for k, v in (overrides.get("sales_multiplier") or {}).items()}
    per_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in sorted(products, key=lambda i: i.name_en):
        meta = cfg.get("products", {}).get(str(item.id))
        if not meta:
            continue
        noise = min(1.5, max(0.6, rng.gauss(1.0, 0.12)))
        qty = int(round(meta["base_daily"] * demand_factor(cal, d, meta["kind"]) * noise * multipliers.get(str(item.id), 1.0)))
        if qty <= 0:
            continue
        remaining = qty
        for i, (method, share) in enumerate(PAYMENT_SPLIT):
            q = remaining if i == len(PAYMENT_SPLIT) - 1 else int(round(qty * share))
            q = min(q, remaining)
            remaining -= q
            if q > 0:
                per_method[method].append({"sold_item_id": str(item.id), "qty": q,
                                           "amount_minor": item.sale_price.amount_minor * q})
    sales = []
    for method, lines in per_method.items():
        total = sum(line["amount_minor"] for line in lines)
        sales.append({"date": d, "lines": lines, "amount_total": Money(total, business.currency),
                      "payment_method": method, "source": "seed", "import_batch_id": f"feed:{d.isoformat()}"})
    return sales


async def feed_sales(business_id: uuid.UUID, d: date, *, force: bool = False) -> int:
    """Daily step `import_sales` (demo mode only): insert the day's generated sales once."""
    if not (force or get_settings().DEMO_MODE):
        return 0
    async with write_session() as s:
        existing = (await s.execute(select(Sale.source).where(Sale.business_id == business_id, Sale.date == d))).scalars().all()
        if existing:  # already fed, or real sales were uploaded/entered for this date
            return 0
        business = await s.get(Business, business_id)
        assert business is not None
        products = list((await s.execute(select(Item).where(Item.business_id == business_id, Item.is_sold.is_(True)))).scalars())
        cfg = await _feed_config(s, business_id)
        rows = generate_sales(business, products, cfg, d, await _override(s, business_id, d))
        for r in rows:
            s.add(Sale(business_id=business_id, **r))
    return len(rows)


def _due_on(ob: Obligation, d: date) -> bool:
    anchor = ob.next_due_date
    last = pycal.monthrange(d.year, d.month)[1]
    same_day = d.day == min(anchor.day, last)
    months = (d.year - anchor.year) * 12 + (d.month - anchor.month)
    if ob.recurrence == "monthly":
        return same_day
    if ob.recurrence == "quarterly":
        return same_day and months % 3 == 0
    if ob.recurrence == "annual":
        return same_day and d.month == anchor.month
    return d == anchor


async def _sales_total(s: AsyncSession, business_id: uuid.UUID, d: date, method: str) -> int:
    rows = (await s.execute(select(Sale).where(Sale.business_id == business_id, Sale.date == d,
                                               Sale.payment_method == method))).scalars().all()
    return sum(r.amount_total.amount_minor for r in rows)


async def _weekly_supplier_costs(s: AsyncSession, business: Business, d: date) -> dict[uuid.UUID, int]:
    """Cost of ingredients consumed in the 7 days before d, grouped by supplier (history fallback)."""
    start = d - timedelta(days=7)
    sales = (await s.execute(select(Sale).where(Sale.business_id == business.id, Sale.date >= start, Sale.date < d))).scalars().all()
    sold: dict[str, Decimal] = defaultdict(Decimal)
    for sale in sales:
        for line in sale.lines:
            sold[line["sold_item_id"]] += Decimal(line["qty"])
    recipes = (await s.execute(select(RecipeLine).where(RecipeLine.business_id == business.id))).scalars().all()
    used: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for r in recipes:
        used[r.ingredient_item_id] += sold.get(str(r.sold_item_id), Decimal(0)) * r.quantity
    items = {i.id: i for i in (await s.execute(select(Item).where(Item.business_id == business.id))).scalars()}
    prices = (await s.execute(select(SupplierPrice).where(SupplierPrice.business_id == business.id))).scalars().all()
    price_for = {p.item_id: p for p in prices}
    costs: dict[uuid.UUID, int] = defaultdict(int)
    for item_id, qty in used.items():
        p = price_for.get(item_id)
        item = items.get(item_id)
        if p is None or item is None or item.preferred_supplier_id is None:
            continue
        costs[item.preferred_supplier_id] += p.price.times(qty).amount_minor
    return costs


async def generate_bank(s: AsyncSession, business: Business, d: date, overrides: dict[str, Any]) -> list[dict[str, Any]]:
    accounts = {("cash" if a.is_cash_on_hand else "bank"): a for a in
                (await s.execute(select(BankAccount).where(BankAccount.business_id == business.id))).scalars()}
    if "bank" not in accounts or "cash" not in accounts:
        return []
    cur = business.currency
    batch = f"feed:{d.isoformat()}"
    txns: list[dict[str, Any]] = []

    def add(acct: str, minor: int, desc: str, kind: str, **meta: Any) -> None:
        if minor == 0:
            return
        txns.append({"account_id": accounts[acct].id, "date": d, "amount": Money(minor, cur), "description": desc,
                     "import_batch_id": batch, "external_ref": f"{kind}:{d.isoformat()}:{len(txns)}",
                     "meta": {"feed": True, "kind": kind, **meta}})

    # Cash sales go into the till the same day.
    add("cash", await _sales_total(s, business.id, d, "cash"), "Cash sales", "cash_sales")
    # Card sales settle the next day, less the processing fee.
    prev = d - timedelta(days=1)
    card = await _sales_total(s, business.id, prev, "card")
    if card:
        add("bank", card, f"Card settlement {prev.strftime('%d/%m')}", "card_settlement", sales_date=prev.isoformat())
        fee = Money(card, cur).percent(CARD_FEE_PERCENT).amount_minor
        add("bank", -fee, f"Card processing fee {prev.strftime('%d/%m')}", "card_fee")
    transfer = await _sales_total(s, business.id, prev, "transfer")
    if transfer:
        add("bank", transfer, f"Instapay transfers {prev.strftime('%d/%m')}", "transfer_sales", sales_date=prev.isoformat())
    # Every third day the till is banked down to a float.
    if d.toordinal() % 3 == 0:
        till = await _balance_before(s, accounts["cash"], d) + sum(t["amount"].amount_minor for t in txns if t["account_id"] == accounts["cash"].id)
        deposit = till - Money.from_decimal(CASH_FLOAT, cur).amount_minor
        if deposit > 0:
            add("cash", -deposit, "Cash deposit to bank", "cash_deposit_out")
            add("bank", deposit, "Cash deposit", "cash_deposit_in")
    # Recurring obligations (rent, salaries, utilities, subscriptions).
    obligations = (await s.execute(select(Obligation).where(Obligation.business_id == business.id))).scalars().all()
    for ob in obligations:
        if _due_on(ob, d):
            add("bank", -ob.amount.amount_minor, f"{ob.type.title()} - {ob.description}", "obligation",
                obligation_id=str(ob.id), obligation_type=ob.type)
    # Supplier payments: from payables once the Accountant story provides them; else weekly by consumption.
    if not state["payables_source"] and d.isoweekday() == 7:
        from app.models.master import Supplier

        names = {sp.id: sp.name_en for sp in (await s.execute(select(Supplier).where(Supplier.business_id == business.id))).scalars()}
        for supplier_id, minor in sorted((await _weekly_supplier_costs(s, business, d)).items(), key=lambda kv: str(kv[0])):
            add("bank", -minor, f"Payment to {names.get(supplier_id, 'supplier')}", "supplier_payment",
                supplier_id=str(supplier_id))
    for source in BANK_SOURCES:
        for extra in await source(s, business, d):
            add(extra.get("account", "bank"), int(extra["amount_minor"]), extra["description"], extra["kind"],
                **extra.get("meta", {}))
    if overrides.get("extra_outflow_minor"):
        add("bank", -int(overrides["extra_outflow_minor"]), overrides.get("extra_outflow_description", "Unplanned equipment repair"),
            "extra_outflow")
    return txns


async def _balance_before(s: AsyncSession, account: BankAccount, d: date) -> int:
    snap = (await s.execute(select(BankBalanceSnapshot).where(BankBalanceSnapshot.account_id == account.id,
                                                              BankBalanceSnapshot.as_of < datetime.combine(d, time.min))
                            .order_by(BankBalanceSnapshot.as_of.desc()).limit(1))).scalar_one_or_none()
    base = snap.balance.amount_minor if snap else 0
    since = snap.as_of.date() + timedelta(days=1) if snap else date.min
    # Include any unsnapshotted transactions (e.g. a skipped feed day that was later uploaded).
    rows = (await s.execute(select(BankTransaction).where(BankTransaction.account_id == account.id,
                                                          BankTransaction.date >= since,
                                                          BankTransaction.date < d))).scalars().all()
    return base + sum(r.amount.amount_minor for r in rows)


async def feed_bank(business_id: uuid.UUID, d: date, *, force: bool = False) -> int:
    """Daily step `bank_import` (demo mode only): the day's bank lines and closing balances."""
    if not (force or get_settings().DEMO_MODE):
        return 0
    async with write_session() as s:
        business = await s.get(Business, business_id)
        assert business is not None
        overrides = await _override(s, business_id, d)
        if overrides.get("skip_bank"):
            return 0  # the missing bank feed day (Chaos scenario 6)
        done = (await s.execute(select(BankTransaction.id).where(BankTransaction.business_id == business_id,
                                                                  BankTransaction.import_batch_id == f"feed:{d.isoformat()}")
                                .limit(1))).first()
        if done:
            return 0
        rows = await generate_bank(s, business, d, overrides)
        for r in rows:
            s.add(BankTransaction(business_id=business_id, **r))
        await s.flush()
        for acct in (await s.execute(select(BankAccount).where(BankAccount.business_id == business_id))).scalars():
            closing = await _balance_before(s, acct, d + timedelta(days=1))
            s.add(BankBalanceSnapshot(business_id=business_id, account_id=acct.id,
                                      as_of=datetime.combine(d, time(23, 59)), balance=Money(closing, business.currency)))
    return len(rows)


def register() -> None:
    from app.graphs.daily_run import register_step

    register_step("import_sales", "demo_feed_sales", feed_sales)
    register_step("bank_import", "demo_feed_bank", feed_bank)
