"""Per-request sample upload for the Python runtime.

Python parity for the Node runtime's sample queue (``reporting.ts`` +
``telemetry.transmitSamples``) and the web runtime's observer queue. Every
measurement the tracker records is buffered here and shipped, in batches, to
``POST /api/samples`` — the same ingest Node, web, RN and the edge kit already
use.

WHY THIS EXISTS: :mod:`boosthis.samples` is an in-process ring the local
surfaces read (the ``/_boosthis`` page, the MCP server, ``boosthis context``).
It never leaves the host. The aggregate :mod:`boosthis.snapshot_mirror` ships
the meter *page*, but a snapshot is one rolled-up picture — it cannot answer
"which route is busiest" or "how many measurements has this install taken",
which is what the dashboard's per-runtime row and the project card's busiest
routes are built from. Without this module a Python back end registers, meters
and reports for ever while its measurement count sits at zero, which reads to a
developer as a broken install.

PRIVACY: samples carry ONLY a code-defined route label (already cleared by
``check_route_label`` on the record path, re-checked in
``telemetry.transmit_samples``), a duration, and a rating bucket — never user
values or source. Uploading rides the SAME gate as the snapshot mirror and the
span emitter: :func:`enqueue_sample` is inert until :mod:`boosthis.telemetry`
wires a submitter, which it only does under effective meter-sharing (the
developer opted in via ``share_meter_with_ai`` OR the server reported the
directive after they connected an AI / switched full telemetry on). A private,
issues-only app therefore RETAINS nothing upload-shaped. ``BOOSTHIS_DISABLED``
and ``forget()`` both stop and clear it.

The fields here are a strict subset of what the span emitter already ships
under that identical gate (spans carry the same route label, duration and
rating), so this adds no new class of data to the wire.

UPLOAD MODEL: like the snapshot mirror and span emitter, the Python runtime has
no always-running event loop and Boosthis's own Python rule book forbids
idle-spinning background pollers. So flushing is **work-gated**: every
:func:`enqueue_sample` buffers the sample and, throttled to at most once per
:data:`SAMPLE_FLUSH_MS`, spawns a ONE-SHOT daemon thread to ship a batch —
never blocking the host request and never spinning when the app is idle.

Everything here is best-effort: sample bookkeeping must NEVER change the host
request's behaviour or crash the app.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

from boosthis.pii import check_route_label
from boosthis.runtime_flags import is_boosthis_disabled

#: Server accepts at most 100 samples per batch (``ingestSamplesBodySamplesMax``).
MAX_SAMPLE_BATCH = 100
#: Hard cap on the in-memory buffer (drop-oldest on overflow) so a burst of
#: measurements between flushes can never grow memory without bound.
MAX_BUFFERED_SAMPLES = 500
#: Work-gated flush throttle window (matches the span emitter's cadence).
SAMPLE_FLUSH_MS = 15_000
#: Duration hard clamp (matches the server ``durationMs`` 0..600000 bound).
MAX_SAMPLE_DURATION_MS = 600_000
#: The only rating buckets the server accepts.
ALLOWED_RATINGS = ("good", "needs-work", "poor")

#: Why a measurement this app recorded never joined the upload queue. A CLOSED
#: set, in the order the status page reads them.
#:
#: These are OUR refusals, not the server's: the sample never left the process,
#: so no upload reply can ever mention it. A queue that refuses in silence is
#: how a whole class of measurement — a background job named after the work it
#: does, say — turns into a project page that reads "registered, measuring
#: nothing" with no way to find out why. The REASON is what travels; the label
#: itself never leaves this module, not even into a log line.
REFUSAL_KEYS: tuple[str, ...] = (
    "routeNamePrivacy",
    "routeNameTooLong",
    "routeNameMissing",
    "ratingUnknown",
    "unreadable",
)

#: Short cause text, in the vocabulary the drop report already uses. Every way
#: out of :func:`enqueue_sample` that loses a measurement has one, including
#: the catch-all: a queue that stays quiet about the input it could not read is
#: the same silence in a smaller room.
REFUSAL_TEXT: dict[str, str] = {
    "routeNamePrivacy": "route names the privacy guard refused",
    "routeNameTooLong": "route names longer than 100 characters",
    "routeNameMissing": "measurements with no usable route name",
    "ratingUnknown": "ratings the server does not accept",
    "unreadable": "measurements this queue could not read",
}

#: What the developer changes to stop it. Second sentence of the warning.
REFUSAL_FIX: dict[str, str] = {
    "routeNamePrivacy": (
        'Name the work in code, the way perf("orders.get") is written \u2014 a '
        "name built out of a request value (an id, an email, an address with a "
        "space in it) is refused here rather than uploaded."
    ),
    "routeNameTooLong": (
        "Use a code-defined route name of 100 characters or fewer."
    ),
    "routeNameMissing": "Pass a code-defined name string to the measurement.",
    "ratingUnknown": "A rating is good, needs-work or poor.",
    "unreadable": (
        "A measurement is a mapping with a name, a numeric duration in "
        "milliseconds and a rating."
    ),
}

#: What a submitter returns to say "this batch never reached the network, keep
#: it" — as opposed to ``0``, which means the send happened and nothing was
#: accepted. The only thing that answers this today is an upload stopped by the
#: kill-switch or the ACTIVATION LOCK. Negative on purpose: no count is
#: negative, so no honest submitter can return it by accident.
NOT_SENT = -1

#: Given a batch, returns how many rows were accepted, or :data:`NOT_SENT`.
SampleSubmitter = Callable[[list[dict[str, Any]]], int]

_submitter: SampleSubmitter | None = None
_buffer: list[dict[str, Any]] = []
_last_flush_at_ms: float = 0.0
_in_flight = False
_refused_counts: dict[str, int] = {}
_warned_refusals: set[str] = set()
_state_lock = threading.Lock()


def set_sample_submitter(fn: SampleSubmitter | None) -> None:
    """Register (or clear) the function that ships buffered samples.

    :mod:`boosthis.telemetry` sets this to ``_transmit_sample_batch`` once
    effective meter-sharing is on and clears it (``None``) when sharing turns
    off, on ``disable()`` or on ``forget()``. Clearing it makes
    :func:`enqueue_sample` inert immediately — the privacy-by-default gate.
    """
    global _submitter
    with _state_lock:
        _submitter = fn


def has_submitter() -> bool:
    """True when a submitter is registered (i.e. meter-sharing is effectively
    on)."""
    with _state_lock:
        return _submitter is not None


def buffered_count() -> int:
    """How many samples are waiting to be shipped. Read by tests and the
    kit's own status surfaces."""
    with _state_lock:
        return len(_buffer)


def local_refusal_count() -> int:
    """How many measurements this kit itself refused to upload since the
    process started (0 on a healthy app)."""
    with _state_lock:
        return sum(_refused_counts.values())


def local_refusal_reasons() -> list[str]:
    """The refusal reasons seen so far, in the fixed reason order."""
    with _state_lock:
        return [k for k in REFUSAL_KEYS if _refused_counts.get(k)]


def local_refusal_summary() -> str:
    """The status-page figure: ``"3 — route names the privacy guard refused"``.

    Empty string when nothing has been refused, so a healthy app shows no row
    at all. Never contains a label — only the closed cause words.
    """
    total = local_refusal_count()
    if total <= 0:
        return ""
    causes = [REFUSAL_TEXT[k] for k in local_refusal_reasons()]
    if not causes:
        return str(total)
    return f"{total} \u2014 {'; '.join(causes)}"


def _note_refusal(reason: str) -> None:
    """Count one measurement this queue would not upload, and say so ONCE.

    The count is what the kit's own page shows; the one-shot log line is what a
    developer can actually act on, because a tally cannot be diagnosed. Ungated
    and never repeated, in the same style as the server-side drop report. Never
    raises, and never logs the label itself.
    """
    with _state_lock:
        if _submitter is None:
            # The gate shut while this measurement was in flight (disable,
            # forget, or sharing turned off). Nothing was going to be uploaded
            # after all, so there is nothing to report.
            return
        _refused_counts[reason] = _refused_counts.get(reason, 0) + 1
        count = _refused_counts[reason]
        first = reason not in _warned_refusals
        if first:
            _warned_refusals.add(reason)
    if not first:
        return
    try:
        logging.getLogger("boosthis").warning(
            "[boosthis] Boosthis is not uploading %d of the measurements this "
            "app recorded: %s. %s",
            count,
            REFUSAL_TEXT[reason],
            REFUSAL_FIX[reason],
        )
    except Exception:  # noqa: BLE001
        pass


def clear_buffered_samples() -> None:
    """Drop the in-memory upload buffer (called by ``forget()``)."""
    with _state_lock:
        _buffer.clear()


def enqueue_sample(sample: dict[str, Any]) -> None:
    """Buffer one measurement for the next flush, then trigger a throttled flush.

    No-op unless a submitter is wired (sharing authorized) and the kill-switch
    is off — so nothing upload-shaped is even RETAINED on a private app. Clamps
    the duration, refuses an over-long label, drops a sample whose rating is not one the
    server accepts (a malformed batch would cost the whole upload), and drops
    the oldest sample when the buffer is full. Never raises — instrumentation
    must stay silent.
    """
    if is_boosthis_disabled():
        return
    # Read the gate FIRST. A refusal below is only worth counting once this app
    # is actually uploading: on a private app nothing was going to be sent
    # anyway, and a privacy tally there would report a problem that does not
    # exist.
    with _state_lock:
        if _submitter is None:
            return
    try:
        raw_label = sample.get("routeLabel")
        # A non-string label is a caller bug, not a route: stringifying it would
        # file a measurement against a label like "None".
        if not isinstance(raw_label, str):
            _note_refusal("routeNameMissing")
            return
        if not raw_label:
            _note_refusal("routeNameMissing")
            return
        from boosthis import route_inventory

        if len(raw_label.strip()) > route_inventory.MAX_PART_NAME:
            _note_refusal("routeNameTooLong")
            return
        label = route_inventory.normalize_part_name(raw_label)
        if label is None:
            _note_refusal("routeNamePrivacy")
            return
        # Route-label PII guard AT THE QUEUE. The tracker guards its own labels,
        # but a measurement also arrives from background jobs, MCP tools and the
        # kit's local /record endpoint, whose labels this queue has never seen.
        # The wire-level check in telemetry.transmit_samples refuses the WHOLE
        # batch on one bad label, which would cost every other measurement in it.
        rating = sample.get("rating")
        if rating not in ALLOWED_RATINGS:
            _note_refusal("ratingUnknown")
            return
        duration_ms = max(
            0, min(MAX_SAMPLE_DURATION_MS, int(round(sample.get("durationMs", 0) or 0)))
        )
        clean: dict[str, Any] = {
            "routeLabel": label,
            "durationMs": duration_ms,
            "rating": rating,
        }
        with _state_lock:
            if _submitter is None:
                return
            _buffer.append(clean)
            while len(_buffer) > MAX_BUFFERED_SAMPLES:
                _buffer.pop(0)
    except Exception:  # noqa: BLE001
        # A duration that will not convert, a sample that is not a mapping —
        # still a measurement that stops here, so it is still counted. Silence
        # in this branch is how a whole malformed-input class would stay
        # invisible behind the very contract this module just adopted.
        _note_refusal("unreadable")
        return
    maybe_flush_samples()


def _requeue(batch: list[dict[str, Any]]) -> None:
    """Put a batch that never left the process back at the FRONT of the queue,
    oldest first, still bounded by :data:`MAX_BUFFERED_SAMPLES`.

    Only ever reached for :data:`NOT_SENT` — see :func:`_flush_sync` for why
    that is the one case a batch comes back.
    """
    with _state_lock:
        _buffer[:0] = batch
        while len(_buffer) > MAX_BUFFERED_SAMPLES:
            _buffer.pop(0)


def _flush_sync() -> int:
    """Ship up to one batch inline (no thread). Never raises. Returns the count
    the submitter reports, or 0 when there is no submitter / no data / an error.

    A batch the server REFUSES is not re-queued: the buffer is drained on
    capture, exactly like the span emitter. Re-queuing a refused batch behind a
    still-filling buffer is how an unreachable server turns into unbounded
    memory in a host app we do not own.

    A batch that never reached the network at all is a different thing, and
    this is the distinction that cost one real project every measurement it
    ever took. While the ACTIVATION LOCK is on — which it is from process start
    until an entitlement check-in comes back — the internal transport answers
    each send locally and touches no socket. It used to answer with a synthetic
    ``204``, so the submitter read "accepted", the batch was dropped, and the
    kit's own counters said the measurements had shipped. The launch check-in
    runs on a daemon thread; the first measurement's work-gated flush runs on
    another; whichever won that race decided whether the project ever saw a
    number, and a process that lives half a second loses it every time.

    So the submitter now says which of the two happened, and a send that never
    happened costs nothing: the batch goes back and the next flush — usually
    the one :mod:`boosthis.exit_flush` runs after it has waited for activation —
    takes it. The queue keeps its own bound either way.
    """
    global _in_flight
    with _state_lock:
        if _submitter is None or _in_flight or not _buffer:
            return 0
        submitter = _submitter
        batch = _buffer[:MAX_SAMPLE_BATCH]
        del _buffer[: len(batch)]
        _in_flight = True
    try:
        sent = submitter(batch)
        if sent == NOT_SENT:
            _requeue(batch)
            return 0
        return sent
    except Exception:  # noqa: BLE001
        return 0
    finally:
        with _state_lock:
            _in_flight = False


def flush_samples_now() -> None:
    """Spawn a one-shot daemon thread to ship buffered samples immediately.

    Skips entirely under the kill-switch or in tests (where the submitter is
    invoked directly). No-op without a registered submitter or an empty buffer.
    """
    if is_boosthis_disabled() or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    with _state_lock:
        if _submitter is None or not _buffer:
            return
    threading.Thread(target=_flush_sync, daemon=True).start()


def flush_samples_blocking(budget_ms: float = 2_000.0) -> int:
    """Drain the buffer INLINE, in the calling thread, inside a wall-clock
    budget. Returns how many measurements the submitter reported shipping.

    This is the flush for a process on its way out (see
    :mod:`boosthis.exit_flush`) and for a short-lived worker that wants to hand
    its measurements over before it returns. It differs from
    :func:`flush_samples_now` in the two ways that matter at shutdown:

    * it does NOT spawn a thread — a ``daemon=True`` thread is frozen rather
      than joined when the interpreter exits, so an upload started on one a
      moment before exit is simply lost;
    * it does not skip under the test runner, because a test that cannot call
      it cannot check it.

    Bounded on purpose. It waits briefly for an in-flight background batch
    rather than exiting on top of it, stops the moment the budget is spent, and
    gives up after ONE failed batch: an unreachable server must cost a shutdown
    a fixed couple of seconds, never a batch-by-batch grind through the whole
    buffer. Never raises.
    """
    if is_boosthis_disabled():
        return 0
    sent = 0
    try:
        deadline = time.monotonic() + max(0.0, budget_ms) / 1000.0
        # A work-gated flush may already be mid-upload on a daemon thread. Give
        # it a moment to land instead of returning 0 and letting the process
        # exit out from under it.
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
                # The batch is gone either way — this queue never re-queues a
                # refused batch — so the only question is whether to keep
                # paying the shutdown budget for a submitter that just failed.
                break
            sent += shipped
    except Exception:  # noqa: BLE001
        pass
    return sent


def maybe_flush_samples() -> None:
    """Work-gated, throttled flush trigger called from :func:`enqueue_sample`.

    Fires at most once per :data:`SAMPLE_FLUSH_MS`, and immediately once a full
    batch is waiting so a busy app ships at the rate it measures instead of
    dropping the overflow. The throttle check is a single timestamp compare
    under a lock; the actual capture + HTTP upload happens on a daemon thread so
    the host request is never blocked. Never raises.
    """
    global _last_flush_at_ms
    if is_boosthis_disabled() or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    now = time.time() * 1000
    with _state_lock:
        if _submitter is None or not _buffer:
            return
        due = now - _last_flush_at_ms >= SAMPLE_FLUSH_MS
        full = len(_buffer) >= MAX_SAMPLE_BATCH
        if not due and not full:
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
        _refused_counts.clear()
        _warned_refusals.clear()


__all__ = [
    "MAX_SAMPLE_BATCH",
    "MAX_BUFFERED_SAMPLES",
    "MAX_SAMPLE_ROUTE_LABEL",
    "SAMPLE_FLUSH_MS",
    "MAX_SAMPLE_DURATION_MS",
    "ALLOWED_RATINGS",
    "REFUSAL_KEYS",
    "REFUSAL_TEXT",
    "REFUSAL_FIX",
    "set_sample_submitter",
    "has_submitter",
    "buffered_count",
    "local_refusal_count",
    "local_refusal_reasons",
    "local_refusal_summary",
    "clear_buffered_samples",
    "enqueue_sample",
    "flush_samples_now",
    "flush_samples_blocking",
    "maybe_flush_samples",
]
