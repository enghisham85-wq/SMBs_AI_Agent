"""Event outbox and demo-feed overrides (data-model.md §5)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import Base, TenantMixin, _clock_now


class Event(TenantMixin, Base):
    __tablename__ = "event"

    type: Mapped[str] = mapped_column(String(60), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    producer: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=_clock_now)
    dispatched: Mapped[bool] = mapped_column(default=False, index=True)


class EventDelivery(TenantMixin, Base):
    __tablename__ = "event_delivery"
    __table_args__ = (UniqueConstraint("event_id", "consumer"),)

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("event.id"), index=True)
    consumer: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(Enum("handled", "failed", native_enum=False))
    handled_at: Mapped[datetime] = mapped_column(DateTime, default=_clock_now)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class FeedOverride(TenantMixin, Base):
    """Per-date changes to the demo data feed, e.g. {"sales_multiplier": {item_id: 3}}."""

    __tablename__ = "feed_override"
    __table_args__ = (UniqueConstraint("business_id", "date"),)

    date: Mapped[date] = mapped_column(Date)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    chaos_injection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
