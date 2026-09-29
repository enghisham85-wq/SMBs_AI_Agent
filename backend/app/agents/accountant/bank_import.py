"""Bank statement CSV import (T087, FR-048: statements only, never bank credentials).

Columns (renameable through `mapping`): date, description, amount (signed) or debit/credit,
balance, reference, currency. Lines already known for the account (same date, amount,
description and reference) are skipped. Each statement date gets a closing-balance snapshot.
After import the reconciliation runs, which publishes `customer_payment.received` for every
receivable it finds paid.
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import InvalidOperation
from typing import Any

from sqlalchemy import delete, select

from app.agents.stock.sales_import import _parse_date
from app.core import clock
from app.core.i18n import normalize_digits
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.action_spec import ActionContext, ActionSpec, VerifyOutcome, register
from app.models.finance_master import BankAccount, BankBalanceSnapshot, BankTransaction
from app.models.tenancy import Business

DEFAULT_MAPPING = {"date": "date", "description": "description", "amount": "amount", "debit": "debit",
                   "credit": "credit", "balance": "balance", "reference": "reference", "currency": "currency"}


class StatementError(ValueError):
    def __init__(self, code: str, message_en: str, message_ar: str) -> None:
        super().__init__(message_en)
        self.code, self.message_en, self.message_ar = code, message_en, message_ar


@dataclass
class ParsedStatement:
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    sha256: str = ""
    has_balance: bool = False


def _money(text: str, currency: str) -> Money | None:
    t = normalize_digits(text or "").replace(",", "").replace(" ", "").strip()
    if not t:
        return None
    negative = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    m = Money.from_decimal(t, currency)
    return -m if negative else m


def parse(data: bytes, currency: str, date_format: str, mapping: dict[str, str] | None = None) -> ParsedStatement:
    mapping = {**DEFAULT_MAPPING, **{k: v.strip().lower() for k, v in (mapping or {}).items()}}
    out = ParsedStatement(sha256=hashlib.sha256(data).hexdigest())
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    headers = {h.strip().lower() for h in reader.fieldnames or []}
    if mapping["date"] not in headers or mapping["description"] not in headers:
        raise StatementError("missing_columns", "The file needs at least a date and a description column.",
                             "يحتاج الملف إلى عمود للتاريخ وعمود للوصف على الأقل.")
    if mapping["amount"] not in headers and not ({mapping["debit"], mapping["credit"]} & headers):
        raise StatementError("missing_columns", "The file needs an amount column, or debit and credit columns.",
                             "يحتاج الملف إلى عمود للمبلغ أو عمودي مدين ودائن.")
    out.has_balance = mapping["balance"] in headers
    today = clock.today()
    mismatched = 0
    for n, raw in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if not any(row.values()):
            continue
        cur = row.get(mapping["currency"], "").upper()
        if cur and cur != currency:
            out.errors.append({"row": n, "reason": f"currency {cur} does not match the account currency {currency}"})
            mismatched += 1
            continue
        try:
            d = _parse_date(row.get(mapping["date"], ""), date_format)
        except (ValueError, TypeError):
            out.errors.append({"row": n, "reason": "invalid date"})
            continue
        if d > today:
            out.errors.append({"row": n, "reason": "date is in the future"})
            continue
        try:
            if row.get(mapping["amount"]):
                amount = _money(row[mapping["amount"]], currency)
            else:
                debit = _money(row.get(mapping["debit"], ""), currency)
                credit = _money(row.get(mapping["credit"], ""), currency)
                amount = (credit or Money.zero(currency)) - (debit or Money.zero(currency)) if (debit or credit) else None
            balance = _money(row.get(mapping["balance"], ""), currency) if out.has_balance else None
        except (InvalidOperation, ValueError):
            out.errors.append({"row": n, "reason": "invalid amount"})
            continue
        if amount is None or amount.amount_minor == 0:
            out.errors.append({"row": n, "reason": "no amount"})
            continue
        out.rows.append({"row": n, "date": d.isoformat(), "description": row.get(mapping["description"], "")[:300],
                         "amount_minor": amount.amount_minor, "reference": row.get(mapping["reference"]) or None,
                         "balance_minor": balance.amount_minor if balance else None})
    if mismatched and not out.rows:
        raise StatementError("currency_mismatch", f"The statement is not in {currency}.", f"كشف الحساب ليس بعملة {currency}.")
    return out


async def default_account(business_id: uuid.UUID) -> BankAccount | None:
    async with read_session() as s:
        return (await s.execute(select(BankAccount).where(BankAccount.business_id == business_id,
                                                          BankAccount.is_cash_on_hand.is_(False))
                                .order_by(BankAccount.created_at).limit(1))).scalar_one_or_none()


async def prepare(business_id: uuid.UUID, data: bytes, account_id: uuid.UUID | None,
                  mapping: dict[str, str] | None) -> tuple[dict[str, Any], ParsedStatement]:
    """Parse a statement into inputs for the `import_bank_statement` action."""
    async with read_session() as s:
        business = await s.get(Business, business_id)
        assert business is not None
        acct = await s.get(BankAccount, account_id) if account_id else None
    acct = acct or await default_account(business_id)
    if acct is None or acct.business_id != business_id:
        raise StatementError("no_account", "No bank account to import into.", "لا يوجد حساب بنكي للاستيراد إليه.")
    parsed = parse(data, acct.currency, business.default_date_format, mapping)
    inputs = {"account_id": str(acct.id), "batch_id": f"stmt:{parsed.sha256[:16]}", "rows": parsed.rows,
              "has_balance": parsed.has_balance}
    return inputs, parsed


# ------------------------------------------------------------------ import_bank_statement action
def _key(d: date, amount: int, description: str, ref: str | None) -> tuple[date, int, str, str]:
    return d, amount, description.strip().lower(), (ref or "").strip().lower()


async def _balance_before(s: Any, account_id: uuid.UUID, d: date) -> int:
    snap = (await s.execute(select(BankBalanceSnapshot).where(BankBalanceSnapshot.account_id == account_id,
                                                              BankBalanceSnapshot.as_of < datetime.combine(d, time.min))
                            .order_by(BankBalanceSnapshot.as_of.desc()).limit(1))).scalar_one_or_none()
    base = snap.balance.amount_minor if snap else 0
    since = snap.as_of.date() + timedelta(days=1) if snap else date.min
    rows = (await s.execute(select(BankTransaction).where(BankTransaction.account_id == account_id,
                                                          BankTransaction.date >= since, BankTransaction.date < d))).scalars().all()
    return int(base + sum(r.amount.amount_minor for r in rows))


async def _execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    account_id = uuid.UUID(inputs["account_id"])
    batch = inputs["batch_id"]
    async with write_session() as s:
        acct = await s.get(BankAccount, account_id)
        assert acct is not None
        dates = sorted({date.fromisoformat(r["date"]) for r in inputs["rows"]})
        existing = set()
        if dates:
            for t in (await s.execute(select(BankTransaction).where(BankTransaction.account_id == account_id,
                                                                    BankTransaction.date >= dates[0],
                                                                    BankTransaction.date <= dates[-1]))).scalars():
                existing.add(_key(t.date, t.amount.amount_minor, t.description, t.external_ref))
        created, skipped = [], 0
        for r in inputs["rows"]:
            k = _key(date.fromisoformat(r["date"]), int(r["amount_minor"]), r["description"], r.get("reference"))
            if k in existing:
                skipped += 1
                continue
            existing.add(k)
            t = BankTransaction(business_id=ctx.business_id, account_id=account_id, date=k[0],
                                amount=Money(int(r["amount_minor"]), acct.currency), description=r["description"],
                                external_ref=r.get("reference"), import_batch_id=batch, meta={"statement": True})
            s.add(t)
            created.append(t)
        await s.flush()
        # A closing balance per statement date: the file's own balance column, else the running total.
        snapshots: list[dict[str, Any]] = []
        for d in dates:
            given = [r["balance_minor"] for r in inputs["rows"] if r["date"] == d.isoformat() and r.get("balance_minor") is not None]
            closing = given[-1] if given else await _balance_before(s, account_id, d + timedelta(days=1))
            as_of = datetime.combine(d, time(23, 59))
            snap = (await s.execute(select(BankBalanceSnapshot).where(BankBalanceSnapshot.account_id == account_id,
                                                                      BankBalanceSnapshot.as_of == as_of))).scalar_one_or_none()
            if snap is None:
                snap = BankBalanceSnapshot(business_id=ctx.business_id, account_id=account_id, as_of=as_of,
                                           balance=Money(int(closing), acct.currency))
                s.add(snap)
                await s.flush()
                snapshots.append({"id": str(snap.id), "previous": None})
            else:
                snapshots.append({"id": str(snap.id), "previous": snap.balance.amount_minor})
                snap.balance = Money(int(closing), acct.currency)
        return {"imported": len(created), "skipped_duplicates": skipped, "txn_ids": [str(t.id) for t in created],
                "snapshots": snapshots, "dates": [d.isoformat() for d in dates],
                "total_minor": sum(t.amount.amount_minor for t in created)}


async def _verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    ids = [uuid.UUID(i) for i in result.get("txn_ids", [])]
    async with read_session() as s:
        rows = (await s.execute(select(BankTransaction).where(BankTransaction.id.in_(ids)))).scalars().all() if ids else []
    total = sum(r.amount.amount_minor for r in rows)
    ok = len(rows) == result["imported"] and total == result["total_minor"]
    return VerifyOutcome(ok, {"rows_in_db": len(rows), "expected": result["imported"], "total_minor": total})


async def _compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    async with write_session() as s:
        ids = [uuid.UUID(i) for i in result.get("txn_ids", [])]
        if ids:
            await s.execute(delete(BankTransaction).where(BankTransaction.id.in_(ids)))
        for sn in result.get("snapshots", []):
            row = await s.get(BankBalanceSnapshot, uuid.UUID(sn["id"]))
            if row is None:
                continue
            if sn["previous"] is None:
                await s.delete(row)
            else:
                row.balance = Money(int(sn["previous"]), row.balance.currency)


async def _finalize(ctx: ActionContext, inputs: dict[str, Any], outcome: str, result: dict[str, Any]) -> None:
    """Match the new lines straight away (a paid receivable publishes customer_payment.received)."""
    if outcome != "completed" or not result.get("imported"):
        return
    from app.agents.accountant.graphs import reconcile

    await reconcile(ctx.business_id, clock.today())


def register_spec() -> None:
    register(ActionSpec(name="import_bank_statement", agent="accountant", risk_class="reversible", execute=_execute,
                        title_en="Import bank statement", title_ar="استيراد كشف حساب بنكي", verify=_verify,
                        compensate=_compensate, on_finalize=_finalize))
