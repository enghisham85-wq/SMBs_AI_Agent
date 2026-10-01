"""Recognise and tally calls held open by design.

The definition is intentionally closed and shared by every Python framework
adapter: an HTTP 101 response, or one of the two unambiguously streaming
response media types. Header reads are fail-open so instrumentation never
guesses a real latency sample away.
"""

from __future__ import annotations

import math
import threading
from typing import Any, Iterable, Optional, Tuple

from .runtime_flags import is_boosthis_disabled

HELD_OPEN_CONTENT_TYPES = (
    "text/event-stream",
    "multipart/x-mixed-replace",
)

_lock = threading.Lock()
_held_open_excluded = 0
_held_open_worst_ms = 0


def is_held_open_content_type(raw: Any) -> bool:
    """True only for a media type in the closed held-open list. Never raises."""
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("latin-1")
        if not isinstance(raw, str):
            return False
        media_type = raw.split(";", 1)[0].strip().lower()
        return media_type in HELD_OPEN_CONTENT_TYPES
    except Exception:  # noqa: BLE001
        return False


def _content_type_from_headers(headers: Optional[Iterable[Tuple[Any, Any]]]) -> Any:
    try:
        for name, value in headers or ():
            key = name.decode("latin-1") if isinstance(name, bytes) else str(name)
            if key.lower() == "content-type":
                return value
    except Exception:  # noqa: BLE001
        return None
    return None


def is_held_open_response(
    response: Any = None,
    *,
    status: Optional[int] = None,
    headers: Optional[Iterable[Tuple[Any, Any]]] = None,
) -> bool:
    """Whether a finished framework/ASGI response was held open. Never raises."""
    try:
        code = status
        if code is None and response is not None:
            code = getattr(response, "status_code", None)
        if isinstance(code, int) and code == 101:
            return True

        raw = _content_type_from_headers(headers)
        if raw is None and response is not None:
            try:
                response_headers = getattr(response, "headers", None)
                if response_headers is not None:
                    raw = response_headers.get("Content-Type")
            except Exception:  # noqa: BLE001
                return False
            if raw is None:
                try:
                    raw = getattr(response, "content_type", None)
                except Exception:  # noqa: BLE001
                    return False
        return is_held_open_content_type(raw)
    except Exception:  # noqa: BLE001
        return False


def note_held_open_excluded(duration_ms: float) -> None:
    """Count one withheld duration and remember the longest integer ms."""
    global _held_open_excluded, _held_open_worst_ms
    try:
        if is_boosthis_disabled():
            return
        value = float(duration_ms)
        if not math.isfinite(value) or value < 0:
            return
        ms = round(value)
        with _lock:
            _held_open_excluded += 1
            if ms > _held_open_worst_ms:
                _held_open_worst_ms = ms
    except Exception:  # noqa: BLE001
        pass


def get_held_open_stats() -> dict[str, int]:
    """Return the two numeric wire fields."""
    try:
        with _lock:
            return {
                "heldOpenExcluded": _held_open_excluded,
                "heldOpenWorstMs": _held_open_worst_ms,
            }
    except Exception:  # noqa: BLE001
        return {"heldOpenExcluded": 0, "heldOpenWorstMs": 0}


def clear_held_open() -> None:
    """Reset the tally on the kit's meter-erasure path."""
    global _held_open_excluded, _held_open_worst_ms
    try:
        with _lock:
            _held_open_excluded = 0
            _held_open_worst_ms = 0
    except Exception:  # noqa: BLE001
        pass