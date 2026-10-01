"""The decorator + context manager — the Python equivalent of `usePerfTracker`.

    from boosthis import track_perf, perf

    @track_perf("orders.get")
    def get_order(order_id): ...

    with perf("db.heavy_query"):
        rows = session.execute(query).all()
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generator, TypeVar, overload

from boosthis import (
    repeated_work,
    samples,
    snapshot_mirror,
    span_emitter,
)
from boosthis.pii import assert_no_pii, check_route_label
from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.score import get_duration_rating
from boosthis.thresholds import Rating
from boosthis.trace import (
    get_parent_span_id,
    get_span_id,
    get_trace_id,
    is_valid_trace_id,
    span_start_offset,
)

logger = logging.getLogger("boosthis")

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class PerfResult:
    """Result of a single perf measurement."""

    name: str
    duration_ms: int
    rating: Rating


@contextmanager
def perf(name: str) -> Generator[None, None, None]:
    """Context manager that times the wrapped block and logs the result.

        with perf("db.load_user"):
            user = db.get(User, user_id)
    """
    t0 = time.perf_counter()
    # Whether the timed block COMPLETED. This is the one thing a `with` block
    # can honestly say about its own work: it either returned or it threw. The
    # KIND of work stays unsaid — only the host knows whether "db.load_user"
    # was a query or a cache read, and guessing from the label would be a
    # reading invented from a string.
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        dt_ms = int(round((time.perf_counter() - t0) * 1000))
        _emit(name, dt_ms, outcome=outcome)


@overload
def track_perf(name_or_fn: str) -> Callable[[F], F]: ...
@overload
def track_perf(name_or_fn: F) -> F: ...
def track_perf(name_or_fn: str | F) -> Any:
    """Decorator that times a function (sync OR async) and logs the result.

    Usage with explicit name (recommended — name is the routing key, like RN's
    ``usePerfTracker("orders/[id]")``)::

        @track_perf("orders.get")
        async def get_order(order_id: int): ...

    Usage without arguments — name is derived from ``module.qualname``::

        @track_perf
        def slow_helper(): ...
    """

    # Form: @track_perf  (no parentheses)
    if callable(name_or_fn) and not isinstance(name_or_fn, str):
        fn = name_or_fn
        return _wrap(fn, _default_name(fn))

    # Form: @track_perf("name")
    name = name_or_fn

    def decorator(fn: F) -> F:
        return _wrap(fn, name)

    return decorator


def _default_name(fn: Callable[..., Any]) -> str:
    module = getattr(fn, "__module__", "") or "?"
    qual = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", "?")
    return f"{module}.{qual}"


def _wrap(fn: F, name: str) -> F:
    if asyncio.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            # Same as perf(): the wrapper stands around the call, so it can say
            # whether the call came back. It cannot say what kind of work the
            # call did, and does not pretend to.
            outcome = "ok"
            try:
                return await fn(*args, **kwargs)
            except BaseException:
                outcome = "error"
                raise
            finally:
                dt_ms = int(round((time.perf_counter() - t0) * 1000))
                _emit(name, dt_ms, (args, kwargs), outcome=outcome)

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        outcome = "ok"
        try:
            return fn(*args, **kwargs)
        except BaseException:
            outcome = "error"
            raise
        finally:
            dt_ms = int(round((time.perf_counter() - t0) * 1000))
            _emit(name, dt_ms, (args, kwargs), outcome=outcome)

    return sync_wrapper  # type: ignore[return-value]


def _emit(
    name: str,
    dt_ms: int,
    call_inputs: tuple[Any, Any] | None = None,
    *,
    kind: str | None = None,
    outcome: str | None = None,
) -> None:
    # ``kind`` and ``outcome`` are what the CALLER could honestly see: a
    # framework adapter measuring a request knows it was a handler and knows
    # the status it answered with; a hand-placed perf() block knows only
    # whether it threw. Both default to None — nothing said — and travel no
    # further than the span. Neither touches the sample, the rating or the
    # score: this is a description of the work, not a judgement of it.
    # Global kill-switch: BOOSTHIS_DISABLED=1 makes every track_perf / perf()
    # call a true no-op — nothing recorded, nothing logged, no PII check.
    if is_boosthis_disabled():
        return
    # Repeated-work detector: @track_perf is the one wrapper that sees BOTH the
    # call target and its arguments, so it is where "the same call twice in one
    # request" can honestly be recognised — and it passes them in as
    # ``call_inputs``. perf() passes None: it has no visible inputs, so two
    # perf("db.load_user") blocks may be two DIFFERENT users, and guessing they
    # are the same work would be a lie. The name + arguments are hashed on this
    # stack and dropped; only the resulting count ever leaves. Runs before the
    # label guard below — a label we refuse to RECORD is still a real call, and
    # the hash carries no text either way. Never raises.
    if call_inputs is not None:
        try:
            args, kwargs = call_inputs
            repeated_work.note_watched_call(
                repeated_work.call_identity(name, args, kwargs), dt_ms
            )
        except Exception:  # noqa: BLE001
            pass  # instrumentation must never disturb the host app.
    rating = get_duration_rating(dt_ms)
    # Route-label PII guard: apply the stricter check_route_label() first so
    # labels containing whitespace ("Jane Doe"), UUIDs, long numeric IDs, emails,
    # etc. are caught before the sample is recorded or logged. Mirrors the RN
    # runtime's routeLabelHasPII filter. Silently skip rather than raise so
    # instrumentation failures never take down the host app.
    if check_route_label(name) is not None:
        return
    # Generic PII guard: the name is meant to be a code-defined route key.
    # assert_no_pii() throws if someone passes e.g. an email-looking string.
    assert_no_pii({"screen": name, "duration_ms": dt_ms})
    # Skip "pending" — buffer only stores measured ratings (good/needs-work/poor)
    measured = rating if rating != "pending" else "good"
    # Recording is also what queues the per-request upload (see samples.record):
    # the batched POST /api/samples that fills the dashboard's per-runtime
    # measurement count and the project card's busiest routes. It rides the same
    # already-guarded label and measured rating, is inert unless telemetry.py has
    # wired a submitter under effective meter-sharing, and never blocks.
    samples.record(name, dt_ms, measured)  # type: ignore[arg-type]
    # Coverage freshness. This is the ONE path every measured unit of work
    # passes through in every mode, which is why the "what are we blind to"
    # answer is re-checked here rather than on an upload: a private
    # (issues-only) app ships no samples and no snapshot, so a job library met
    # lazily, a database opened at the first request, or a refused attach would
    # otherwise never reach the dashboard. Change-only, throttled, and the
    # round trip is handed to a daemon thread — see telemetry.note_measured_work.
    try:
        from boosthis import telemetry as _telemetry

        _telemetry.note_measured_work()
    except Exception:  # noqa: BLE001
        pass  # instrumentation must never disturb the host app.
    logger.info("[boosthis] %s · %sms · %s", name, dt_ms, rating)
    # Throttled perf-snapshot mirror: when the developer has enabled meter
    # sharing (or connected an AI from the web dashboard), this ships the latest
    # privacy-safe snapshot so their own AI can read the live picture. It is a
    # true no-op unless a submitter is registered (telemetry.py wires it only
    # under effective share) and self-throttles, so the hot path stays cheap.
    # Never raises — snapshot failures must not disturb the host app.
    snapshot_mirror.maybe_flush_snapshot()
    # Full-stack trace (Stage 2): emit ONE privacy-safe span for this
    # measurement, correlated to the current request's trace id when
    # BoosthisTraceMiddleware (or set_trace_id) has set one. Reuse the SAME
    # already-guarded label + measured rating + duration the sample recorded.
    # A measurement with no trace context (worker/CLI/undecorated request) emits
    # no span — there is nothing to correlate to. enqueue_span is itself inert
    # unless meter sharing is authorized + the kill-switch is off, so a private
    # app buffers nothing span-shaped. Never raises.
    tid = get_trace_id()
    if is_valid_trace_id(tid):
        span_emitter.enqueue_span(
            {
                "traceId": tid,
                "layer": "py",
                "routeLabel": span_emitter.span_label(name),
                "durationMs": dt_ms,
                # Offset from the trace root at which this measurement STARTED:
                # elapsed base adopted on receive + (time-in-request − its own
                # duration). 0 outside any request context. Relative-only.
                "startOffsetMs": span_start_offset(dt_ms),
                "rating": measured,
                "spanId": get_span_id(),
                "parentSpanId": get_parent_span_id(),
                # Absent unless the caller could see them; enqueue_span drops
                # a None rather than sending one, so a span that has nothing
                # to say is byte-identical to what this kit sent before.
                "kind": kind,
                "outcome": outcome,
            }
        )
