"""Rule contribution scaffolding.

The `boosthis propose-rule` CLI uses this to drop a structured draft into
``~/.boosthis/proposed-rules/<id>.json`` and print exactly how to turn it
into a PR. We don't ship a server-side ingest — the contribution flow is
intentionally a human PR so a maintainer reviews the rule against real
evidence before it ships.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")


def _dir() -> Path:
    p = Path(os.path.expanduser("~/.boosthis/proposed-rules"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def validate_id(rule_id: str) -> None:
    if not ID_RE.match(rule_id):
        raise ValueError(
            f"rule id must match {ID_RE.pattern} (kebab-case, got {rule_id!r})"
        )


def scaffold(
    rule_id: str,
    title: str,
    when_to_apply: str,
    fix_template: str,
    runtime: str = "python",
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    """Validate + persist a proposed rule. Returns the saved payload."""
    validate_id(rule_id)
    if runtime not in {"python", "react-native", "node"}:
        raise ValueError(f"runtime must be python|react-native|node, got {runtime!r}")
    if len(title) < 8:
        raise ValueError("title must be at least 8 characters")
    if len(when_to_apply) < 40:
        raise ValueError(
            "when_to_apply must be at least 40 characters — be specific about the "
            "code shape that triggers this rule, and the visible symptom"
        )
    if len(fix_template) < 40:
        raise ValueError(
            "fix_template must be at least 40 characters — describe what to change, "
            "not what to think about"
        )
    # Evidence is mandatory — CONTRIBUTING.md says "Boosthis only ships rules
    # backed by a real production fix." Reject empty / whitespace-only entries
    # at the scaffold step so the draft never claims to have evidence it doesn't.
    cleaned_evidence = [e.strip() for e in (evidence or []) if e and e.strip()]
    if not cleaned_evidence:
        raise ValueError(
            "evidence is required — pass at least one --evidence pointing to a real "
            "commit, file path, or measurement where you saw this regression"
        )
    payload: dict[str, Any] = {
        "id": rule_id,
        "title": title,
        "whenToApply": when_to_apply,
        "fixTemplate": fix_template,
        "evidence": cleaned_evidence,
        "runtime": runtime,
        "drafted_at": int(time.time() * 1000),
    }
    path = _dir() / f"{rule_id}.json"
    path.write_text(json.dumps(payload, indent=2))
    return payload


def list_proposed() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(_dir().glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except Exception:
            continue
    return out


PR_INSTRUCTIONS = """\
Your proposal is saved to {path}.

To turn it into a Boosthis rule, open a PR that adds it to the right
checklist source file:

  • Python runtime rules → lib/boosthis-py/boosthis/checklist.py
  • React Native rules   → lib/boosthis-checklist/src/index.ts
  • Node.js rules        → (new file — see CONTRIBUTING.md, "Adding a
                          Node-specific rule")

Include in the PR:
  1. A link to the real code + commit where you saw this regression
     (the `evidence` field is mandatory for new rules — Boosthis only
     ships rules backed by a real production fix).
  2. A short before/after measurement showing the fix moved the needle.
  3. Tests if the rule's keyword index needs updating.

A maintainer will review against the threat model + privacy contract
before merging. Rules cannot ship if their fix_template asks the user
to log, transmit, or persist any value that looks like PII.
"""
