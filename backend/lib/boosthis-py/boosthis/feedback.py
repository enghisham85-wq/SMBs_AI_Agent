"""Feedback store — the AI → Boosthis learning loop.

When an AI agent (Claude, Cursor, Replit AI, …) uses Boosthis to diagnose
a perf issue and then *acts* on the suggestion, the outcome flows back
here as a structured event. The corpus this builds is what lets us turn
real-world experience into better rules over time.

Three event kinds today:

  - fix_outcome           — AI applied a rule's fix_template; did it help?
  - unmatched_pattern     — AI saw a slow pattern with no matching rule.
  - rule_improvement      — AI proposes a refinement to an existing rule.

Storage is append-only JSONL at ``~/.boosthis/feedback/feedback.jsonl``
(capped at FEEDBACK_MAX_LINES, oldest rotated out). Everything is local
and on-device by default — nothing leaves the host unless the user
explicitly opts in via the existing telemetry pipeline.

Every payload is filtered through ``assert_no_pii`` BEFORE it touches
disk, on the same denylist the rest of the project uses. The AI can lie
about field names; the guard does not trust them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from boosthis.pii import PIIDetectedError, assert_no_pii

logger = logging.getLogger("boosthis.feedback")

FEEDBACK_DIR = Path.home() / ".boosthis" / "feedback"
FEEDBACK_PATH = FEEDBACK_DIR / "feedback.jsonl"
FEEDBACK_MAX_LINES = 5000  # cap to keep the file under ~5MB at typical row size
FEEDBACK_TRIM_TO = 4000    # when we rotate, keep this many newest lines

EventKind = Literal["fix_outcome", "unmatched_pattern", "rule_improvement"]

_lock = threading.Lock()


@dataclass(frozen=True)
class FeedbackEvent:
    """One feedback event, ready to be serialized as a JSONL row."""

    kind: EventKind
    timestamp_ms: int
    app_id: str | None
    payload: dict

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "timestamp_ms": self.timestamp_ms,
            "app_id": self.app_id,
            "payload": self.payload,
        }


class FeedbackRejectedError(ValueError):
    """Raised when a feedback payload fails validation (shape or PII)."""


def _ensure_dir() -> None:
    try:
        FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Surface as a normal error; the MCP layer will translate to a
        # tool-error response rather than crashing the JSON-RPC loop.
        raise FeedbackRejectedError(f"cannot create {FEEDBACK_DIR}: {exc}") from exc


def _rotate_if_needed() -> None:
    """Keep the JSONL file bounded. Best-effort — failure does not block writes."""
    try:
        if not FEEDBACK_PATH.exists():
            return
        # cheap line count without loading whole file into memory
        with FEEDBACK_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= FEEDBACK_MAX_LINES:
            return
        keep = lines[-FEEDBACK_TRIM_TO:]
        tmp = FEEDBACK_PATH.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, FEEDBACK_PATH)
    except OSError as exc:
        logger.warning("feedback rotation failed: %s", exc)


def _coerce_payload(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise FeedbackRejectedError("payload must be an object")
    return raw


def record(kind: EventKind, payload: dict, app_id: str | None = None) -> FeedbackEvent:
    """Append a feedback event. PII-guarded; raises on rejection.

    The PII guard is intentionally run on the *whole* payload, including
    keys an AI might choose — so an AI cannot smuggle a user's email
    through by stashing it in a field called ``free_form_notes``.
    """
    if kind not in ("fix_outcome", "unmatched_pattern", "rule_improvement"):
        raise FeedbackRejectedError(f"unknown kind: {kind!r}")
    payload = _coerce_payload(payload)
    try:
        assert_no_pii(payload)
    except PIIDetectedError as exc:
        raise FeedbackRejectedError(
            f"PII guard rejected feedback (field={exc.field_name!r})"
        ) from exc

    event = FeedbackEvent(
        kind=kind,
        timestamp_ms=int(time.time() * 1000),
        app_id=app_id,
        payload=payload,
    )
    with _lock:
        _ensure_dir()
        with FEEDBACK_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), default=str) + "\n")
        _rotate_if_needed()
    return event


def recent(limit: int = 100, kind: EventKind | None = None) -> list[FeedbackEvent]:
    """Return the most recent events (newest first)."""
    if not FEEDBACK_PATH.exists():
        return []
    try:
        with FEEDBACK_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[FeedbackEvent] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            k = row.get("kind")
            if k not in ("fix_outcome", "unmatched_pattern", "rule_improvement"):
                continue
            if kind and k != kind:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            out.append(
                FeedbackEvent(
                    kind=k,  # type: ignore[arg-type]
                    timestamp_ms=int(row.get("timestamp_ms") or 0),
                    app_id=row.get("app_id"),
                    payload=payload,
                )
            )
            if len(out) >= limit:
                break
        except (ValueError, TypeError):
            continue
    return out


def summary() -> dict:
    """Aggregate counts by kind + rule_id (when present in payload)."""
    events = recent(limit=FEEDBACK_MAX_LINES)
    counts: dict[str, int] = {
        "fix_outcome": 0,
        "unmatched_pattern": 0,
        "rule_improvement": 0,
    }
    helpful = 0
    unhelpful = 0
    by_rule: dict[str, dict[str, int]] = {}
    for e in events:
        counts[e.kind] = counts.get(e.kind, 0) + 1
        if e.kind == "fix_outcome":
            rule_id = str(e.payload.get("rule_id") or "")
            was_helpful = bool(e.payload.get("was_helpful"))
            if was_helpful:
                helpful += 1
            else:
                unhelpful += 1
            if rule_id:
                bucket = by_rule.setdefault(rule_id, {"helpful": 0, "unhelpful": 0})
                bucket["helpful" if was_helpful else "unhelpful"] += 1
    return {
        "total": len(events),
        "by_kind": counts,
        "fix_outcomes": {"helpful": helpful, "unhelpful": unhelpful},
        "by_rule": by_rule,
    }


def clear() -> None:
    """Remove the feedback file. Mainly for tests and `boosthis telemetry forget`."""
    with _lock:
        try:
            FEEDBACK_PATH.unlink()
        except FileNotFoundError:
            pass
