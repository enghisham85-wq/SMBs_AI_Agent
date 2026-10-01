"""Stock levels, movements, counts and demand forecasts (data-model.md §2). Written by the Stock Agent."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import Base, TenantMixin
from app.models.master import QTY

MOVEMENT_TYPES = ("sale", "purchase", "waste", "spoilage", "adjustment", "transfer", "count_correction")
REASON_REQUIRED = ("waste", "spoilage", "adjustment", "count_correction")


class StockLevel(TenantMixin, Base):
    __tablename__ = "stock_level"
    __table_args__ = (UniqueConstraint("item_id", "location"),)

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id"), index=True)
    location: Mapped[str] = mapped_column(String(40), default="main")
    quantity: Mapped[Decimal] = mapped_column(QTY, default=Decimal(0))
    last_counted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class StockMovement(TenantMixin, Base):
    """Append-only; StockLevel is the running sum."""

    __tablename__ = "stock_movement"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id"), index=True)
    location: Mapped[str] = mapped_column(String(40), default="main")
    type: Mapped[str] = mapped_column(Enum(*MOVEMENT_TYPES, native_enum=False))
    quantity: Mapped[Decimal] = mapped_column(QTY)  # signed
    date: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(20), default="agent")
    # Required for waste, spoilage, adjustment and count_correction.
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    reference_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class StockCount(TenantMixin, Base):
    __tablename__ = "stock_count"

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id"), index=True)
    counted_qty: Mapped[Decimal] = mapped_column(QTY)
    calculated_qty: Mapped[Decimal] = mapped_column(QTY)
    variance: Mapped[Decimal] = mapped_column(QTY)
    counted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    date: Mapped[date] = mapped_column(Date)
    resolution: Mapped[str] = mapped_column(Enum("accepted", "investigating", "adjusted", native_enum=False))


class DemandForecast(TenantMixin, Base):
    __tablename__ = "demand_forecast"
    __table_args__ = (UniqueConstraint("item_id", "forecast_date", "generated_on"),)

    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id"), index=True)
    forecast_date: Mapped[date] = mapped_column(Date, index=True)
    low: Mapped[Decimal] = mapped_column(QTY)
    expected: Mapped[Decimal] = mapped_column(QTY)
    high: Mapped[Decimal] = mapped_column(QTY)
    method: Mapped[str] = mapped_column(Enum("holt_winters", "same_weekday_avg", "derived", native_enum=False))
    generated_on: Mapped[date] = mapped_column(Date, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
