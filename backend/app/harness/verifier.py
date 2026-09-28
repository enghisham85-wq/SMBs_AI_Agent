"""Independent second check (FR-003, research R5).

The verifier gets a fresh request built only from the VerificationPacket: action type, source
inputs, proposed output and active rules. It never sees the primary agent's reasoning or messages.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.harness.audit import jsonable
from app.llm.client import LLMRefusalError, LLMUnavailableError, MissingFixtureError, get_llm, text_block
from app.llm.schemas import VerifierIssue, VerifierVerdict

_PROMPT = (Path(__file__).parent.parent / "llm" / "prompts" / "verifier.md").read_text(encoding="utf-8")
FORBIDDEN_KEYS = {"reasoning", "rationale", "messages", "thinking", "plan_reason"}


def build_packet(
    action_type: str,
    source_inputs: dict[str, Any],
    proposed_output: dict[str, Any],
    active_rules: list[str] | None = None,
    known_issues: list[dict[str, Any]] | None = None,
    irreversible: bool = False,
) -> dict[str, Any]:
    packet = {
        "action_type": action_type,
        "irreversible": irreversible,
        "source_inputs": source_inputs,
        "proposed_output": proposed_output,
        "active_rules": active_rules or [],
        # Deterministic findings the offline stand-in uses; the live verifier re-checks everything.
        "known_issues": known_issues or [],
    }
    leaked = FORBIDDEN_KEYS & set(proposed_output) | FORBIDDEN_KEYS & set(source_inputs)
    if leaked:
        raise ValueError(f"verification packet must not include primary reasoning: {sorted(leaked)}")
    return jsonable(packet)  # type: ignore[no-any-return]


def _offline(packet: dict[str, Any]) -> VerifierVerdict:
    issues = [
        VerifierIssue(
            field_or_aspect=str(i.get("field", "proposal")),
            problem=str(i.get("problem", "inconsistent with inputs")),
            severity=i.get("severity", "high"),
        )
        for i in packet.get("known_issues", [])
    ]
    return VerifierVerdict(
        agrees=not any(i.severity == "high" for i in issues),
        issues=issues,
        failure_modes_checked=["offline: deterministic checks only"],
        confidence=0.5,
    )


async def verify_independently(packet: dict[str, Any]) -> VerifierVerdict:
    content = [
        text_block(
            "Action type: "
            + str(packet["action_type"])
            + ("\nThis action is irreversible." if packet.get("irreversible") else "")
            + "\n\nSource inputs and proposal (JSON):\n"
            + json.dumps({k: v for k, v in packet.items() if k != "known_issues"}, ensure_ascii=False)
        )
    ]
    try:
        return await get_llm().parse("verifier", _PROMPT, content, VerifierVerdict, offline=lambda: _offline(packet))
    except (LLMRefusalError, LLMUnavailableError, MissingFixtureError) as exc:
        # Unavailable review is not agreement: escalate to the owner.
        return VerifierVerdict(
            agrees=False,
            issues=[VerifierIssue(field_or_aspect="verifier", problem=f"review unavailable: {exc}", severity="high")],
            failure_modes_checked=[],
            confidence=0.0,
        )
