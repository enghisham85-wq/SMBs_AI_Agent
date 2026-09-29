"""Business, users and per-business settings (data-model.md §1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import Base, Money, TenantMixin, _clock_now, money_col, new_uuid

ROLES = ("staff", "manager", "owner")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}


class Business(Base):
    __tablename__ = "business"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200))
    # ISO 3166 code selecting a country profile (default Egypt). Values below are copied from it.
    country: Mapped[str] = mapped_column(String(2), default="EG")
    # ISO 4217; changeable only while no financial record exists (no FX conversion in the MVP).
    currency: Mapped[str] = mapped_column(String(3), default="EGP")
    vat_registered: Mapped[bool] = mapped_column(Boolean, default=True)
    # Stored and shown as a percentage: 14.00 = 14 %.
    vat_rate_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("14.00"))
    vat_period: Mapped[str] = mapped_column(Enum("monthly", "quarterly", native_enum=False), default="monthly")
    # ISO weekday numbers, Mon=1 ... Sun=7. Egypt: Friday and Saturday.
    weekend_days: Mapped[list[int]] = mapped_column(JSON, default=lambda: [5, 6])
    tax_id_pattern: Mapped[str] = mapped_column(String(100), default=r"^\d{9}$")
    default_date_format: Mapped[str] = mapped_column(String(3), default="DMY")
    min_cash_buffer: Mapped[Money] = money_col("min_cash_buffer")
    demo_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_clock_now)


class User(TenantMixin, Base):
    __tablename__ = "user"
    __table_args__ = (UniqueConstraint("business_id", "username"),)

    username: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(Enum(*ROLES, native_enum=False))
    language: Mapped[str] = mapped_column(Enum("en", "ar", native_enum=False), default="en")
    # One Telegram chat <-> one user.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    # One-time code, expires 15 minutes after issue.
    telegram_link_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    telegram_link_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


SETTING_DEFAULTS: dict[str, Any] = {
    "price_change_pct": 15,
    "stock_variance_pct": 5,
    "approval_timeout_hours": 4,
    # minor units of the business currency; filled from the country profile (EG: EGP 10,000.00)
    "journal_value_limit": 1_000_000,
    "stale_bank_days": 1,
    "dead_stock_days": 21,
    "po_auto_approve_limit": 0,  # 0 = off
    "reminder_auto_approve": "off",  # off | polite_only
    "manual_bookkeeping_hours_per_week": 6,
    # Stock Agent: a product's 7-day forecast error above this switches it to the safer method.
    "forecast_mape_threshold": 0.35,
    "confidence_high": 0.90,
    "confidence_low": 0.60,
    # Cash-Flow Agent: yesterday's projected vs actual closing balance beyond this % is investigated.
    "cash_variance_pct": 10,
}


class Setting(TenantMixin, Base):
    __tablename__ = "setting"
    __table_args__ = (UniqueConstraint("business_id", "key"),)

    key: Mapped[str] = mapped_column(String(80))
    value: Mapped[Any] = mapped_column(JSON)


class FileRef(TenantMixin, Base):
    """An uploaded file stored on disk under FILES_DIR, named by its sha256."""

    __tablename__ = "file_ref"

    sha256: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(String(500))
    mime: Mapped[str] = mapped_column(String(100))
    original_name: Mapped[str] = mapped_column(String(255))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("user.id"), nullable=True)
