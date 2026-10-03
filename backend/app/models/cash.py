"""Cash forecast runs, shortfall plans, purchasing budget and payment reminders.

Written by the Cash-Flow Agent.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import Base, Money, TenantMixin, money_col

SCENARIOS = ("expected", "pessimistic", "optimistic")
PLAN_ACTION_TYPES = ("chase_receivable", "delay_payable", "defer_po", "move_expense", "financing")
RISKS = ("low", "medium", "high")
REMINDER_STATUSES = ("scheduled", "pending_approval", "sent", "cancelled_paid", "cancelled_owner")


class CashForecastRun(TenantMixin, Base):
    __tablename__ = "cash_forecast_run"

    generated_on: Mapped[date] = mapped_column(Date, index=True)
    # Latest bank data behind the opening balance (end of that business day).
    bank_data_as_of: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    low_confidence_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # expected, or pessimistic when an inflow assumption looks unrealistic
    primary_scenario: Mapped[str] = mapped_column(Enum(*SCENARIOS, native_enum=False), default="expected")
    opening_balance: Mapped[Money] = money_col("opening_balance")
    lowest_balance: Mapped[Money] = money_col("lowest_balance")
    lowest_date: Mapped[date] = mapped_column(Date)
    buffer: Mapped[Money] = money_col("buffer")
    horizon_days: Mapped[int] = mapped_column(Integer, default=30)
    # Itemised flows of the expected scenario: [{date, amount_minor, kind, ref, label}] (explains every figure)
    flows: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Check outcomes: [{name, passed, details, reason_en}]
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)


class CashForecast(TenantMixin, Base):
    """One row per run x scenario x day."""

    __tablename__ = "cash_forecast"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cash_forecast_run.id"), index=True)
    scenario: Mapped[str] = mapped_column(Enum(*SCENARIOS, native_enum=False))
    date: Mapped[date] = mapped_column(Date)
    opening: Mapped[Money] = money_col("opening")
    inflows: Mapped[Money] = money_col("inflows")
    outflows: Mapped[Money] = money_col("outflows")
    closing: Mapped[Money] = money_col("closing")
    confidence: Mapped[float] = mapped_column(Float)
    below_buffer: Mapped[bool] = mapped_column(Boolean, default=False)


class ShortfallPlan(TenantMixin, Base):
    __tablename__ = "shortfall_plan"

    forecast_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("cash_forecast_run.id"), index=True)
    gap_amount: Mapped[Money] = money_col("gap_amount")
    gap_date: Mapped[date] = mapped_column(Date)  # first day below the buffer
    lowest_balance: Mapped[Money] = money_col("lowest_balance")
    lowest_date: Mapped[date] = mapped_column(Date)
    days_to_act: Mapped[int] = mapped_column(Integer)
    # Lowest balance if every non-financing action is taken together.
    combined_lowest_balance: Mapped[Money] = money_col("combined_lowest_balance")
    status: Mapped[str] = mapped_column(
        Enum("proposed", "presented", "accepted", "dismissed", "superseded", native_enum=False), default="proposed"
    )
    graph_thread_id: Mapped[str | None] = mapped_column(String(120), nullable=True)


class PlanAction(TenantMixin, Base):
    __tablename__ = "plan_action"

    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("shortfall_plan.id"), index=True)
    type: Mapped[str] = mapped_column(Enum(*PLAN_ACTION_TYPES, native_enum=False))
    target_type: Mapped[str] = mapped_column(String(40))  # receivable | payable | po | planned_purchases | obligation | financing
    target_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    description_en: Mapped[str] = mapped_column(String(300))
    description_ar: Mapped[str] = mapped_column(String(300))
    impact: Mapped[Money] = money_col("impact")  # how much the lowest balance improves
    risk: Mapped[str] = mapped_column(Enum(*RISKS, native_enum=False))
    rank: Mapped[int] = mapped_column(Integer)
    simulated_lowest_balance: Mapped[Money] = money_col("simulated_lowest_balance")
    simulated_lowest_date: Mapped[date] = mapped_column(Date)
    # Flow changes this action makes: [{key, move_to?, scale?}]; replayed by later forecasts once accepted.
    adjustments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Closing balance per day after the action: [{date, closing_minor}]
    simulated_series: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(Enum("proposed", "accepted", "declined", native_enum=False), default="proposed")


class PurchasingBudget(TenantMixin, Base):
    __tablename__ = "purchasing_budget"
    __table_args__ = (UniqueConstraint("business_id", "week_start"),)

    week_start: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Money] = money_col("amount")
    reason: Mapped[str] = mapped_column(String(300), default="")
    tightened: Mapped[bool] = mapped_column(Boolean, default=False)
    published_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    forecast_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class PaymentReminder(TenantMixin, Base):
    __tablename__ = "payment_reminder"
    __table_args__ = (UniqueConstraint("receivable_invoice_id", "level"),)

    receivable_invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("receivable_invoice.id"), index=True)
    level: Mapped[int] = mapped_column(Integer)  # 1 polite -> 3 firm
    scheduled_for: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(Enum(*REMINDER_STATUSES, native_enum=False), default="scheduled", index=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    text_en: Mapped[str] = mapped_column(String(600), default="")
    text_ar: Mapped[str] = mapped_column(String(600), default="")
    action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto_approved: Mapped[bool] = mapped_column(Boolean, default=False)


class PaymentPromise(TenantMixin, Base):
    __tablename__ = "payment_promise"

    receivable_invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("receivable_invoice.id"), index=True)
    promised_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Money] = money_col("amount")
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # kept | broken; None while the promised date has not passed
    outcome: Mapped[str | None] = mapped_column(String(10), nullable=True)
