"""Boosthis: host-suspend sensor (Python).

Python sibling of ``lib/boosthis-runtime-node/src/suspendSensor.ts``.

Scale-to-zero / autoscale hosts SUSPEND the container between requests: timers
fire tens of seconds late, CFS throttling accrues while nothing is being served,
and the first requests after a wake-up pay the resume cost. The kit's honesty
axes (eventLoopLag / asyncioLoopLag / latencyFloor / cpuThrottling) would
otherwise record those gaps as terrifying reds even though no user-facing work
was ever stalled.

This module is the SHARED detector those axes consult so they can DISCOUNT
(never hide) suspend artifacts:

  * A request arriving after >= SUSPEND_GAP_MS with zero activity is a WAKE —
    the idle gap before it is where a suspend can have happened.
  * For WAKE_WINDOW_MS after a wake, the process is "waking": request latencies
    in that window reflect resume cost, not steady-state code. (Prod evidence:
    every >=10s request fell within 60s of a wake after a >=10s idle gap; zero
    during sustained traffic.)

Consumers subscribe to three boundaries:
  on_suspend_idle_start — the loop just went idle (snapshot clean baselines).
  on_suspend_wake       — a request arrived after a suspicious idle gap
                          (attribute whatever accrued in the gap to suspend).
  on_suspend_activity   — every request arrival (refresh traffic-time marks).

Reuses the EXISTING request boundaries ``live_detectors`` already tracks — no
new timer, no background poller, bounded state. PRIVACY: timestamps and counts
only; nothing here ever sees a route, URL, or customer string.
"""

from __future__ import annotations

import time
from typing import Callable, List, Tuple

from .runtime_flags import is_boosthis_disabled

# An idle gap at/above this (ms) is long enough for the host to have suspended
# the container; the wake after it opens a discount window. Byte-identical to
# the Node sibling.
SUSPEND_GAP_MS = 10_000
# How long (ms) after a wake request latencies are attributed to resume cost
# rather than the app's own code. Byte-identical to the Node sibling.
WAKE_WINDOW_MS = 60_000

# Bounded ring of recent post-wake windows (start_ms, end_ms) so the
# latency-floor reader can tell whether a given sample landed inside resume-cost
# territory. Bounded so a long-lived process can never grow this unbounded.
_WAKE_WINDOW_RING_CAP = 256

Listener = Callable[[float], None]

_last_activity_ms = 0.0
_wake_until_ms = 0.0
_wake_count = 0
_wake_windows: List[Tuple[float, float]] = []

_idle_start_listeners: List[Listener] = []
_wake_listeners: List[Listener] = []
_activity_listeners: List[Listener] = []


def _now_ms() -> float:
    return time.time() * 1000.0


def _fire(listeners: List[Listener], gap_ms: float) -> None:
    for fn in listeners:
        try:
            fn(gap_ms)
        except Exception:
            # a consumer's bookkeeping must never break the host's request path
            pass


def note_suspend_activity() -> None:
    """Call on EVERY request arrival (same boundary as ``live_detectors``'
    ``note_request_start``). Detects wakes after suspicious idle gaps, then lets
    consumers refresh their traffic-time marks. No-op under the kill-switch.
    Never raises."""
    if is_boosthis_disabled():
        return
    global _last_activity_ms, _wake_until_ms, _wake_count
    now = _now_ms()
    if _last_activity_ms > 0:
        gap = now - _last_activity_ms
        if gap >= SUSPEND_GAP_MS:
            _wake_count += 1
            _wake_until_ms = now + WAKE_WINDOW_MS
            _wake_windows.append((now, _wake_until_ms))
            if len(_wake_windows) > _WAKE_WINDOW_RING_CAP:
                del _wake_windows[0 : len(_wake_windows) - _WAKE_WINDOW_RING_CAP]
            _fire(_wake_listeners, gap)
    _last_activity_ms = now
    _fire(_activity_listeners, 0)


def note_suspend_idle_start() -> None:
    """Call when the loop returns to idle (zero requests in flight — same
    boundary as ``live_detectors``' idle transition). Consumers snapshot their
    clean "everything up to now was traffic time" baselines here. No-op under
    the kill-switch. Never raises."""
    if is_boosthis_disabled():
        return
    global _last_activity_ms
    _last_activity_ms = _now_ms()
    _fire(_idle_start_listeners, 0)


def in_suspend_wake_window(now_ms: float | None = None) -> bool:
    """True while inside the current post-wake window (resume-cost territory)."""
    now = now_ms if now_ms is not None else _now_ms()
    return _wake_until_ms > 0 and now <= _wake_until_ms


def sample_in_wake_window(ts_ms: float) -> bool:
    """True when a sample recorded at ``ts_ms`` (epoch ms) landed inside ANY
    recorded post-wake window — resume cost, not the app's own latency. Used by
    the latency-floor reader (which sees historical sample timestamps, not just
    "now") to exclude and count suspend-attributed samples."""
    for start, end in _wake_windows:
        if start <= ts_ms <= end:
            return True
    return False


def get_suspend_wake_count() -> int:
    """How many suspend-suspicious wakes have been seen this session."""
    return _wake_count


def on_suspend_idle_start(fn: Listener) -> None:
    _idle_start_listeners.append(fn)


def on_suspend_wake(fn: Listener) -> None:
    _wake_listeners.append(fn)


def on_suspend_activity(fn: Listener) -> None:
    _activity_listeners.append(fn)


def clear_suspend_sensor() -> None:
    """Wipe sensor state (wired into the same ``forget()`` path as the other
    server meters). Listener registrations survive — they are module wiring,
    not data. Idempotent. Never raises."""
    global _last_activity_ms, _wake_until_ms, _wake_count
    _last_activity_ms = 0.0
    _wake_until_ms = 0.0
    _wake_count = 0
    _wake_windows.clear()


# ── @internal test hooks ────────────────────────────────────────────────────
def _reset_for_tests() -> None:
    clear_suspend_sensor()


def _set_wake_window_for_test(until_ms: float) -> None:
    """Force the sensor into (or out of) a wake window without wall-clock games."""
    global _wake_until_ms
    _wake_until_ms = until_ms


def _push_wake_window_for_test(start_ms: float, end_ms: float) -> None:
    """Record a post-wake window interval directly (latency-floor tests)."""
    _wake_windows.append((start_ms, end_ms))


def _set_last_activity_for_test(ms: float) -> None:
    """Backdate the last-activity mark so the next activity registers a gap."""
    global _last_activity_ms
    _last_activity_ms = ms


_suspend_sensor_internals = {
    "SUSPEND_GAP_MS": SUSPEND_GAP_MS,
    "WAKE_WINDOW_MS": WAKE_WINDOW_MS,
}
