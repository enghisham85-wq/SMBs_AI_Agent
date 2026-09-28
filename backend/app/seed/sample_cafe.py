"""Sample cafe seed (FR-052, T043): an Egyptian cafe by default, any country profile via --country.

3 months of sales and bank history are produced by the same generator as the daily demo feed,
so history and future days follow the same patterns.
"""

from __future__ import annotations

import calendar as pycal
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete

from app.config import get_settings
from app.core import clock, country_profiles, settings_store
from app.core.auth import hash_password
from app.core.i18n import normalize_arabic_name
from app.db.engine import write_session
from app.db.types import Base, Money
from app.models.clock import BusinessClock
from app.models.finance_master import Account, BankAccount, BankBalanceSnapshot, Obligation
from app.models.master import Item, RecipeLine, Supplier, SupplierAlias, SupplierPrice
from app.models.tenancy import Business, User
from app.seed import feed

log = logging.getLogger(__name__)
SEED_DIR = Path(__file__).resolve().parent
HISTORY_DAYS = 90
PASSWORDS = {"owner": "owner-demo-2026", "manager": "manager-demo-2026", "staff": "staff-demo-2026"}

# Stories extend the seed (opening stock, sample invoices, receivables ...): fn(business_id, start_date, catalog_ids)
SeedExtension = Callable[[uuid.UUID, date, dict[str, Any]], Awaitable[None]]
SEED_EXTENSIONS: list[SeedExtension] = []


def register_extension(fn: SeedExtension) -> None:
    if fn not in SEED_EXTENSIONS:
        SEED_EXTENSIONS.append(fn)


def load_catalog() -> dict[str, Any]:
    return json.loads((SEED_DIR / "catalog.json").read_text(encoding="utf-8"))


def _first_due(day: int, start: date) -> date:
    y, m = start.year, start.month
    for _ in range(2):
        last = pycal.monthrange(y, m)[1]
        d = date(y, m, min(day, last))
        if d >= start:
            return d
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return start


async def seed(country: str | None = None, start_date: date | None = None, history_days: int = HISTORY_DAYS) -> dict[str, Any]:
    settings = get_settings()
    profile = country_profiles.load(country or settings.DEFAULT_COUNTRY)
    factor = profile.seed_price_factor
    cur = profile.currency
    start = start_date or date.today()
    catalog = load_catalog()

    def price(v: str) -> Money:
        return Money.from_decimal(Decimal(v) * factor, cur)

    bid = uuid.uuid4()
    ids: dict[str, Any] = {"suppliers": {}, "items": {}, "bank_accounts": {}, "users": {}}
    async with write_session() as s:
        b = Business(id=bid, name="Nile Corner Cafe" if profile.code == "EG" else "Corner Cafe",
                     min_cash_buffer=Money(0, cur), demo_mode=settings.DEMO_MODE)
        extra_settings = country_profiles.apply(b, profile, creating=True, vat_override=settings.DEFAULT_VAT_RATE_PERCENT)
        s.add(b)
        await s.flush()
        for k, v in extra_settings.items():
            await settings_store.put(s, bid, k, v)

        for role, pw in PASSWORDS.items():
            u = User(business_id=bid, username=role, password_hash=hash_password(pw), role=role,
                     language="ar" if role == "staff" else "en")
            s.add(u)
            await s.flush()
            ids["users"][role] = u.id

        for sp in catalog["suppliers"]:
            sup = Supplier(business_id=bid, name_en=sp["name_en"], name_ar=sp["name_ar"], vat_number=sp["vat_number"],
                           phone=sp.get("phone"), stated_lead_time_days=sp["stated_lead_time_days"],
                           observed_lead_time_days=sp["observed_lead_time_days"],
                           payment_terms_days=sp["payment_terms_days"],
                           early_payment_discount_percent=Decimal(sp["early_payment_discount_percent"])
                           if sp.get("early_payment_discount_percent") else None,
                           early_payment_days=sp.get("early_payment_days"))
            s.add(sup)
            await s.flush()
            ids["suppliers"][sp["key"]] = sup.id
            names = [(sp["name_en"], "en"), (sp["name_ar"], "ar")]
            names += [(a, "en") for a in sp.get("aliases_en", [])] + [(a, "ar") for a in sp.get("aliases_ar", [])]
            for text, lang in names:
                s.add(SupplierAlias(business_id=bid, supplier_id=sup.id, alias_text=text,
                                    normalised_text=normalize_arabic_name(text), language=lang))

        ingredient_cost: dict[str, Money] = {}
        for ing in catalog["ingredients"]:
            unit_price = price(ing["price"])
            item = Item(business_id=bid, name_en=ing["name_en"], name_ar=ing["name_ar"], unit=ing["unit"],
                        category=ing["category"], is_ingredient=True, is_sold=False,
                        shelf_life_days=ing.get("shelf_life_days"), safety_stock=Decimal(ing["safety_stock"]),
                        storage_capacity=Decimal(ing["storage_capacity"]),
                        preferred_supplier_id=ids["suppliers"][ing["supplier"]], is_critical=ing["critical"],
                        unit_cost=unit_price, sale_price=Money(0, cur), margin_class=ing["margin_class"])
            s.add(item)
            await s.flush()
            ids["items"][ing["key"]] = item.id
            ingredient_cost[ing["key"]] = unit_price
            s.add(SupplierPrice(business_id=bid, supplier_id=ids["suppliers"][ing["supplier"]], item_id=item.id,
                                pack_size=Decimal(ing["pack_size"]), unit=ing["unit"],
                                min_order_qty=Decimal(ing["min_order_qty"]), price=unit_price,
                                valid_from=start - timedelta(days=history_days + 30)))

        feed_products: dict[str, Any] = {}
        for prod in catalog["products"]:
            cost = Money(0, cur)
            for key, qty, _unit in prod["recipe"]:
                cost = cost + ingredient_cost[key].times(Decimal(qty))
            item = Item(business_id=bid, name_en=prod["name_en"], name_ar=prod["name_ar"], unit="piece",
                        category=prod["kind"], is_ingredient=False, is_sold=True, unit_cost=cost,
                        sale_price=price(prod["price"]), margin_class="high" if prod["kind"] == "drink" else "normal")
            s.add(item)
            await s.flush()
            ids["items"][prod["key"]] = item.id
            feed_products[str(item.id)] = {"base_daily": prod["base_daily"], "kind": prod["kind"], "key": prod["key"]}
            for key, qty, unit in prod["recipe"]:
                s.add(RecipeLine(business_id=bid, sold_item_id=item.id, ingredient_item_id=ids["items"][key],
                                 quantity=Decimal(qty), unit=unit))

        for acc in json.loads((SEED_DIR / "chart_of_accounts.json").read_text(encoding="utf-8")):
            s.add(Account(business_id=bid, code=acc["code"], name_en=acc["name_en"], name_ar=acc["name_ar"],
                          type=acc["type"], is_bank=acc.get("is_bank", False), is_inventory=acc.get("is_inventory", False),
                          is_vat_input=acc.get("is_vat_input", False), is_vat_output=acc.get("is_vat_output", False)))

        history_start = start - timedelta(days=history_days)
        for ba in catalog["bank_accounts"]:
            acct = BankAccount(business_id=bid, name=ba["name"], bank=ba["bank"], currency=cur,
                               is_cash_on_hand=ba["cash"], ledger_account_code=ba["ledger"])
            s.add(acct)
            await s.flush()
            ids["bank_accounts"][ba["key"]] = acct.id
            s.add(BankBalanceSnapshot(business_id=bid, account_id=acct.id,
                                      as_of=datetime.combine(history_start - timedelta(days=1), time(23, 59)),
                                      balance=price(ba["opening"])))

        for ob in catalog["obligations"]:
            s.add(Obligation(business_id=bid, type=ob["type"], description=ob["description"], amount=price(ob["amount"]),
                             next_due_date=_first_due(ob["day"], start), recurrence=ob["recurrence"],
                             bank_account_id=ids["bank_accounts"]["bank"]))

        await settings_store.put(s, bid, "demo_feed", {"products": feed_products, "prices_include_vat": True,
                                                       "customers": catalog["customers"]})
        s.add(BusinessClock(business_id=bid, mode="simulated" if settings.DEMO_MODE else "real",
                            current_date=start - timedelta(days=1), last_run_date=start - timedelta(days=1)))

    # History: the same generator the daily feed uses.
    clock.set_state("simulated", history_start)
    d = history_start
    while d < start:
        clock.set_state("simulated", d)
        await feed.feed_sales(bid, d, force=True)
        await feed.feed_bank(bid, d, force=True)
        d += timedelta(days=1)
    clock.set_state("simulated" if settings.DEMO_MODE else "real", start - timedelta(days=1))

    for ext in SEED_EXTENSIONS:
        await ext(bid, start, ids)

    return {
        "business_id": bid,
        "country": profile.code,
        "currency": cur,
        "start_date": start,
        "current_date": start - timedelta(days=1),
        "users": {role: {"username": role, "password": pw} for role, pw in PASSWORDS.items()},
    }


async def wipe() -> None:
    async with write_session() as s:
        for table in reversed(Base.metadata.sorted_tables):
            await s.execute(delete(table))


async def reset_and_seed(country: str | None = None, start_date: date | None = None) -> dict[str, Any]:
    await wipe()
    return await seed(country=country, start_date=start_date)
