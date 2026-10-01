"""Active-span scope: the causality half of full-stack tracing.

A trace id says which calls belong to one action; ``spanId`` says which call is
this one and ``parentSpanId`` says which open call caused it. Incoming parent
ids are ADOPT-OR-DROP, never adopt-or-mint: inventing a replacement parent would
fabricate a cause that never existed.

The ambient scope is deliberately SYNCHRONOUS. :func:`run_in_span` closes it
when its callback returns, including when the callback returns a coroutine.
Keeping a scope across suspension could mislabel concurrent calls as parent and
child. A flat trace is visibly incomplete; a false tree reads as truth.

Privacy contract: an id is exactly 16 lowercase hex characters. Pure hex cannot
match the PII guard's value patterns, and ``spanId``, ``parentSpanId`` and
``x-boosthis-trace-parent`` tokenize cleanly. The ids are random, ephemeral, and
never derived from user data. Like a trace id, a span id must NEVER appear in a
route or screen label.
"""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

PARENT_HEADER = "x-boosthis-trace-parent"
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_TRACE_ID_SHAPE = re.compile(r"^[0-9a-f]{32}$")
MAX_ACTIVE_SPANS = 32

_T = TypeVar("_T")
_UNSET = object()


def is_valid_span_id(value: Any) -> bool:
    """True only for the locked 16-character lowercase-hex shape."""
    return isinstance(value, str) and SPAN_ID_RE.match(value) is not None


def new_span_id() -> str:
    """Mint 64 random bits as 16 lowercase hexadecimal characters."""
    return secrets.token_hex(8)


def sanitize_span_id(value: Any) -> Optional[str]:
    """Adopt a valid span id unchanged; drop every other value."""
    return value if is_valid_span_id(value) else None


def adopt_parent_span_id(header_value: Any) -> Optional[str]:
    """Normalize an inbound header value to its first item, then adopt-or-drop.

    Header parsing is best-effort and never raises.
    """
    try:
        raw = header_value[0] if isinstance(header_value, (list, tuple)) else header_value
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("latin-1")
        return sanitize_span_id(raw)
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True)
class SpanHandle:
    """One call's own identity and the identity of its caller.

    ``trace_id`` is the ACTION the call belongs to, when the caller knows it.
    It is optional because a handle is often minted for a leg of work whose
    trace is resolved elsewhere; when it IS set, the handle can answer "which
    action is open right now?" — the question a crash asks.
    """

    span_id: str
    parent_span_id: Optional[str]
    trace_id: Optional[str] = None


@dataclass(frozen=True)
class ActionContext:
    """The action a call belongs to: the trace, and the leg of it that is
    open."""

    trace_id: str
    span_id: str


@dataclass(frozen=True)
class _ActiveFrame:
    span_id: str
    trace_id: Optional[str] = None


_active_spans: ContextVar[tuple[_ActiveFrame, ...]] = ContextVar(
    "boosthis_active_spans", default=()
)


def current_span_id() -> Optional[str]:
    """Return the innermost synchronously open span, if any."""
    active = _active_spans.get()
    return active[-1].span_id if active else None


def _sanitize_trace_id(value: Any) -> Optional[str]:
    """A 32-hex trace id, or None. Checked here rather than imported: trace.py
    imports THIS module, so reaching back the other way would close a cycle."""
    if isinstance(value, str) and _TRACE_ID_SHAPE.match(value):
        return value
    return None


def current_action() -> Optional[ActionContext]:
    """The action open on this context right now, or None.

    Two places hold one: a call explicitly opened with ``run_in_span`` on a
    trace-bearing handle, and — the common case — the request context the
    trace middleware already keeps for span parentage. Both are context-local,
    so what comes back is genuinely the work this task is inside.

    None is a real answer: a worker thread, a CLI run, or a crash raised
    outside any measured request has no action, and reporting one would mean
    guessing. Absence is filed as "no action recorded".
    """
    for frame in reversed(_active_spans.get()):
        if frame.trace_id is not None:
            return ActionContext(trace_id=frame.trace_id, span_id=frame.span_id)
    # The request-scoped pair, imported at call time: trace.py imports this
    # module, so the dependency may only run in this direction lazily.
    try:
        from boosthis.trace import get_span_id, get_trace_id, is_valid_trace_id

        trace_id = get_trace_id()
        span_id = sanitize_span_id(get_span_id())
        if is_valid_trace_id(trace_id) and span_id is not None:
            return ActionContext(trace_id=trace_id, span_id=span_id)
    except Exception:  # noqa: BLE001
        pass  # instrumentation must never disturb the host app.
    return None


def begin_span(parent_span_id: Any = _UNSET, trace_id: Any = None) -> SpanHandle:
    """Mint a span without opening it.

    An explicit argument, including ``None``, names the caller (invalid values
    drop to ``None``). If omitted, the innermost open scope is the caller.
    ``trace_id`` names the action this call belongs to, when the caller knows
    it; an unrecognisable value is dropped rather than carried.
    """
    parent = (
        current_span_id()
        if parent_span_id is _UNSET
        else sanitize_span_id(parent_span_id)
    )
    return SpanHandle(
        span_id=new_span_id(),
        parent_span_id=parent,
        trace_id=_sanitize_trace_id(trace_id),
    )


def run_in_span(span: SpanHandle, fn: Callable[[], _T]) -> _T:
    """Run ``fn`` with ``span`` ambient until the callback returns or raises."""
    active = _active_spans.get()
    pushed = len(active) < MAX_ACTIVE_SPANS
    frame = _ActiveFrame(span.span_id, span.trace_id)
    if pushed:
        _active_spans.set(active + (frame,))
    try:
        return fn()
    finally:
        if pushed:
            # Remove this exact frame rather than popping somebody else's.
            remaining = list(_active_spans.get())
            for index in range(len(remaining) - 1, -1, -1):
                if remaining[index] is frame:
                    del remaining[index]
                    break
            _active_spans.set(tuple(remaining))


def _active_count_for_tests() -> int:
    return len(_active_spans.get())


def _reset_for_tests() -> None:
    _active_spans.set(())


__all__ = [
    "PARENT_HEADER",
    "SPAN_ID_RE",
    "MAX_ACTIVE_SPANS",
    "SpanHandle",
    "ActionContext",
    "current_action",
    "is_valid_span_id",
    "new_span_id",
    "sanitize_span_id",
    "adopt_parent_span_id",
    "current_span_id",
    "begin_span",
    "run_in_span",
]