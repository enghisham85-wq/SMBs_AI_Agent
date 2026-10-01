"""Full-stack trace — Stage 2: span emission (Python layer).

Byte-for-byte the same wire contract as the RN + Node span emitters. Stage 1
(see :mod:`boosthis.trace`) only *propagated* a 32-hex correlation id on the
``x-boosthis-trace`` header; Stage 2 lets the Python layer emit ONE privacy-safe
span per measured operation so the server can stitch a waterfall
(RN -> Node -> Python) keyed by that id.

A span is a tiny, closed, code-derived shape (identical keys to the server's
``POST /api/spans`` schema and the RN/Node emitters)::

    {traceId, layer: "py", routeLabel, durationMs, startOffsetMs, rating}

EMISSION SITE: unlike RN (which mints a span at the outbound ``trace_headers``
call site) and Node (which emits from the ``boosthis()`` middleware), the Python
runtime is decorator-driven end-to-end — every measurement flows through
``tracker._emit``. So Python emits its span there, reusing the SAME code-defined
label the sample already recorded (already cleared by ``check_route_label``) and
the trace id set on the request contextvar by :class:`BoosthisTraceMiddleware`.
A measurement with no trace context set emits no span (nothing to correlate to).

PRIVACY-BY-DEFAULT: :func:`enqueue_span` is a no-op until a submitter is wired.
The telemetry client only wires the span submitter when the snapshot mirror is
allowed (the developer opted in via ``share_meter_with_ai`` OR connected an AI
from the web dashboard), so a private app RETAINS nothing span-shaped until
sharing is authorized. The emergency kill-switch (``BOOSTHIS_DISABLED``) and
``forget()`` both stop and clear it, exactly like the snapshot mirror.

UPLOAD MODEL: mirroring :mod:`boosthis.snapshot_mirror`, the Python runtime has
no always-running event loop and Boosthis's own Python rule book forbids
idle-spinning background pollers. So flushing is **work-gated**: every
:func:`enqueue_span` buffers the span and, throttled to at most once per
:data:`SPAN_FLUSH_MS`, spawns a ONE-SHOT daemon thread to ship a batch — never
blocking the host request and never spinning when the app is idle.

Everything here is best-effort: span bookkeeping must NEVER change the host
request's behavior or crash the app.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.sample_uploader import NOT_SENT
from boosthis.score import get_duration_rating
from boosthis.span_scope import sanitize_span_id
from boosthis.span_work import sanitize_outcome, sanitize_work_kind
from boosthis.thresholds import Rating

#: Server accepts at most 50 spans per batch.
MAX_SPAN_BATCH = 50
#: Hard cap on the in-memory buffer (drop-oldest on overflow) so a burst of
#: measurements between flushes can never grow memory without bound.
MAX_BUFFERED_SPANS = 50
#: ``routeLabel`` server cap.
MAX_SPAN_ROUTE_LABEL = 100
#: Work-gated flush throttle window (matches Node's auto-flush cadence).
SPAN_FLUSH_MS = 15_000
#: Duration hard clamp (matches the server ``durationMs`` 0..600000 bound).
MAX_SPAN_DURATION_MS = 600_000


def rate_span_duration(ms: float) -> Rating:
    """Classify a duration against the shared TTI band — byte-identical to RN
    ``rateSpanDuration`` and Node ``rateSpanDuration``. Delegates to the shared
    :func:`boosthis.score.get_duration_rating` so there is one threshold source.
    """
    return get_duration_rating(ms)


def span_label(label: str) -> str:
    """Cap the code-defined route label at the server bound. The label itself is
    the already-guarded ``@track_perf`` / ``perf`` name."""
    return label[:MAX_SPAN_ROUTE_LABEL] if len(label) > MAX_SPAN_ROUTE_LABEL else label


SpanSubmitter = Callable[[list[dict[str, Any]]], int]

_submitter: SpanSubmitter | None = None
_buffer: list[dict[str, Any]] = []
_last_flush_at_ms: float = 0.0
_in_flight = False
_state_lock = threading.Lock()


def set_span_submitter(fn: SpanSubmitter | None) -> None:
    """Register (or clear) the function that ships buffered spans.

    :mod:`boosthis.telemetry` sets this to ``_transmit_spans`` once effective
    meter-sharing is on and clears it (``None``) when sharing turns off or on
    ``forget()``. Clearing it makes :func:`enqueue_span` inert immediately — the
    privacy-by-default gate.
    """
    global _submitter
    with _state_lock:
        _submitter = fn


def has_submitter() -> bool:
    """True when a submitter is registered (i.e. meter-sharing is effectively
    on)."""
    with _state_lock:
        return _submitter is not None


def clear_buffered_spans() -> None:
    """Drop the in-memory span buffer (called by ``forget()``)."""
    with _state_lock:
        _buffer.clear()


def enqueue_span(span: dict[str, Any]) -> None:
    """Buffer one span for the next flush, then trigger a throttled flush.

    No-op unless a submitter is wired (sharing authorized) and the kill-switch is
    off — so nothing span-shaped is even RETAINED on a private app. Clamps the
    duration + start offset and drops the oldest span when the buffer is full.
    Never raises — instrumentation must stay silent.
    """
    if is_boosthis_disabled():
        return
    try:
        duration_ms = max(
            0, min(MAX_SPAN_DURATION_MS, int(round(span.get("durationMs", 0) or 0)))
        )
        start_offset_ms = max(
            0,
            min(MAX_SPAN_DURATION_MS, int(round(span.get("startOffsetMs", 0) or 0))),
        )
        clean = {
            "traceId": span.get("traceId"),
            "layer": span.get("layer"),
            "routeLabel": span.get("routeLabel"),
            "durationMs": duration_ms,
            "startOffsetMs": start_offset_ms,
            "rating": span.get("rating"),
        }
        span_id = sanitize_span_id(span.get("spanId"))
        if span_id is not None:
            clean["spanId"] = span_id
        parent_span_id = sanitize_span_id(span.get("parentSpanId"))
        if parent_span_id is not None:
            clean["parentSpanId"] = parent_span_id
        # What KIND of work this was and whether it WORKED — same rule as the
        # causality pair above: validated against the shared vocabulary and
        # ADDED ONLY WHEN PRESENT. A value the vocabulary does not know is
        # dropped here rather than sent, because a batch refused over a new
        # field is a silent outage; and a span with nothing to say sends
        # exactly the keys it always did, which reads as "this kit does not
        # report it" rather than as a clean result.
        kind = sanitize_work_kind(span.get("kind"))
        if kind is not None:
            clean["kind"] = kind
        outcome = sanitize_outcome(span.get("outcome"))
        if outcome is not None:
            clean["outcome"] = outcome
        with _state_lock:
            if _submitter is None:
                return
            _buffer.append(clean)
            while len(_buffer) > MAX_BUFFERED_SPANS:
                _buffer.pop(0)
    except Exception:  # noqa: BLE001
        return
    maybe_flush_spans()


def _flush_sync() -> int:
    """Ship up to one batch inline (no thread). Never raises. Returns the count
    the submitter reports, or 0 when there is no submitter / no data / an error.

    A batch the submitter reports as never having reached the network goes
    back on the queue, for the same reason the sample queue does: under the
    kill-switch or the ACTIVATION LOCK the transport answers locally and
    touches no socket, so dropping the batch would spend the spans on nothing.
    See ``sample_uploader.NOT_SENT``.
    """
    global _in_flight
    with _state_lock:
        if _submitter is None or _in_flight or not _buffer:
            return 0
        submitter = _submitter
        batch = _buffer[:MAX_SPAN_BATCH]
        del _buffer[: len(batch)]
        _in_flight = True
    try:
        sent = submitter(batch)
        if sent == NOT_SENT:
            with _state_lock:
                _buffer[:0] = batch
                while len(_buffer) > MAX_BUFFERED_SPANS:
                    _buffer.pop(0)
            return 0
        return sent
    except Exception:  # noqa: BLE001
        return 0
    finally:
        with _state_lock:
            _in_flight = False


def flush_spans_now() -> None:
    """Spawn a one-shot daemon thread to ship buffered spans immediately.

    Skips entirely under the kill-switch or in tests (where the submitter is
    invoked directly). No-op without a registered submitter or an empty buffer.
    """
    if is_boosthis_disabled() or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    with _state_lock:
        if _submitter is None or not _buffer:
            return
    threading.Thread(target=_flush_sync, daemon=True).start()


def flush_spans_blocking(budget_ms: float = 2_000.0) -> int:
    """Drain the span buffer INLINE, in the calling thread, inside a wall-clock
    budget. Returns how many spans the submitter reported shipping.

    The shutdown counterpart of :func:`flush_spans_now`, and the same contract
    as ``sample_uploader.flush_samples_blocking``: no thread (the interpreter
    freezes daemon threads on the way out rather than joining them), no
    test-runner skip, bounded by the budget, and it gives up after one failed
    batch. Never raises. See :mod:`boosthis.exit_flush`.
    """
    if is_boosthis_disabled():
        return 0
    sent = 0
    try:
        deadline = time.monotonic() + max(0.0, budget_ms) / 1000.0
        while time.monotonic() < deadline:
            with _state_lock:
                if not _in_flight:
                    break
            time.sleep(0.005)
        while time.monotonic() < deadline:
            with _state_lock:
                if _submitter is None or not _buffer:
                    break
            shipped = _flush_sync()
            if shipped <= 0:
                break
            sent += shipped
    except Exception:  # noqa: BLE001
        pass
    return sent


def maybe_flush_spans() -> None:
    """Work-gated, throttled flush trigger called from :func:`enqueue_span`.

    Fires at most once per :data:`SPAN_FLUSH_MS`. The throttle check is a single
    timestamp compare under a lock; the actual capture + HTTP upload happens on a
    daemon thread so the host request is never blocked. Never raises.
    """
    global _last_flush_at_ms
    if is_boosthis_disabled() or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    now = time.time() * 1000
    with _state_lock:
        if _submitter is None or not _buffer:
            return
        if now - _last_flush_at_ms < SPAN_FLUSH_MS:
            return
        _last_flush_at_ms = now
    try:
        threading.Thread(target=_flush_sync, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def _reset_for_tests() -> None:
    """Clear module state between hermetic test runs."""
    global _submitter, _last_flush_at_ms, _in_flight
    with _state_lock:
        _submitter = None
        _buffer.clear()
        _last_flush_at_ms = 0.0
        _in_flight = False


__all__ = [
    "MAX_SPAN_BATCH",
    "MAX_BUFFERED_SPANS",
    "MAX_SPAN_ROUTE_LABEL",
    "SPAN_FLUSH_MS",
    "MAX_SPAN_DURATION_MS",
    "rate_span_duration",
    "span_label",
    "set_span_submitter",
    "has_submitter",
    "clear_buffered_spans",
    "enqueue_span",
    "flush_spans_now",
    "flush_spans_blocking",
    "maybe_flush_spans",
]
