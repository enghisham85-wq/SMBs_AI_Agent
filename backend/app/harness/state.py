"""ActionState carried through harness_graph (data-model.md §5)."""

from __future__ import annotations

from typing import Any, TypedDict


class ActionState(TypedDict, total=False):
    action_id: str
    business_id: str
    spec_name: str
    inputs: dict[str, Any]
    plan: dict[str, Any]
    precheck_results: list[dict[str, Any]]
    risk_class: str
    verifier_verdict: dict[str, Any] | None
    approval: dict[str, Any] | None
    attempt: int
    reask: int
    execution_result: dict[str, Any] | None
    verification_result: dict[str, Any] | None
    corrections: dict[str, Any] | None
    outcome: str | None
    reason: str | None
    dry_run: bool
    route: str
