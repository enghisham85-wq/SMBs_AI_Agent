"""Double-entry journal builders. Entries always balance and are never edited; reversals post the opposite."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.books import JournalEntry, JournalLine
from app.models.finance_master import Account

AR, INVENTORY, VAT_IN, CARD_TRANSIT = "1100", "1200", "1300", "1050"
AP, VAT_OUT, EQUITY = "2000", "2100", "3000"
SALES, COGS, BANK_CHARGES = "4000", "5000", "5700"
OBLIGATION_ACCOUNTS = {"rent": "5200", "salary": "5300", "utility": "5400", "subscription": "5500",
                       "loan": "2200", "tax": "2100", "other": "3000"}


class UnbalancedEntryError(ValueError):
    pass


@dataclass
class Draft:
    date: date
    reference_type: str
    reference_id: str | None
    memo: str
    lines: list[tuple[str, int, int, str]] = field(default_factory=list)  # (account code, debit, credit, memo)

    def dr(self, code: str, minor: int, memo: str = "") -> Draft:
        if minor:
            self.lines.append((code, minor, 0, memo) if minor > 0 else (code, 0, -minor, memo))
        return self

    def cr(self, code: str, minor: int, memo: str = "") -> Draft:
        if minor:
            self.lines.append((code, 0, minor, memo) if minor > 0 else (code, -minor, 0, memo))
        return self

    @property
    def balanced(self) -> bool:
        dr = sum(line[1] for line in self.lines)
        return dr == sum(line[2] for line in self.lines) and dr > 0


def assert_balanced(d: Draft) -> None:
    if not d.balanced:
        raise UnbalancedEntryError(f"{d.reference_type}: debits {sum(x[1] for x in d.lines)} != credits {sum(x[2] for x in d.lines)}")


async def accounts(s: AsyncSession, business_id: uuid.UUID) -> dict[str, Account]:
    return {a.code: a for a in (await s.execute(select(Account).where(Account.business_id == business_id))).scalars()}


async def post(s: AsyncSession, business_id: uuid.UUID, d: Draft, currency: str, *, action_id: uuid.UUID | None = None,
               created_by: str = "accountant") -> JournalEntry:
    assert_balanced(d)
    accts = await accounts(s, business_id)
    entry = JournalEntry(business_id=business_id, date=d.date, reference_type=d.reference_type,
                         reference_id=d.reference_id, memo=d.memo, created_by=created_by, status="posted",
                         action_id=action_id, currency=currency)
    s.add(entry)
    await s.flush()
    for code, dr, cr, memo in d.lines:
        if code not in accts:
            raise KeyError(f"account {code} is not in the chart of accounts")
        s.add(JournalLine(business_id=business_id, entry_id=entry.id, account_id=accts[code].id,
                          debit_minor=dr, credit_minor=cr, memo=memo[:200]))
    return entry


async def reverse(s: AsyncSession, entry: JournalEntry, *, action_id: uuid.UUID | None = None, memo: str = "") -> JournalEntry:
    lines = (await s.execute(select(JournalLine).where(JournalLine.entry_id == entry.id))).scalars().all()
    rev = JournalEntry(business_id=entry.business_id, date=entry.date, reference_type=entry.reference_type,
                       reference_id=entry.reference_id, memo=memo or f"Reversal of: {entry.memo}", created_by="accountant",
                       status="posted", reverses_id=entry.id, action_id=action_id, currency=entry.currency)
    s.add(rev)
    await s.flush()
    for ln in lines:
        s.add(JournalLine(business_id=entry.business_id, entry_id=rev.id, account_id=ln.account_id,
                          debit_minor=ln.credit_minor, credit_minor=ln.debit_minor, memo=ln.memo))
    entry.status = "reversed"
    return rev


def payable_invoice(inv_date: date, ref: str, supplier: str, lines: list[dict], vat_minor: int, total_minor: int) -> Draft:
    """Stock lines -> Inventory; other lines -> their classified expense account; VAT input; AP."""
    d = Draft(inv_date, "payable_invoice", ref, f"Supplier invoice {supplier}")
    net_total = 0
    for ln in lines:
        code = INVENTORY if ln.get("item_id") else (ln.get("account_code") or "5900")
        d.dr(code, ln["line_total_minor"], ln.get("description", ""))
        net_total += ln["line_total_minor"]
    d.dr(VAT_IN, vat_minor, "VAT input")
    # rounding difference vs the printed total goes to VAT input
    diff = total_minor - net_total - vat_minor
    if diff:
        d.dr(VAT_IN, diff, "rounding")
    d.cr(AP, total_minor, f"payable to {supplier}")
    return d


def vat_split(gross_minor: int, rate_percent: Decimal) -> tuple[int, int]:
    """(net, vat) for a VAT-inclusive amount."""
    net = int((Decimal(gross_minor) * 100 / (100 + rate_percent)).quantize(Decimal(1), ROUND_HALF_UP))
    return net, gross_minor - net


def sales_summary(day: date, by_method: dict[str, int], vat_rate: Decimal, cogs_minor: int) -> Draft:
    """Till sales (VAT-inclusive) by payment method, plus cost of goods sold."""
    d = Draft(day, "sales_summary", day.isoformat(), f"Sales {day.isoformat()}")
    gross = sum(by_method.values())
    for method, minor in sorted(by_method.items()):
        code = {"cash": "1000", "card": CARD_TRANSIT, "transfer": CARD_TRANSIT, "credit": AR}[method]
        d.dr(code, minor, f"{method} sales")
    net, vat = vat_split(gross, vat_rate)
    d.cr(SALES, net, "sales")
    d.cr(VAT_OUT, vat, "VAT output")
    if cogs_minor:
        d.dr(COGS, cogs_minor, "cost of goods sold").cr(INVENTORY, cogs_minor, "stock used")
    return d


def supplier_payment(day: date, ref: str, supplier: str, amount_minor: int, bank_code: str) -> Draft:
    return Draft(day, "supplier_payment", ref, f"Payment to {supplier}").dr(AP, amount_minor).cr(bank_code, amount_minor)


def settlement(day: date, ref: str, amount_minor: int, bank_code: str) -> Draft:
    return Draft(day, "card_settlement", ref, "Card/transfer settlement").dr(bank_code, amount_minor).cr(CARD_TRANSIT, amount_minor)


def bank_expense(day: date, ref: str, memo: str, account_code: str, amount_minor: int, bank_code: str) -> Draft:
    return Draft(day, "bank_expense", ref, memo).dr(account_code, amount_minor).cr(bank_code, amount_minor)


def transfer(day: date, ref: str, from_code: str, to_code: str, amount_minor: int) -> Draft:
    return Draft(day, "transfer", ref, "Transfer between accounts").dr(to_code, amount_minor).cr(from_code, amount_minor)


def receivable_invoice(inv_date: date, ref: str, customer: str, subtotal_minor: int, vat_minor: int) -> Draft:
    return (Draft(inv_date, "receivable_invoice", ref, f"Invoice to {customer}")
            .dr(AR, subtotal_minor + vat_minor).cr(SALES, subtotal_minor).cr(VAT_OUT, vat_minor))


def customer_payment(day: date, ref: str, customer: str, amount_minor: int, bank_code: str) -> Draft:
    return Draft(day, "customer_payment", ref, f"Payment from {customer}").dr(bank_code, amount_minor).cr(AR, amount_minor)


def opening_balance(day: date, balances: dict[str, int]) -> Draft:
    d = Draft(day, "opening_balance", day.isoformat(), "Opening balances")
    for code, minor in balances.items():
        d.dr(code, minor)
    d.cr(EQUITY, sum(balances.values()), "owner's equity")
    return d
