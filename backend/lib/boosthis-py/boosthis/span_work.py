"""BOOSTHIS_SPAN_WORK_V1 — what KIND of work a span measured, and whether it
WORKED.

This kit's copy of the shared vocabulary in ``lib/span-wire-vocabulary.json``.
Every runtime carries the same eight kind names and the same two outcome names,
spelled identically, so "a database query" found by this kit and by a Go
service meet in one place on the server instead of splitting into two.

THE FIELDS ARE OPTIONAL AND ABSENCE IS NOT A VALUE. A span with no ``kind``
came from a kit that does not report one; it is not a handler, and it is not
``other``. A span with no ``outcome`` is not "ok" — nothing was said about it.
The server stores both as absent and every reading says "not reporting" rather
than inventing a clean result.

A value not on these lists is dropped at the door rather than refusing the
batch. Adding one means adding it to the shared JSON first, then to every kit's
copy, then to the server's stored enum — never here alone.
"""

from __future__ import annotations

from typing import Any, Optional

#: The eight kinds of work a span may name. Closed set.
SPAN_WORK_KINDS = (
    "handler",
    "db",
    "http",
    "cache",
    "queue",
    "job",
    "render",
    "other",
)

#: Did the measured work succeed? Closed set, and INDEPENDENT of the rating:
#: a fast call can fail and a slow one can succeed.
SPAN_OUTCOMES = ("ok", "error")


def sanitize_work_kind(value: Any) -> Optional[str]:
    """Return the kind unchanged if it is one of the eight, else ``None``.

    ``None`` means the span reports no kind at all, which reads as "not
    reporting" — never as ``other``.
    """
    return value if isinstance(value, str) and value in SPAN_WORK_KINDS else None


def sanitize_outcome(value: Any) -> Optional[str]:
    """Same contract for the outcome."""
    return value if isinstance(value, str) and value in SPAN_OUTCOMES else None


def outcome_for_status(status: Any, threw: bool = False) -> str:
    """Decide an outcome for work that answered with an HTTP status.

    A refusal that is a real answer — 404, 401, a validation 422 — is ``ok``:
    the app did what it was asked to do. Only a 5xx is the work itself failing.
    Work that threw before any status arrived is ``error`` by way of ``threw``.
    """
    if threw:
        return "error"
    try:
        return "error" if int(status) >= 500 else "ok"
    except (TypeError, ValueError):
        return "ok"
