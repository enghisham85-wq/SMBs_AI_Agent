"""Harness records: actions, checks, approvals, incidents, rules, calibration, audit."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import Base, TenantMixin, _clock_now

AGENTS = ("stock", "cashflow", "accountant", "harness")
RISK_CLASSES = ("read_only", "reversible", "irreversible_external")
ACTION_STAGES = (
    "planned",
    "prechecked",
    "awaiting_approval",
    "executing",
    "verifying",
    "completed",
    "rolled_back",
    "retrying",
    "escalated",
    "failed",
    "rejected",
)


class Action(TenantMixin, Base):
    __tablename__ = "action"

    agent: Mapped[str] = mapped_column(Enum(*AGENTS, native_enum=False))
    type: Mapped[str] = mapped_column(String(60), index=True)
    graph_name: Mapped[str] = mapped_column(String(60), default="harness")
    graph_thread_id: Mapped[str] = mapped_column(String(100), index=True)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk_class: Mapped[str] = mapped_column(Enum(*RISK_CLASSES, native_enum=False))
    stage: Mapped[str] = mapped_column(Enum(*ACTION_STAGES, native_enum=False), default="planned")
    dry_run: Mapped[bool] = mapped_column(default=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    parent_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("action.id"), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    verifier_verdict: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class CheckResult(TenantMixin, Base):
    __tablename__ = "check_result"

    action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("action.id"), nullable=True, index=True)
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    check_name: Mapped[str] = mapped_column(String(60))
    passed: Mapped[bool]
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    learned_rule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class ApprovalRequest(TenantMixin, Base):
    __tablename__ = "approval_request"
    __table_args__ = (UniqueConstraint("graph_thread_id", "gate_key"),)

    action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("action.id"), nullable=True)
    kind: Mapped[str] = mapped_column(Enum("approval", "question", "alert", native_enum=False))
    text_en: Mapped[str] = mapped_column(Text)
    text_ar: Mapped[str] = mapped_column(Text)
    # [{key, label_en, label_ar, effect}]
    options: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    required_role: Mapped[str] = mapped_column(String(10), default="manager")
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Option key applied on timeout; never an irreversible effect.
    safe_default: Mapped[str | None] = mapped_column(String(40), nullable=True)
    urgency: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(
        Enum("pending", "resolved", "timed_out", "superseded", native_enum=False), default="pending", index=True
    )
    resolved_option: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resolved_edits: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    resolved_via: Mapped[str | None] = mapped_column(
        Enum("dashboard", "telegram", "system", native_enum=False), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reask_count: Mapped[int] = mapped_column(Integer, default=0)
    telegram_message_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    graph_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    graph_thread_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    gate_key: Mapped[str] = mapped_column(String(80), default="gate")
    interrupt_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Opaque token used in Telegram callbacks instead of the id.
    request_token: Mapped[str] = mapped_column(String(32), unique=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    agent: Mapped[str] = mapped_column(String(20), default="harness")


class Incident(TenantMixin, Base):
    __tablename__ = "incident"

    agent: Mapped[str] = mapped_column(String(20))
    type: Mapped[str] = mapped_column(String(60), index=True)
    detected_by: Mapped[str] = mapped_column(String(60))
    summary: Mapped[str] = mapped_column(Text)
    action_taken: Mapped[str] = mapped_column(Text, default="")
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("open", "investigating", "resolved", "wont_fix", native_enum=False), default="open"
    )
    refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    chaos_injection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LearnedRule(TenantMixin, Base):
    __tablename__ = "learned_rule"

    agent: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(
        Enum("precondition", "check", "parsing_hint", "classification", "policy", native_enum=False)
    )
    rule_text_en: Mapped[str] = mapped_column(Text)
    rule_text_ar: Mapped[str] = mapped_column(Text, default="")
    trigger: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_incident_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    proposed_by: Mapped[str] = mapped_column(String(20), default="harness")
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("proposed", "active", "rejected", "inactive", native_enum=False), default="proposed", index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    times_applied: Mapped[int] = mapped_column(Integer, default=0)
    times_overridden: Mapped[int] = mapped_column(Integer, default=0)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentCalibration(TenantMixin, Base):
    __tablename__ = "agent_calibration"
    __table_args__ = (UniqueConstraint("business_id", "agent", "metric"),)

    agent: Mapped[str] = mapped_column(String(20))
    metric: Mapped[str] = mapped_column(String(60))
    window_days: Mapped[int] = mapped_column(Integer, default=7)
    current_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float] = mapped_column(Float)
    high_confidence_threshold: Mapped[float] = mapped_column(Float, default=0.90)
    low_confidence_threshold: Mapped[float] = mapped_column(Float, default=0.60)
    auto_approve_factor: Mapped[float] = mapped_column(Float, default=1.0)
    degraded: Mapped[bool] = mapped_column(default=False)
    degraded_since: Mapped[date | None] = mapped_column(Date, nullable=True)
    healthy_streak: Mapped[int] = mapped_column(Integer, default=0)
    restore_progress: Mapped[float] = mapped_column(Float, default=1.0)
    method_override: Mapped[str | None] = mapped_column(String(40), nullable=True)


class AgentCalibrationHistory(TenantMixin, Base):
    __tablename__ = "agent_calibration_history"

    agent: Mapped[str] = mapped_column(String(20))
    metric: Mapped[str] = mapped_column(String(60))
    date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    degraded: Mapped[bool]
    high_confidence_threshold: Mapped[float] = mapped_column(Float)


class AuditLogEntry(Base):
    """Append-only. Not a TenantMixin so timestamps are explicit (business clock + wall clock)."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    business_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("business.id"), index=True, nullable=True)
    business_time: Mapped[datetime] = mapped_column(DateTime, default=_clock_now, index=True)
    wall_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    event: Mapped[str] = mapped_column(String(60), index=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verification_result: Mapped[str | None] = mapped_column(String(30), nullable=True)
