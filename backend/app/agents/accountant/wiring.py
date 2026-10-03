from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.accountant import action_specs, graphs, posting, receivables
from app.config import get_settings
from app.core import settings_store
from app.db.engine import write_session
from app.models.books import ReceivableInvoice
from app.models.finance_master import BankAccount, BankBalanceSnapshot
from app.models.master import Item
from app.models.stock_ops import StockLevel
from app.models.tenancy import Business, User

SEED_RECEIVABLES = [
    # (customer, days relative to start for invoice date, days relative to start for due date, lines)
    ("Al Mazaya Offices", -40, -10, [("Office coffee service - August", 1, "6000")]),
    ("Nile Tech Hub", -18, 12, [("Catering - team breakfast", 40, "100")]),
    ("Zamalek Events", -25, 5, [("Event coffee bar", 1, "10000")]),
]


async def seed_books(business_id: uuid.UUID, start: date, ids: dict[str, Any]) -> None:
    """Opening balances at the demo start, seeded customer invoices and the sample invoice files."""
    opening_day = start - timedelta(days=1)
    async with write_session() as s:
        b = await s.get(Business, business_id)
        assert b is not None
        await settings_store.put(s, business_id, "books_start_date", start.isoformat())
        balances: dict[str, int] = {}
        for ba in (await s.execute(select(BankAccount).where(BankAccount.business_id == business_id))).scalars():
            snap = (await s.execute(select(BankBalanceSnapshot).where(
                BankBalanceSnapshot.account_id == ba.id,
                BankBalanceSnapshot.as_of <= datetime.combine(opening_day, time.max))
                .order_by(BankBalanceSnapshot.as_of.desc()).limit(1))).scalar_one_or_none()
            if snap is not None:
                balances[ba.ledger_account_code] = balances.get(ba.ledger_account_code, 0) + snap.balance.amount_minor
        items = {i.id: i for i in (await s.execute(select(Item).where(Item.business_id == business_id))).scalars()}
        inventory = 0
        for lvl in (await s.execute(select(StockLevel).where(StockLevel.business_id == business_id))).scalars():
            item = items.get(lvl.item_id)
            if item is not None and lvl.quantity > 0:
                inventory += item.unit_cost.times(lvl.quantity).amount_minor
        balances[posting.INVENTORY] = inventory
        ar_total = 0
        for customer, inv_off, due_off, lines in SEED_RECEIVABLES:
            rec = await receivables.build(s, b, {
                "customer_name": customer, "invoice_date": (start + timedelta(days=inv_off)).isoformat(),
                "due_date": (start + timedelta(days=due_off)).isoformat(), "source": "seed",
                "lines": [{"description": d, "qty": q, "unit_price": p} for d, q, p in lines]})
            s.add(rec)
            ar_total += rec.total.amount_minor
        balances[posting.AR] = ar_total
        # Negative balances (e.g. an overdrawn day) go on the credit side.
        draft = posting.Draft(opening_day, "opening_balance", opening_day.isoformat(), "Opening balances")
        for code, minor in balances.items():
            draft.dr(code, minor)
        draft.cr(posting.EQUITY, sum(balances.values()), "owner's equity")
        await posting.post(s, business_id, draft, b.currency, created_by="seed")
    try:
        from app.seed.invoices.generate import generate

        # rendering takes a few seconds, keep it off the loop
        await asyncio.to_thread(generate, Path(get_settings().SAMPLE_INVOICES_DIR), opening_day)
    except FileNotFoundError:
        pass  # no Arabic-capable font on this machine; sample invoices are optional


async def customer_payments(s: AsyncSession, business: Business, d: date) -> list[dict[str, Any]]:
    """Demo bank source: customers pay open invoices, late or on time according to their history."""
    from app.seed.feed import rng_for

    cfg = (await settings_store.get_all(business.id, s)).get("demo_feed") or {}
    prob = {c["name"]: float(c.get("on_time_probability", 0.8)) for c in cfg.get("customers", [])}
    out = []
    for rec in (await s.execute(select(ReceivableInvoice).where(ReceivableInvoice.business_id == business.id,
                                                                ReceivableInvoice.status.in_(("open", "partially_paid"))))).scalars():
        rng = rng_for(business.id, rec.due_date, f"pay:{rec.number}")
        on_time = rng.random() < prob.get(rec.customer_name, 0.8)
        pay_on = rec.due_date + timedelta(days=rng.randint(-2, 1) if on_time else rng.randint(12, 30))
        if pay_on != d:  # the feed runs once per date, so each invoice is paid once
            continue
        out.append({"amount_minor": receivables.outstanding(rec), "description": f"Transfer from {rec.customer_name} {rec.number}",
                    "kind": "customer_payment", "meta": {"receivable": rec.number}})
    return out


async def telegram_document(user: User, data: bytes, mime: str, name: str, caption: str) -> str:
    from app.agents.accountant.intake import UnsupportedFileError, submit

    try:
        res = await submit(user.business_id, data, mime, name, "telegram", user.id)
    except UnsupportedFileError:
        return "Please send a PDF or a photo." if user.language == "en" else "يرجى إرسال ملف PDF أو صورة."
    if res["outcome"] == "posted":
        return "Invoice read and posted to the books." if user.language == "en" else "تمت قراءة الفاتورة وترحيلها."
    if res["waiting_for_owner"]:
        return ("I read the invoice but need an answer first; I sent you a question." if user.language == "en"
                else "قرأت الفاتورة لكني أحتاج إجابة أولاً؛ أرسلت لك سؤالاً.")
    if res["outcome"] == "same_file_already_received":
        return "I already received this exact file." if user.language == "en" else "استلمت هذا الملف نفسه من قبل."
    return f"Status: {res['outcome']}"


def register() -> None:
    from app.agents.accountant import bank_import, handlers
    from app.approvals.telegram_bot import register_upload
    from app.seed.feed import register_bank_source
    from app.seed.sample_cafe import register_extension

    action_specs.register_specs()
    bank_import.register_spec()
    handlers.register()
    graphs.register_graphs()
    register_extension(seed_books)
    register_bank_source(customer_payments)
    register_upload("document", "manager", telegram_document)
