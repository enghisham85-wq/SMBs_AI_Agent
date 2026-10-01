"""ActionSpec: what an agent capability plugs into harness_graph (research R12/R17)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActionContext:
    business_id: uuid.UUID
    action_id: uuid.UUID
    attempt: int = 1
    dry_run: bool = False


@dataclass
class Check:
    """Outcome of one precondition. `ask=True` holds the action and asks the owner instead of failing;
    `cancel=True` means the action is no longer needed (e.g. the invoice was paid) and ends it quietly.
    `incident=True` records a failure as an incident (with root cause and a proposed rule): a fault the
    agent caught, not just a routine question."""

    name: str
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)
    ask: bool = False
    cancel: bool = False
    reason_en: str = ""
    reason_ar: str = ""
    learned_rule_id: str | None = None
    incident: bool = False


@dataclass
class VerifyOutcome:
    ok: bool
    details: dict[str, Any] = field(default_factory=dict)
    corrections: dict[str, Any] | None = None


@dataclass
class OwnerAsk:
    kind: str  # approval | question | alert
    text_en: str
    text_ar: str
    options: list[dict[str, str]]  # {key, label_en, label_ar, effect}
    required_role: str = "manager"
    safe_default: str | None = "reask"
    deadline_hours: float | None = None
    urgency: int = 1
    context: dict[str, Any] = field(default_factory=dict)
    allow_text: bool = False
    reask: int = 0  # how many times this question was re-sent after a timeout


Fn = Callable[..., Awaitable[Any]]


@dataclass
class ActionSpec:
    name: str
    agent: str
    risk_class: str  # read_only | reversible | irreversible_external
    execute: Fn  # (ctx, inputs) -> dict
    title_en: str = ""
    title_ar: str = ""
    plan: Fn | None = None  # (ctx, inputs) -> {intent, reason, data_refs}
    preconditions: Fn | None = None  # (ctx, inputs) -> list[Check]
    verify: Fn | None = None  # (ctx, inputs, result) -> VerifyOutcome
    compensate: Fn | None = None  # (ctx, inputs, result) -> None
    verifier_packet: Fn | None = None  # (ctx, inputs, plan) -> dict | None
    auto_approve: Fn | None = None  # (ctx, inputs, settings) -> bool   (default: never)
    approval_request: Fn | None = None  # (ctx, inputs, plan) -> OwnerAsk
    on_option: Fn | None = None  # (ctx, inputs, option_key, edits) -> {"inputs": ..., "next": ...}
    on_hold: Fn | None = None  # (ctx, inputs, failed_checks) -> OwnerAsk | None
    on_finalize: Fn | None = None  # (ctx, inputs, outcome, result) -> None  (tidy own records)


_registry: dict[str, ActionSpec] = {}


def register(spec: ActionSpec) -> ActionSpec:
    _registry[spec.name] = spec
    return spec


def get_spec(name: str) -> ActionSpec:
    return _registry[name]


def all_specs() -> dict[str, ActionSpec]:
    return dict(_registry)
