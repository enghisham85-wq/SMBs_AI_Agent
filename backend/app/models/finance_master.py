"""Chart of accounts, bank data, sales and obligations (data-model.md §3)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Enum, Float, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import Base, Money, TenantMixin, money_col

PAYMENT_METHODS = ("cash", "card", "transfer", "credit")
MATCH_STATUSES = ("unmatched", "suggested", "auto_matched", "confirmed", "excluded")


class Account(TenantMixin, Base):
    __tablename__ = "account"

    code: Mapped[str] = mapped_column(String(20), index=True)
    name_en: Mapped[str] = mapped_column(String(200))
    name_ar: Mapped[str] = mapped_column(String(200), default="")
    type: Mapped[str] = mapped_column(
        Enum("asset", "liability", "equity", "income", "expense", native_enum=False)
    )
    is_bank: Mapped[bool] = mapped_column(Boolean, default=False)
    is_inventory: Mapped[bool] = mapped_column(Boolean, default=False)
    is_vat_input: Mapped[bool] = mapped_column(Boolean, default=False)
    is_vat_output: Mapped[bool] = mapped_column(Boolean, default=False)


class BankAccount(TenantMixin, Base):
    __tablename__ = "bank_account"

    name: Mapped[str] = mapped_column(String(120))
    bank: Mapped[str] = mapped_column(String(120), default="")
    currency: Mapped[str] = mapped_column(String(3))
    is_cash_on_hand: Mapped[bool] = mapped_column(Boolean, default=False)
    ledger_account_code: Mapped[str] = mapped_column(String(20))


class BankTransaction(TenantMixin, Base):
    __tablename__ = "bank_transaction"

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bank_account.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Money] = money_col("amount")  # signed: + in, - out
    description: Mapped[str] = mapped_column(String(300))
    external_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    import_batch_id: Mapped[str] = mapped_column(String(64))
    match_status: Mapped[str] = mapped_column(Enum(*MATCH_STATUSES, native_enum=False), default="unmatched")
    matched_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    matched_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # auto | suggested_confirmed | manual (FR-046: where each match came from)
    match_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Demo-feed metadata used by the generator (e.g. kind=card_settlement, customer ref).
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class BankBalanceSnapshot(TenantMixin, Base):
    __tablename__ = "bank_balance_snapshot"

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bank_account.id"), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime, index=True)
    balance: Mapped[Money] = money_col("balance")


class Sale(TenantMixin, Base):
    __tablename__ = "sale"

    date: Mapped[date] = mapped_column(Date, index=True)
    # [{sold_item_id, qty, amount_minor}]
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    amount_total: Mapped[Money] = money_col("amount_total")
    payment_method: Mapped[str] = mapped_column(Enum(*PAYMENT_METHODS, native_enum=False))
    source: Mapped[str] = mapped_column(String(20))  # seed | csv_upload | manual
    import_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stock_applied: Mapped[bool] = mapped_column(Boolean, default=False)


class Obligation(TenantMixin, Base):
    __tablename__ = "obligation"

    type: Mapped[str] = mapped_column(
        Enum("rent", "salary", "loan", "tax", "utility", "subscription", "other", native_enum=False)
    )
    description: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Money] = money_col("amount")
    next_due_date: Mapped[date] = mapped_column(Date)
    recurrence: Mapped[str] = mapped_column(Enum("monthly", "quarterly", "annual", "once", native_enum=False))
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_transaction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    bank_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("bank_account.id"), nullable=True)
