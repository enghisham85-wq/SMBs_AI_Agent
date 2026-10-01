"""Profit and loss and balance sheet from journal lines (FR-035). Quarantined entries are excluded."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import select

from app.db.engine import read_session
from app.models.books import JournalEntry, JournalLine
from app.models.finance_master import Account


async def _balances(business_id: uuid.UUID, start: date | None, end: date) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, Account]]:
    async with read_session() as s:
        accts = {a.id: a for a in (await s.execute(select(Account).where(Account.business_id == business_id))).scalars()}
        q = (select(JournalLine, JournalEntry.date).join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
             .where(JournalEntry.business_id == business_id, JournalEntry.status != "quarantined", JournalEntry.date <= end))
        if start is not None:
            q = q.where(JournalEntry.date >= start)
        rows = (await s.execute(q)).all()
    bal: dict[uuid.UUID, int] = defaultdict(int)
    for line, _d in rows:
        bal[line.account_id] += line.debit_minor - line.credit_minor
    return bal, accts


def _section(bal: dict[uuid.UUID, int], accts: dict[uuid.UUID, Account], kind: str, sign: int) -> list[dict[str, Any]]:
    return sorted(
        ({"code": a.code, "name_en": a.name_en, "name_ar": a.name_ar, "amount_minor": sign * bal[aid]}
         for aid, a in accts.items() if a.type == kind and bal.get(aid)),
        key=lambda r: r["code"])


async def pnl(business_id: uuid.UUID, start: date, end: date) -> dict[str, Any]:
    bal, accts = await _balances(business_id, start, end)
    income = _section(bal, accts, "income", -1)
    expense = _section(bal, accts, "expense", 1)
    ti, te = sum(r["amount_minor"] for r in income), sum(r["amount_minor"] for r in expense)
    cogs = sum(r["amount_minor"] for r in expense if r["code"] == "5000")
    return {"from": start, "to": end, "income": income, "expenses": expense, "total_income_minor": ti,
            "total_expenses_minor": te, "gross_profit_minor": ti - cogs, "net_profit_minor": ti - te}


async def balance_sheet(business_id: uuid.UUID, as_of: date) -> dict[str, Any]:
    bal, accts = await _balances(business_id, None, as_of)
    assets = _section(bal, accts, "asset", 1)
    liabilities = _section(bal, accts, "liability", -1)
    equity = _section(bal, accts, "equity", -1)
    earnings = -sum(v for aid, v in bal.items() if accts[aid].type in ("income", "expense"))
    ta = sum(r["amount_minor"] for r in assets)
    tl = sum(r["amount_minor"] for r in liabilities)
    te = sum(r["amount_minor"] for r in equity) + earnings
    return {"as_of": as_of, "assets": assets, "liabilities": liabilities, "equity": equity,
            "current_earnings_minor": earnings, "total_assets_minor": ta, "total_liabilities_minor": tl,
            "total_equity_minor": te, "balanced": ta == tl + te}
