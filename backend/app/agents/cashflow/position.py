"""Cash position: balances per account, cash on hand and bank-data freshness (FR-023, FR-031)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.country_profiles import Calendar
from app.models.finance_master import BankAccount, BankBalanceSnapshot, BankTransaction

GAP_WINDOW_DAYS = 7


@dataclass
class AccountBalance:
    account_id: uuid.UUID
    name: str
    is_cash_on_hand: bool
    balance_minor: int
    as_of: datetime | None  # latest snapshot, or None if the account has no statement yet
    last_date: date | None  # last date with bank data (snapshot or transaction)


@dataclass
class Freshness:
    bank_data_as_of: datetime | None
    last_bank_date: date | None
    age_business_days: int
    stale: bool
    missing_dates: list[date] = field(default_factory=list)

    @property
    def low_confidence(self) -> bool:
        return self.stale or bool(self.missing_dates)

    def reason_en(self) -> str | None:
        if self.last_bank_date is None:
            return "no bank data yet"
        if self.stale:
            return f"bank data is from {self.last_bank_date.isoformat()} ({self.age_business_days} business days old)"
        if self.missing_dates:
            return "bank data missing for " + ", ".join(d.isoformat() for d in self.missing_dates)
        return None


async def account_balances(s: AsyncSession, business_id: uuid.UUID, up_to: date) -> list[AccountBalance]:
    """Closing balance per account at the end of `up_to`: latest snapshot plus later transactions."""
    out: list[AccountBalance] = []
    end = datetime.combine(up_to, time.max)
    for acct in (await s.execute(select(BankAccount).where(BankAccount.business_id == business_id)
                                 .order_by(BankAccount.is_cash_on_hand, BankAccount.name))).scalars():
        snap = (await s.execute(select(BankBalanceSnapshot).where(BankBalanceSnapshot.account_id == acct.id,
                                                                  BankBalanceSnapshot.as_of <= end)
                                .order_by(BankBalanceSnapshot.as_of.desc()).limit(1))).scalar_one_or_none()
        base = snap.balance.amount_minor if snap else 0
        since = snap.as_of.date() + timedelta(days=1) if snap else date.min
        txns = (await s.execute(select(BankTransaction).where(BankTransaction.account_id == acct.id,
                                                              BankTransaction.date >= since,
                                                              BankTransaction.date <= up_to))).scalars().all()
        last_txn = max((t.date for t in txns), default=None)
        last = max(d for d in (snap.as_of.date() if snap else None, last_txn) if d is not None) if (snap or last_txn) else None
        out.append(AccountBalance(acct.id, acct.name, acct.is_cash_on_hand, base + sum(t.amount.amount_minor for t in txns),
                                  snap.as_of if snap else None, last))
    return out


def business_days_between(cal: Calendar, after: date, up_to: date) -> int:
    """Business days in (after, up_to]."""
    n, d = 0, after + timedelta(days=1)
    while d <= up_to:
        if not cal.is_weekend(d) and not cal.is_holiday(d):
            n += 1
        d += timedelta(days=1)
    return n


async def freshness(s: AsyncSession, business_id: uuid.UUID, today: date, cal: Calendar, stale_days: int) -> Freshness:
    """Bank data older than `stale_days` business days is stale; a skipped day in the last week is a gap."""
    banks = [a for a in (await s.execute(select(BankAccount).where(BankAccount.business_id == business_id,
                                                                    BankAccount.is_cash_on_hand.is_(False)))).scalars()]
    ids = [a.id for a in banks]
    if not ids:
        return Freshness(None, None, 0, True)
    snaps = (await s.execute(select(BankBalanceSnapshot).where(
        BankBalanceSnapshot.account_id.in_(ids),
        BankBalanceSnapshot.as_of <= datetime.combine(today, time.max)))).scalars().all()
    if not snaps:
        return Freshness(None, None, 0, True)
    latest = max(sn.as_of for sn in snaps)
    last_date = latest.date()
    age = business_days_between(cal, last_date, today)
    first = min(sn.as_of.date() for sn in snaps)
    have = {sn.as_of.date() for sn in snaps}
    missing = []
    d = max(first, today - timedelta(days=GAP_WINDOW_DAYS))
    while d < last_date:
        if d not in have:
            missing.append(d)
        d += timedelta(days=1)
    return Freshness(latest, last_date, age, age > stale_days, missing)


def summary(balances: list[AccountBalance]) -> dict[str, Any]:
    total = sum(b.balance_minor for b in balances)
    cash = sum(b.balance_minor for b in balances if b.is_cash_on_hand)
    return {"total_minor": total, "cash_on_hand_minor": cash, "bank_minor": total - cash}
