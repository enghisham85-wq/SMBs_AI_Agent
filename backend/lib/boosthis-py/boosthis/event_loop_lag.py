"""Boosthis: event-loop lag axis (Python).

Python sibling of ``lib/boosthis-runtime-node/src/eventLoopLag.ts``. A fifth
honest, display-only meter: how far the asyncio event loop is *delayed* beyond
when its ready callbacks were scheduled to run. Scheduling delay is the truest
single signal of a blocked/overloaded async process — a sync DB call inside a
coroutine, a huge ``json.loads`` on the loop thread, a tight CPU loop, or GC
pressure all inflate it even when the per-route timer (which times only the
handlers Boosthis wraps) sees nothing.

MEASUREMENT (no background poller): Node reads libuv's native
``monitorEventLoopDelay`` histogram. Python has no such native primitive, and
Boosthis's own rule book forbids idle-spinning background pollers, so instead we
probe at REQUEST BOUNDARIES only: :func:`sample_event_loop_lag` (called from the
ASGI trace middleware) schedules one ``loop.call_soon`` and measures how long
that already-ready callback waited before the loop dispatched it. Under a busy /
blocked loop that wait grows; on an idle loop it is ~0. Because the probe fires
only when a request arrives, it never spins when the app is idle.

ASYNC-ONLY BY DESIGN: the probe needs a running asyncio loop. A synchronous WSGI
app has none, so :func:`sample_event_loop_lag` no-ops and the axis is simply
OMITTED (the server renders an absent axis as pending) — exactly as the Node
module omits it when the native histogram is unavailable.

HOST-SUSPEND HONESTY: a scale-to-zero host suspends the container between
requests, so a callback scheduled just before a suspend is not dispatched until
the host wakes tens of seconds later — the probe would record a monster "freeze"
no user ever felt. Via the shared suspend sensor, at RING RECORD TIME a sample
attributable to a suspend gap is EXCLUDED from the ring and counted: a sample is
suspect when it was recorded while inside the post-wake window, or when its own
delay is itself >= SUSPEND_GAP_MS (the callback waited through a whole suspend).
The axis (and the ``asyncioLoopLag`` sibling that reads the same ring) then
reports ``suspendDiscounts`` so the discount is visible, never silent. A stall
DURING traffic still lands in the ring and still turns the tile red.

PRIVACY: the axis carries ONLY numbers (p50/p99/max loop delay in ms, a sample
count, a 0..100 score, a suspend-discount count) plus a fixed rating bucket —
never a route name, value, timestamp of user activity, or source. It rides
inside the existing snapshot upload and clears the same PII guard as every other
field.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from .runtime_flags import is_boosthis_disabled
from .health_axes import linear_score, rating_for
from . import extra_meters
from .suspend_sensor import SUSPEND_GAP_MS, in_suspend_wake_window

# Lag score bands (ms) — axis-specific and deliberately NOT the shared
# TTFF/TTI/FID thresholds: a p99 scheduling delay of ~50ms is still smooth,
# ~250ms is a clearly blocked loop. ``linear_score`` maps <=good -> 100,
# >=poor -> 0. Byte-identical to the Node sibling.
LAG_GOOD_MS = 50
LAG_POOR_MS = 250

# Below this many probes the axis is OMITTED rather than reporting a score
# derived from one or two requests on a just-booted process.
MIN_LAG_SAMPLES = 20

# Bounded ring of recent delay samples (ms) so a long-lived process can never
# grow this unbounded; oldest are dropped past the cap.
MAX_LAG_SAMPLES = 500

_armed = False
_samples: List[float] = []
# Host-suspend discount count: probes dropped at ring-record time because they
# were attributable to a suspend gap (recorded inside the post-wake window, or a
# delay itself >= SUSPEND_GAP_MS). Kept visible on the axis — discount, don't
# hide. Purely a counter; no timestamp or route ever enters it.
_suspend_discounts = 0


def start_event_loop_lag() -> None:
    """Arm request-boundary loop-lag sampling. Idempotent, no-op under the
    kill-switch. Called from ``enable_telemetry`` so probes don't accrue before
    the developer has turned Boosthis on."""
    global _armed
    if is_boosthis_disabled():
        return
    _armed = True


def sample_event_loop_lag() -> None:
    """Fire one scheduling-delay probe. Called at each request boundary from the
    ASGI trace middleware. No-op when disarmed, under the kill-switch, or when NO
    asyncio loop is running (sync WSGI apps never accrue samples, so the axis is
    omitted). Never raises — a probe must never disturb the host app."""
    if not _armed or is_boosthis_disabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop (sync app) -> omit the axis entirely
    try:
        t0 = loop.time()

        def _record() -> None:
            global _suspend_discounts
            try:
                delay_ms = (loop.time() - t0) * 1000.0
                if delay_ms < 0:
                    delay_ms = 0.0
                # Host-suspend discount (at RING RECORD time): if this sample is
                # attributable to a scale-to-zero host suspending the container —
                # recorded inside the post-wake window, or a delay itself
                # >= SUSPEND_GAP_MS (the callback waited through a whole suspend)
                # — it reflects resume cost, not the app's own code. Exclude it
                # from the ring and count it (discount, don't hide). A genuine
                # stall DURING traffic clears neither test and still lands here.
                if in_suspend_wake_window() or delay_ms >= SUSPEND_GAP_MS:
                    _suspend_discounts += 1
                    return
                _samples.append(delay_ms)
                if len(_samples) > MAX_LAG_SAMPLES:
                    del _samples[0 : len(_samples) - MAX_LAG_SAMPLES]
                # Feed the blockingAsync meter: a big scheduling delay in an
                # async app is sync work hogging the loop thread.
                extra_meters.note_loop_lag_sample(delay_ms)
            except Exception:
                pass

        loop.call_soon(_record)
    except Exception:
        pass  # never let the probe disturb the host app


def _percentile(sorted_vals: List[float], p: float) -> float:
    """Nearest-rank percentile over an already-sorted list. Empty -> 0."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    rank = int(round((p / 100.0) * (n - 1)))
    rank = max(0, min(n - 1, rank))
    return sorted_vals[rank]


def read_event_loop_lag() -> Optional[Dict[str, Any]]:
    """Current lag axis, or None when disarmed / not enough samples (the
    snapshot then omits the axis). Pure read — never mutates state. Never
    raises."""
    if is_boosthis_disabled() or not _armed:
        return None
    try:
        count = len(_samples)
        if count < MIN_LAG_SAMPLES:
            return None
        ordered = sorted(_samples)
        lag_p50 = round(_percentile(ordered, 50))
        lag_p99 = round(_percentile(ordered, 99))
        lag_max = round(ordered[-1])
        # Score the p99: the tail delay is what users feel as jank.
        score = linear_score(lag_p99, LAG_GOOD_MS, LAG_POOR_MS)
        axis: Dict[str, Any] = {
            "score": score,
            "rating": rating_for(score),
            "lagP50Ms": lag_p50,
            "lagP99Ms": lag_p99,
            "lagMaxMs": lag_max,
            "sampleCount": count,
        }
        # Host-suspend honesty: report the discount only when > 0 (discount,
        # don't hide). Numeric-only field — the server allowlists exactly this.
        if _suspend_discounts > 0:
            axis["suspendDiscounts"] = _suspend_discounts
        return axis
    except Exception:
        return None


def loop_lag_percentiles() -> Optional[Dict[str, Any]]:
    """Return {p50, p99, count} over the current lag samples, or None when
    disarmed / not enough samples. Reuses the SAME request-boundary probe ring
    as :func:`read_event_loop_lag` — no new sampling — so the ``asyncioLoopLag``
    axis costs nothing extra. Pure read. Never raises."""
    if is_boosthis_disabled() or not _armed:
        return None
    try:
        count = len(_samples)
        if count < MIN_LAG_SAMPLES:
            return None
        ordered = sorted(_samples)
        return {
            "p50": round(_percentile(ordered, 50)),
            "p99": round(_percentile(ordered, 99)),
            "count": count,
            # Suspend discounts applied to the shared ring — surfaced so the
            # asyncioLoopLag axis can carry the same honesty note as eventLoopLag.
            "suspendDiscounts": _suspend_discounts,
        }
    except Exception:
        return None


def clear_event_loop_lag() -> None:
    """Disarm + wipe sample state (wired into ``forget()`` so nothing
    Boosthis-shaped keeps sampling after erasure)."""
    global _armed, _suspend_discounts
    _armed = False
    _samples.clear()
    _suspend_discounts = 0


def _sample_for_tests(delay_ms: float) -> None:
    """@internal test hook — inject a synthetic delay sample."""
    _samples.append(float(delay_ms))
    if len(_samples) > MAX_LAG_SAMPLES:
        del _samples[0 : len(_samples) - MAX_LAG_SAMPLES]


def _arm_for_tests() -> None:
    """@internal test hook — arm without going through enable_telemetry."""
    global _armed
    _armed = True


_event_loop_lag_internals = {
    "LAG_GOOD_MS": LAG_GOOD_MS,
    "LAG_POOR_MS": LAG_POOR_MS,
    "MIN_LAG_SAMPLES": MIN_LAG_SAMPLES,
    "MAX_LAG_SAMPLES": MAX_LAG_SAMPLES,
}
