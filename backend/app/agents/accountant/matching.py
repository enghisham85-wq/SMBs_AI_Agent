"""Bank reconciliation.

Each bank line is scored against possible counterparts from amount, date proximity and name
similarity. Matches at or above the high threshold are applied automatically; between the
thresholds they are suggested for the owner; below they stay unmatched. Every match records its
source (auto, suggested_confirmed, manual) and confidence.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.accountant.posting import OBLIGATION_ACCOUNTS
from app.agents.accountant.receivables import outstanding
from app.core.i18n import normalize_arabic_name
from app.models.books import PayableInvoice, ReceivableInvoice
from app.models.finance_master import BankAccount, BankTransaction, Obligation, Sale
from app.models.master import Supplier

W_AMOUNT, W_DATE, W_NAME = 0.6, 0.25, 0.15
CARD_FEE_PERCENT = 2


@dataclass
class Candidate:
    type: str  # sales_cash | settlement | card_fee | transfer | supplier_invoice | customer_payment | obligation
    ref: str
    name: str
    amount_minor: int  # signed, as it should appear on the bank line
    on: date
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scored:
    candidate: Candidate
    score: float


def _name_score(description: str, name: str) -> float:
    a, b = normalize_arabic_name(description), normalize_arabic_name(name)
    if not a or not b:
        return 0.0
    if b in a or a in b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def score(txn: BankTransaction, c: Candidate) -> float:
    amt = txn.amount.amount_minor
    if (amt >= 0) != (c.amount_minor >= 0):
        return 0.0
    diff = abs(amt - c.amount_minor)
    amount_score = 1.0 if diff == 0 else max(0.0, 1 - diff / max(abs(c.amount_minor), 1) * 20)
    date_score = 1 - min(abs((txn.date - c.on).days), 7) / 7
    return round(W_AMOUNT * amount_score + W_DATE * date_score + W_NAME * _name_score(txn.description, c.name), 4)


async def _sales_by_method(s: AsyncSession, business_id: uuid.UUID, day: date) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in (await s.execute(select(Sale).where(Sale.business_id == business_id, Sale.date == day))).scalars():
        out[r.payment_method] = out.get(r.payment_method, 0) + r.amount_total.amount_minor
    return out


def _obligation_due_near(ob: Obligation, day: date) -> date | None:
    for delta in range(-3, 4):
        d = day + timedelta(days=delta)
        if d.day == ob.next_due_date.day or (ob.recurrence == "once" and d == ob.next_due_date):
            return d
    return None


async def candidates(s: AsyncSession, txn: BankTransaction, account: BankAccount) -> list[Candidate]:
    bid = txn.business_id
    out: list[Candidate] = []
    amt = txn.amount.amount_minor
    if account.is_cash_on_hand and amt > 0:
        cash = (await _sales_by_method(s, bid, txn.date)).get("cash", 0)
        if cash:
            out.append(Candidate("sales_cash", f"sales:{txn.date}", "cash sales", cash, txn.date))
    if not account.is_cash_on_hand and amt > 0:
        for back in (1, 2, 3):
            day = txn.date - timedelta(days=back)
            by = await _sales_by_method(s, bid, day)
            for method in ("card", "transfer"):
                if by.get(method):
                    label = "card settlement" if method == "card" else "instapay transfers"
                    out.append(Candidate("settlement", f"{method}:{day}", f"{label} {day.strftime('%d/%m')}", by[method],
                                         txn.date, {"method": method, "sales_date": day.isoformat()}))
        for inv in (await s.execute(select(ReceivableInvoice).where(ReceivableInvoice.business_id == bid,
                                                                    ReceivableInvoice.status.in_(("open", "partially_paid"))))).scalars():
            out.append(Candidate("customer_payment", str(inv.id), inv.customer_name, outstanding(inv), txn.date,
                                 {"number": inv.number}))
    if amt < 0:
        for back in (0, 1, 2):
            by = await _sales_by_method(s, bid, txn.date - timedelta(days=back + 1))
            if by.get("card"):
                fee = -round(by["card"] * CARD_FEE_PERCENT / 100)
                out.append(Candidate("card_fee", f"fee:{txn.date - timedelta(days=back + 1)}", "card processing fee", fee, txn.date))
        suppliers = {sp.id: sp for sp in (await s.execute(select(Supplier).where(Supplier.business_id == bid))).scalars()}
        for inv in (await s.execute(select(PayableInvoice).where(PayableInvoice.business_id == bid,
                                                                 PayableInvoice.status == "posted"))).scalars():
            sp = suppliers.get(inv.supplier_id)
            owed = inv.total.amount_minor - inv.amount_paid_minor
            out.append(Candidate("supplier_invoice", str(inv.id), f"payment to {sp.name_en if sp else ''}", -owed,
                                 inv.due_date or txn.date, {"number": inv.invoice_number, "supplier_ar": sp.name_ar if sp else ""}))
        for ob in (await s.execute(select(Obligation).where(Obligation.business_id == bid))).scalars():
            due = _obligation_due_near(ob, txn.date)
            if due is not None:
                out.append(Candidate("obligation", str(ob.id), f"{ob.type} {ob.description}", -ob.amount.amount_minor, due,
                                     {"account_code": OBLIGATION_ACCOUNTS.get(ob.type, "5900")}))
    # Transfers: an equal and opposite line in another account on the same day.
    others = (await s.execute(select(BankTransaction).where(BankTransaction.business_id == bid, BankTransaction.date == txn.date,
                                                            BankTransaction.account_id != txn.account_id,
                                                            BankTransaction.match_status.in_(("unmatched", "suggested"))))).scalars()
    for o in others:
        if o.amount.amount_minor == -amt:
            out.append(Candidate("transfer", str(o.id), "cash deposit" if amt > 0 else "transfer out", amt, txn.date,
                                 {"counterpart_id": str(o.id)}))
    return out


def best(txn: BankTransaction, cands: list[Candidate]) -> Scored | None:
    scored = sorted((Scored(c, score(txn, c)) for c in cands), key=lambda x: -x.score)
    return scored[0] if scored else None


def describe(candidate: Candidate) -> str:
    return re.sub(r"\s+", " ", f"{candidate.type}: {candidate.name}").strip()
