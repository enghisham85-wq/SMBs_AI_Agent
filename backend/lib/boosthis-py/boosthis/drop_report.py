"""\"The server stored fewer rows than this app sent\" — said inside the app.

WHY THIS EXISTS. The upload endpoints answer ``202`` and then discard
individual rows they cannot store: a route label the shared privacy guard
refuses, a span past the per-trace cap, a snapshot entry whose name is
rejected. Until now the kit read that ``202`` as unqualified success, so a
developer whose every route label was being thrown away saw a healthy app, no
output, and a dashboard that was quietly missing data. The only way to find out
was to read the database by hand — exactly what cost real debugging time during
the Flutter live smoke.

CONTRACT (identical in every kit — change it in one, change it in all):
  - Read ``dropped`` and ``droppedByCause`` from an upload reply. Their ABSENCE
    (older server, non-dict body, unreadable body) means "nothing to report":
    no warning, no crash, no guessing.
  - Warn EXACTLY ONCE per process per cause on the ``boosthis`` logger (whose
    default handler is stderr), naming the cause and the fix. Never once per
    request, never behind a debug flag — this exists for the developer who does
    not yet suspect the kit, the same reason the "stayed inactive" line is
    ungated.
  - Keep a cumulative count + the causes seen, so the kit's own status page and
    bubble can show the size of the hole in the SAME words the web page uses.
  - Nothing here may ever fail an upload or reach the host's request path: every
    entry point is self-guarded and the reply is read after the response is
    already in hand.
  - Nothing is uploaded. The warning is local output only.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

# Why the server threw a row away. Mirrors the server's ``DropCause``; any cause
# key this kit does not know is counted in the total and named nowhere — a kit
# must never invent an explanation for something it cannot read.
CAUSE_KEYS: tuple[str, ...] = (
    "labelRejected",
    "traceCapReached",
    "snapshotEntryFiltered",
)

# Short cause text — the same words the project's web page uses, so the in-app
# view and the dashboard never tell two different stories.
DROP_CAUSE_TEXT: dict[str, str] = {
    "labelRejected": "route names the privacy guard refused",
    "traceCapReached": "spans past the 20-span limit",
    "snapshotEntryFiltered": "snapshot entries the privacy guard refused",
}

# What the developer changes to stop it. Second sentence of the warning. (The
# dash in the traceCapReached fix is an em dash, U+2014.)
DROP_CAUSE_FIX: dict[str, str] = {
    "labelRejected": "Name routes in code, the way GET /orders is written.",
    "traceCapReached": (
        "A trace keeps its first 20 spans \u2014 measure fewer steps per "
        "request, or split a very long trace."
    ),
    "snapshotEntryFiltered": (
        "Name screens in code, never from what a person typed or an id."
    ),
}


def _finite_count(v: Any) -> int:
    """A count is only real when it is a positive, finite integer; anything
    else (missing, NaN, a string, a float that reads as 0) is nothing."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    if v != v or v in (float("inf"), float("-inf")) or v <= 0:
        return 0
    return int(v)


def parse_drop_reply(body: Any) -> Optional[tuple[int, list[tuple[str, int]]]]:
    """Read the honesty fields out of a parsed reply body.

    Returns ``(total, by_cause)`` where ``by_cause`` is a list of
    ``(cause, count)`` in the fixed cause order for the causes this kit knows.
    Returns ``None`` when the body says nothing about drops — a server that
    predates the fields, a body that is not a dict, or a ``dropped`` that is not
    a positive number. ``None`` means "behave exactly as before"."""
    try:
        if not isinstance(body, dict):
            return None
        total = _finite_count(body.get("dropped"))
        if total <= 0:
            return None
        raw = body.get("droppedByCause")
        by_cause: list[tuple[str, int]] = []
        if isinstance(raw, dict):
            for cause in CAUSE_KEYS:
                n = _finite_count(raw.get(cause))
                if n > 0:
                    by_cause.append((cause, n))
        return total, by_cause
    except Exception:  # noqa: BLE001
        return None


# ── Process-local state ─────────────────────────────────────────────────────
#
# Cumulative for the life of the process: it is the size of the hole in the
# dashboard, so a later clean upload does not erase it.

_dropped_rows = 0
_seen_causes: set[str] = set()
_warned_causes: set[str] = set()


def get_dropped_row_count() -> int:
    """How many rows the server has refused since this process started."""
    return _dropped_rows


def get_dropped_row_causes() -> list[str]:
    """The causes seen so far, in the fixed cause order. Empty when nothing has
    been dropped."""
    return [c for c in CAUSE_KEYS if c in _seen_causes]


def drop_summary_text() -> str:
    """The status-page / bubble figure: ``\"6 \u2014 route names the privacy
    guard refused; spans past the 20-span limit\"``. Empty string when nothing
    has been dropped, so a healthy app shows nothing at all."""
    if _dropped_rows <= 0:
        return ""
    causes = [DROP_CAUSE_TEXT[c] for c in get_dropped_row_causes()]
    if not causes:
        return str(_dropped_rows)
    return f"{_dropped_rows} \u2014 {'; '.join(causes)}"


def _warn_cause_once(cause: str, count: int) -> None:
    """Emit the one-shot line for a cause on the ``boosthis`` logger, ungated,
    never repeated. Same latch style as ``_warn_stayed_inactive_once``. Never
    raises — a broken logger must not take the host down."""
    if cause in _warned_causes:
        return
    _warned_causes.add(cause)
    try:
        logging.getLogger("boosthis").warning(
            "[boosthis] Boosthis dropped %d of the measurements this app sent: %s. %s",
            count,
            DROP_CAUSE_TEXT[cause],
            DROP_CAUSE_FIX[cause],
        )
    except Exception:  # noqa: BLE001
        pass


def note_server_drops(body: Any) -> None:
    """Record what one upload reply said, and warn once per cause.

    Safe to call with anything: a reply without the fields, a string, ``None``.
    Only a body that actually reports a positive ``dropped`` changes any state
    or prints anything. Never raises — telling someone about a dropped row must
    never break an upload."""
    global _dropped_rows
    try:
        parsed = parse_drop_reply(body)
        if parsed is None:
            return
        total, by_cause = parsed
        _dropped_rows += total
        for cause, count in by_cause:
            _seen_causes.add(cause)
            _warn_cause_once(cause, count)
    except Exception:  # noqa: BLE001
        pass


def _reset_drop_report_for_tests() -> None:
    """Test-only: forget every drop and every warn-once latch. The sibling of
    ``_reset_registration_warning_for_tests``."""
    global _dropped_rows
    _dropped_rows = 0
    _seen_causes.clear()
    _warned_causes.clear()
