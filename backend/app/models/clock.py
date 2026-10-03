"""Persisted business clock."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, Enum, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import Base, TenantMixin


class BusinessClock(TenantMixin, Base):
    __tablename__ = "business_clock"
    __table_args__ = (UniqueConstraint("business_id"),)

    mode: Mapped[str] = mapped_column(Enum("real", "simulated", native_enum=False), default="simulated")
    current_date: Mapped[date] = mapped_column(Date)
    last_run_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    advancing: Mapped[bool] = mapped_column(Boolean, default=False)
