"""Chaos mode injections: which fault was injected and what the agents did about it (detected,
explained, corrected, rule proposed) and how long that took."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import Base, TenantMixin, _clock_now

# The 8 fault scenarios, in a fixed order.
SCENARIOS = (
    "duplicate_invoice",
    "price_spike",
    "date_format",
    "demand_spike",
    "paid_before_reminder",
    "missing_bank_day",
    "short_delivery",
    "cash_crunch",
)


class ChaosInjection(TenantMixin, Base):
    __tablename__ = "chaos_injection"

    scenario: Mapped[str] = mapped_column(Enum(*SCENARIOS, native_enum=False), index=True)
    injected_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    injected_at: Mapped[datetime] = mapped_column(DateTime, default=_clock_now)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # What the injector changed (document, order, reminder, override dates, ...).
    affected_refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(Enum("running", "completed", "failed", native_enum=False), default="running")
    detected: Mapped[bool] = mapped_column(Boolean, default=False)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Wall-clock seconds from injection until a rule was proposed (or the run ended).
    elapsed_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Detection method, explanation, correction, timeline and scenario-specific evidence.
    outcome: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
