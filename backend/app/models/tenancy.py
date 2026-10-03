"""Business, users and per-business settings."""

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
    # picks the country profile the fields below are copied from
    country: Mapped[str] = mapped_column(String(2), default="EG")
    # locked once money has been recorded, since there's no FX
    currency: Mapped[str] = mapped_column(String(3), default="EGP")
    vat_registered: Mapped[bool] = mapped_column(Boolean, default=True)
    vat_rate_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("14.00"))
    vat_period: Mapped[str] = mapped_column(Enum("monthly", "quarterly", native_enum=False), default="monthly")
    # ISO weekdays, Mon=1
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
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    # one-time, expires after 15 min
    telegram_link_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    telegram_link_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


SETTING_DEFAULTS: dict[str, Any] = {
    "price_change_pct": 15,
    "stock_variance_pct": 5,
    "approval_timeout_hours": 4,
    # minor units; filled in from the country profile
    "journal_value_limit": 1_000_000,
    "stale_bank_days": 1,
    "dead_stock_days": 21,
    "po_auto_approve_limit": 0,  # 0 = off
    "reminder_auto_approve": "off",  # off | polite_only
    "manual_bookkeeping_hours_per_week": 6,
    # 7-day forecast error that switches a product to the safer method
    "forecast_mape_threshold": 0.35,
    "confidence_high": 0.90,
    "confidence_low": 0.60,
    # % gap between projected and actual closing balance worth investigating
    "cash_variance_pct": 10,
    # daily failed-action share that marks an agent degraded
    "action_failure_threshold": 0.2,
}


class Setting(TenantMixin, Base):
    __tablename__ = "setting"
    __table_args__ = (UniqueConstraint("business_id", "key"),)

    key: Mapped[str] = mapped_column(String(80))
    value: Mapped[Any] = mapped_column(JSON)


class FileRef(TenantMixin, Base):
    """An uploaded file under FILES_DIR, named by sha256."""

    __tablename__ = "file_ref"

    sha256: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(String(500))
    mime: Mapped[str] = mapped_column(String(100))
    original_name: Mapped[str] = mapped_column(String(255))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("user.id"), nullable=True)
