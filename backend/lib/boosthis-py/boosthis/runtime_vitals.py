"""Boosthis: runtime-vitals axes (Python).

Python sibling of ``lib/boosthis-runtime-node/src/runtimeVitals.ts``. Three more
honest, display-only backend meters, added so the Python/Node/Go/Java dashboards
expose a comparable set of gauges to the RN kit (the owner's request: "RN shows
many meters, the others show few"). Each is derived from cheap, always-available
runtime reads — NO background poller (Boosthis's own rule book forbids idle
spinners), NO new collection, NO PII:

- **Memory Stability** — heap/RSS-growth trend (a leak/steady-climb signal). The
  least-squares slope of resident memory (MB) over wall-clock minutes, sampled
  at request boundaries. Flat/shrinking = healthy; a sustained climb = poor.
- **GC Pressure** — the share of wall time this process has spent paused in
  garbage collection (``gc.callbacks`` start/stop timing). Low = healthy.
- **Reliability** — the request error rate (HTTP 5xx / total) observed by the
  request instrumentation, with throughput reported in the caption. Low = good.

PARITY: keys, labels, rating bands, and the wire shape are the cross-runtime
standard — Node/Go/Java mirror them byte-for-byte. The metric->score thresholds
are the shared standard (documented in the Node reference). Python has no V8
heap, so "memory" reads current RSS via ``/proc/self/statm`` rather than a heap
counter; the ``heapMb`` extra + caption carry that RSS figure so the wire shape
stays identical. If ``/proc`` is unavailable (non-Linux), the axis is omitted.

INVARIANT: these axes are ADDITIVE and display-only. They never feed the
composite Speed score (TTFF/TTI/FID). They carry only numbers, a fixed rating
bucket, and a machine-built caption (numbers + fixed vocabulary, never a route
name/value/source). Each rides inside the existing snapshot upload and clears
the same PII guard as every other field.

HONESTY: an axis is OMITTED (the server renders it "pending") until it has
enough data to mean something — never a 100 or 0 fabricated from one request or
a just-booted process.
"""

from __future__ import annotations

import gc
import math
import os
import sys
import resource
import threading
import time
from typing import Any, Dict, List, Optional

from .runtime_flags import is_boosthis_disabled, is_collection_gated
from . import extra_meters
from . import os_meters
from . import ready_to_serve
from .health_axes import linear_score, rating_for

# ── Memory Stability bands ──────────────────────────────────────────────────
# Growth score bands (MB/min). <=2 MB/min of sustained growth reads as stable
# (100); >=25 MB/min is a clear steady climb (0). Growth <=0 (flat or shrinking)
# always scores 100.
MEM_GROWTH_GOOD = 2
MEM_GROWTH_POOR = 25
# Minimum boundary samples before a trend is trustworthy.
MIN_MEM_SAMPLES = 12
# Both memory trends use the contract's five-minute judged window after dropping
# the first minute of process life.
MIN_MEM_SPAN_MS = 300_000
MEM_STARTUP_EXCLUSION_MS = 60_000
MEM_LOW_WATER_BUCKET_MS = 15_000
MEM_RISE_FLOOR_MB = 1.0
# Whole-process resident growth has the tighter cross-runtime band.
RESIDENT_GROWTH_GOOD = 0.5
RESIDENT_GROWTH_POOR = 5.0
# Throttle between memory samples (ms) — one per second is plenty for a trend
# and keeps a burst of requests from flooding the ring.
MEM_THROTTLE_MS = 1_000
# Bounded ring so a long-lived process can never grow this unbounded.
MEM_RING_CAP = 420
# The shared axis-reason vocabulary: the kit sends a CODE, the server owns the
# words (axisAvailability.ts). 9 says the fitted trend sits inside its own
# noise — a reading that is waiting for a calmer window, not one that will
# never be graded.
REASON_TREND_INSIDE_NOISE = 9

# ── GC Pressure bands ───────────────────────────────────────────────────────
# GC-share score bands (% of wall time). <=1% reads as healthy (100); >=10% is
# a process spending real time paused in collection (0).
GC_PCT_GOOD = 1
GC_PCT_POOR = 10
# Minimum elapsed wall time before the share is meaningful (ms).
MIN_GC_ELAPSED_MS = 5_000
# Recent GC tax: collection time in a rolling window of at least one minute.
GC_TAX_GOOD = 1
GC_TAX_POOR = 10
GC_TAX_WINDOW_MS = 60_000

# ── gcPauseTail bands ───────────────────────────────────────────────────────
# Per-collection stop-the-world pause bands (ms). The cyclic collector's p99
# pause <=5ms reads as healthy (100); a p99 >=100ms is a real latency stall (0).
# Reuses the gc.callbacks timing already wired for gcPressure — no new hook.
GC_PAUSE_P99_GOOD = 5
GC_PAUSE_P99_POOR = 100
# Minimum timed collections before the p99 tail is trustworthy.
MIN_GC_PAUSE_SAMPLES = 10
# Bounded ring so a long-lived process can never grow this unbounded.
GC_PAUSE_RING_CAP = 512

# ── Reliability bands ───────────────────────────────────────────────────────
# Error-rate score bands (% of requests answered 5xx). <=1% reads as healthy
# (100); >=20% is a service in trouble (0).
ERR_PCT_GOOD = 1
ERR_PCT_POOR = 20
# Minimum requests before an error rate is trustworthy.
MIN_REQ_FOR_RELIABILITY = 10
# HTTP status at/above which a response counts as a server error.
SERVER_ERROR_STATUS = 500

# ── crashFree bands (universal) ─────────────────────────────────────────────
# Crash-rate score bands (crashes/hr). 0 -> 100, >=3/hr -> 0.
CRASHFREE_RATE_GOOD = 0
CRASHFREE_RATE_POOR = 3
# Observe at least this many minutes before claiming crash-free (a crash shows
# immediately regardless).
CRASHFREE_MIN_WINDOW_MIN = 5

# ── gcGenerationBalance bands ───────────────────────────────────────────────
# CPython's generational GC should collect the young gen (gen0) far more often
# than it does a full gen2 sweep. A gen2/gen0 collection RATIO near 0 is healthy
# (short-lived allocations, cheap collections); a high ratio means promotion
# pressure / long-lived churn forcing expensive full sweeps. <=0.01 -> 100,
# >=0.1 -> 0.
GC_GEN_RATIO_GOOD = 0.01
GC_GEN_RATIO_POOR = 0.1
# Minimum young-gen collections before the ratio is meaningful.
MIN_YOUNG_GCS = 50

# ── finalizerBacklog bands ──────────────────────────────────────────────────
FINALIZER_BACKLOG_GOOD = 100
FINALIZER_BACKLOG_POOR = 10_000
FINALIZER_BACKLOG_MIN_ELAPSED_MS = 60_000

# ── asyncioLoopLag bands ────────────────────────────────────────────────────
# p99 event-loop scheduling delay (ms). <=10ms -> 100, >=100ms -> 0. Derived
# from the existing request-boundary probe ring (event_loop_lag) — no new task.
LOOP_LAG_GOOD_MS = 10
LOOP_LAG_POOR_MS = 100

# ── threadHealth bands ──────────────────────────────────────────────────────
# Live-thread count growth (threads/min). A flat/steady pool is healthy; a
# steady climb is a thread leak. <=0.5/min -> 100, >=5/min -> 0. ANY detected
# deadlock forces the score to 0 regardless of the trend.
THREAD_GROWTH_GOOD = 0.5
THREAD_GROWTH_POOR = 5
MIN_THREAD_SAMPLES = 12
MIN_THREAD_SPAN_MS = 60_000
# Throttle between thread samples (ms) — one per second is plenty for a trend.
THREAD_THROTTLE_MS = 1_000
# Bounded ring so a long-lived process can never grow this unbounded.
THREAD_RING_CAP = 240

_BYTES_PER_MB = 1024 * 1024

# containerPressure — process/cgroup memory in use vs the cgroup memory LIMIT
# (the container OOM-kill wall). <=75% is comfortable headroom (100); >=95% is
# imminent-OOM territory (0). A HARDER wall than a managed-heap soft limit: RSS,
# native allocations and buffers all count against it and nothing GCs it back.
CONTAINER_PCT_GOOD = 75
CONTAINER_PCT_POOR = 95
# cgroup-v1 "unlimited" is a near-Int64-max sentinel; a limit at/above this
# means no real cap is set, so the axis is omitted rather than faked.
_CGROUP_V1_UNLIMITED = 0x7000_0000_0000_0000

# cpuThrottling — the share of CFS bandwidth periods in which the container was
# throttled (cgroup CPU quota exhausted). <=1% is comfortable (100); >=25% is a
# process being held back hard by its CPU cap (0). Higher throttledPct is WORSE,
# same lower-is-better direction as containerPressure's usedPct.
CPU_THROTTLE_PCT_GOOD = 1
CPU_THROTTLE_PCT_POOR = 25
# Both cgroup counters are lifetime totals. Keep a recent observation window so
# this axis answers whether the container is being throttled now.
CPU_THROTTLE_MIN_WINDOW_MS = 15_000

# fdSaturation — open file descriptors vs the process's RLIMIT_NOFILE soft limit
# (the "too many open files" wall). <=70% is comfortable headroom (100); >=90% is
# imminent-exhaustion territory (0). Higher usedPct is WORSE, same lower-is-better
# direction as containerPressure's usedPct.
FD_SATURATION_PCT_GOOD = 70
FD_SATURATION_PCT_POOR = 90

# coldStart — how long the process had been alive when telemetry came up (OS
# process start -> the host's enable_telemetry call). A cold-start/boot-time
# proxy, captured ONCE at telemetry init and FROZEN (never re-read at snapshot
# time — a live "now - processStart" read is uptime, not cold start, and would
# grow forever). This stops at telemetry-enable rather than app readiness, so it
# is a PARTIAL interval and must remain unrated: no good/poor thresholds are
# defined here, because a threshold for "a good start" cannot be applied to part
# of one. Display-only; ADDITIVE — never feeds the composite Speed score.
# A host that enables telemetry long after boot is not measuring bootstrap;
# reporting a huge value as "poor cold start" would be a lie, so a captured age
# over this cap is OMITTED (the honest move) rather than scored.
COLD_START_MAX_MS = 600_000  # 10 minutes
# Closed boundary code: runtime start -> the kit was switched on. This is only
# part of startup, so the reading travels without a score or verdict.
COLD_START_MEASURED_TO_KIT_ENABLE = 4
# Process start -> the first server socket began accepting connections. WHOLE:
# the interval the tile's name promises, so this one IS rated. See
# ready_to_serve.py and docs/cold-start-boundary-contract.md.
COLD_START_MEASURED_TO_SERVING = 1
# Rating bands for the whole interval only (code 1). A partial reading (code 4)
# is never scored, whatever it says.
COLD_START_GOOD_MS = 5_000
COLD_START_POOR_MS = 30_000

# startupImport — import-graph weight frozen ONCE at enable: how many modules
# the app had loaded by the time telemetry started. A heavy import graph is why
# cold start was slow. Display-only bands on the module count.
STARTUP_IMPORT_GOOD_MODULES = 800
STARTUP_IMPORT_POOR_MODULES = 4_000

# memoryPerRequest — RSS slope divided by request rate: KB leaked/grown per
# request served (the Python leak early-warning). tracemalloc stays OFF.
MEM_PER_REQ_GOOD_KB = 4.0
MEM_PER_REQ_POOR_KB = 128.0
MEM_PER_REQ_MIN_REQUESTS = 50

_started = False
_started_at = 0  # ms

# Memory: bounded ring of (t_ms, rss_mb) samples.
_mem_samples: List[Dict[str, float]] = []
_last_mem_sample_at = 0  # ms

# GC: accumulated pause time + collection count, and the in-flight start mark.
_gc_callback: Optional[Any] = None
_gc_ms = 0.0
_gc_count = 0
_gc_start: Optional[float] = None  # seconds (perf_counter)
# gcPauseTail: bounded ring of per-collection pause durations (ms).
_gc_pause_ring: List[float] = []
# (collection stop timestamp ms, pause duration ms), bounded to the recent tax
# window plus a small margin.
_gc_tax_events: List[Dict[str, float]] = []

# Reliability counters.
_req_total = 0
_req_errors = 0
_first_req_at = 0  # ms
_last_req_at = 0  # ms

# threadHealth: bounded ring of (t_ms, count) live-thread samples + throttle.
_thread_samples: List[Dict[str, float]] = []
_last_thread_sample_at = 0  # ms

# gcGenerationBalance: optional test override of (young_gcs, full_gcs) so the
# ratio can be exercised deterministically without driving the real collector.
_gc_stats_override: Optional[Dict[str, int]] = None
# threadHealth: optional test override forcing the deadlock verdict.
_deadlock_override: Optional[bool] = None
# containerPressure: optional test override — a {"usedBytes","maxBytes"} dict of
# injected cgroup numbers, or the string "unavailable" to force the no-cgroup
# path. None reads the live cgroup.
_cgroup_override: Optional[Any] = None
# cpuThrottling: optional test override — a {"periods","throttled"} dict of
# injected cgroup CFS-bandwidth numbers, or the string "unavailable" to force
# the no-cgroup / unlimited-quota path. None reads the live cgroup.
_cpu_throttle_override: Optional[Any] = None
# fdSaturation: optional test override — a {"open","max"} dict of injected
# file-descriptor numbers, or the string "unavailable" to force the
# unreadable / unlimited-limit path. None reads the live process.
_fd_override: Optional[Any] = None

# coldStart: the process age (ms) captured ONCE at telemetry-enable and FROZEN.
# None until captureColdStart() runs successfully; a second capture never
# overwrites a stored value (idempotent). The axis is omitted while None.
_cold_start_ms: Optional[int] = None
_startup_modules: Optional[int] = None
# coldStart: optional test override — a callable returning the process age in ms
# (or raising) used in place of the live /proc reader. None reads /proc.
_cold_start_reader_override: Optional[Any] = None

# cpuThrottling host-suspend discount — CFS periods/throttles that accrued while
# the loop was IDLE across a suspend-suspicious gap (a scale-to-zero host
# throttling a suspended container, not the app fighting its quota). The idle
# baseline is snapshotted when the loop goes idle; the delta at the next wake is
# attributed to suspend and SUBTRACTED from the scored ratio. Kept visible on the
# axis via ``suspendDiscounts`` — discount, don't hide. Mirrors the Node sibling.
_cpu_idle_baseline: Optional[Dict[str, int]] = None
_suspend_periods_discounted = 0
_suspend_throttled_discounted = 0
_suspend_throttle_wakes = 0
# Counters at the last completed window boundary. The first read only plants
# this anchor; a cached answer lets multiple snapshot readers share one window.
_cpu_throttle_anchor: Optional[Dict[str, int]] = None
_cpu_throttle_last: Optional[Dict[str, Any]] = None
_cpu_throttle_lock = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _r0(n: float) -> int:
    return round(n)


def _r1(n: float) -> float:
    return round(n * 10) / 10


def _r2(n: float) -> float:
    return round(n * 100) / 100


def _round_share(n: float) -> float:
    """Round to one decimal or three significant figures, whichever is finer."""
    if n == 0:
        return 0.0
    decimals = max(1, min(6, 3 - math.floor(math.log10(abs(n))) - 1))
    return round(n, decimals)


def _signed(n: float) -> str:
    v = _r1(n)
    return f"+{v}" if n >= 0 else f"{v}"


def _read_rss_mb() -> Optional[float]:
    """Current resident set size in MB via ``/proc/self/statm`` (Linux). Returns
    None when ``/proc`` is unavailable (non-Linux) so the caller omits the axis.
    Never raises."""
    try:
        with open("/proc/self/statm", "r") as fh:
            fields = fh.read().split()
        resident_pages = int(fields[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
        return (resident_pages * page_size) / _BYTES_PER_MB
    except Exception:
        return None


def start_vitals() -> None:
    """Start vitals collection. Idempotent, no-op under the kill-switch. Wires a
    ``gc.callbacks`` hook to time GC pauses (best-effort). Never raises. Called
    from ``enable_telemetry``."""
    global _started, _started_at, _gc_callback
    if is_boosthis_disabled() or _started:
        return
    _started = True
    _started_at = _now_ms()
    try:
        def _gc_hook(phase: str, info: Dict[str, Any]) -> None:
            global _gc_ms, _gc_count, _gc_start
            try:
                if phase == "start":
                    _gc_start = time.perf_counter()
                elif phase == "stop":
                    if _gc_start is not None:
                        pause_ms = (time.perf_counter() - _gc_start) * 1000.0
                        _gc_ms += pause_ms
                        _gc_start = None
                        _gc_tax_events.append({"t": float(_now_ms()), "ms": pause_ms})
                        if len(_gc_tax_events) > GC_PAUSE_RING_CAP:
                            del _gc_tax_events[0 : len(_gc_tax_events) - GC_PAUSE_RING_CAP]
                        _gc_pause_ring.append(pause_ms)
                        if len(_gc_pause_ring) > GC_PAUSE_RING_CAP:
                            del _gc_pause_ring[0 : len(_gc_pause_ring) - GC_PAUSE_RING_CAP]
                    _gc_count += 1
            except Exception:
                pass

        gc.callbacks.append(_gc_hook)
        _gc_callback = _gc_hook
    except Exception:
        _gc_callback = None


def requests_observed() -> int:
    """Units of work this kit has actually timed in this process.

    Read-back evidence that the request boundary is genuinely attached here —
    a mounted adapter that never sees a request is not the same thing as a
    watched surface. A count and nothing else."""
    return _req_total


def note_request(status_code: Optional[int] = None) -> None:
    """Record one finished request at the boundary: bumps the reliability
    counters and (throttled) samples memory. Called once per request from the
    request instrumentation (ASGI + Flask finish paths). No-op before start, and
    while collection is gated — the env kill-switch OR a server-side lock
    (revoked / unpaid / paused / never activated), re-checked HERE at record
    time so turning Boosthis off mid-run really stops the sampling rather than
    only hiding it. Never raises — instrumentation must never take down the
    host."""
    global _req_total, _req_errors, _first_req_at, _last_req_at
    if not _started or is_collection_gated():
        return
    try:
        from . import extra_meters

        extra_meters.note_worker_completed()
    except Exception:  # noqa: BLE001
        pass
    try:
        ts = _now_ms()
        _req_total += 1
        if isinstance(status_code, int) and status_code >= SERVER_ERROR_STATUS:
            _req_errors += 1
        if _first_req_at == 0:
            _first_req_at = ts
        _last_req_at = ts
        _sample_memory(ts)
        _sample_threads(ts)
        extra_meters.note_request_boundary()
        os_meters.note_request_boundary()
    except Exception:
        pass  # best-effort — never fatal to the host


def _sample_memory(ts: int) -> None:
    global _last_mem_sample_at
    if ts - _last_mem_sample_at < MEM_THROTTLE_MS:
        return
    rss_mb = _read_rss_mb()
    if rss_mb is None:
        return  # /proc unavailable -> omit memoryStability entirely
    _last_mem_sample_at = ts
    # On Python there is no distinct heap-used counter like V8's; RSS is the
    # honest resident-memory figure, used for both the trend and the caption.
    _mem_samples.append({"t": ts, "heapMb": rss_mb, "rssMb": rss_mb})
    if len(_mem_samples) > MEM_RING_CAP:
        del _mem_samples[0 : len(_mem_samples) - MEM_RING_CAP]


def _sample_threads(ts: int) -> None:
    """Throttled sample of the live-thread count at a request boundary. Feeds the
    threadHealth trend. No new poller — piggybacks on the existing boundary."""
    global _last_thread_sample_at
    if ts - _last_thread_sample_at < THREAD_THROTTLE_MS:
        return
    _last_thread_sample_at = ts
    _thread_samples.append({"t": ts, "count": threading.active_count()})
    if len(_thread_samples) > THREAD_RING_CAP:
        del _thread_samples[0 : len(_thread_samples) - THREAD_RING_CAP]


def _trend_fit(samples: List[Dict[str, float]], key: str) -> Dict[str, float]:
    """Least-squares slope and its standard error, in units/minute."""
    n = len(samples)
    t0 = samples[0]["t"]
    sx = sy = sxx = sxy = 0.0
    for s in samples:
        x = (s["t"] - t0) / 60_000.0
        y = s[key]
        sx += x
        sy += y
        sxx += x * x
        sxy += x * y
    denom = n * sxx - sx * sx
    if denom == 0:
        return {"slope": 0.0, "se": 0.0}
    slope = (n * sxy - sx * sy) / denom
    xbar = sx / n
    ybar = sy / n
    intercept = ybar - slope * xbar
    residual = 0.0
    centered_x = 0.0
    for s in samples:
        x = (s["t"] - t0) / 60_000.0
        residual += (s[key] - (intercept + slope * x)) ** 2
        centered_x += (x - xbar) ** 2
    se = math.sqrt((residual / (n - 2)) / centered_x) if n > 2 and centered_x > 0 else 0.0
    return {"slope": slope, "se": se}


def _slope_per_min(samples: List[Dict[str, float]], key: str) -> float:
    return _trend_fit(samples, key)["slope"]


def _heap_slope_per_min(samples: List[Dict[str, float]]) -> float:
    """Least-squares slope of heapMb over wall-clock minutes."""
    return _slope_per_min(samples, "heapMb")


def _memory_low_water_samples(key: str) -> List[Dict[str, float]]:
    """Drop startup, then keep the lowest value in each fixed time bucket."""
    eligible = [
        s for s in _mem_samples
        if s["t"] >= _started_at + MEM_STARTUP_EXCLUSION_MS
    ]
    buckets: Dict[int, Dict[str, float]] = {}
    for sample in eligible:
        bucket = int((sample["t"] - (_started_at + MEM_STARTUP_EXCLUSION_MS)) // MEM_LOW_WATER_BUCKET_MS)
        current = buckets.get(bucket)
        if current is None or sample[key] < current[key]:
            buckets[bucket] = sample
    return [buckets[b] for b in sorted(buckets)]


def _read_memory_trend(key: str, good: float, poor: float, noun: str) -> Optional[Dict[str, Any]]:
    samples = _memory_low_water_samples(key)
    if len(samples) < MIN_MEM_SAMPLES:
        return None
    span_ms = samples[-1]["t"] - samples[0]["t"]
    if span_ms < MIN_MEM_SPAN_MS:
        return None
    fit = _trend_fit(samples, key)
    growth = fit["slope"]
    se = fit["se"]
    window_min = span_ms / 60_000.0
    # HOW MUCH memory is in use, beside how fast it is moving. A slope with no
    # level behind it cannot be read: "+0.1 MB/min" says nothing about whether
    # this app is holding 40 MB or 4 GB, and every sibling kit sends the level
    # under these same two keys. Read from the newest raw sample rather than
    # the low-water fit, because this field answers "right now" while the
    # slope answers "over the window".
    level = _mem_samples[-1] if _mem_samples else samples[-1]
    out: Dict[str, Any] = {
        "heapMb": _r0(level["heapMb"]),
        "rssMb": _r0(level["rssMb"]),
        "growthMbPerMin": _r1(growth),
        "slopeSeMbPerMin": _r2(se),
        "sampleCount": len(samples),
        "windowMin": _r1(window_min),
        "caption": f"{noun} {_r0(level[key])} MB · {_signed(growth)} MB/min over {_r1(window_min)}m",
    }
    if abs(growth) <= se:
        # A slope inside its own fit noise is a WAIT, not a verdict: a calmer
        # window produces a score. `not-scored` is this product's word for a
        # reading deliberately never graded AT ALL, so using it here files a
        # temporary silence as a permanent decision — and a flat, healthy app
        # is the commonest way into this branch, so it would be the commonest
        # answer too. `pending` plus the shared reason code is what the server
        # turns into "could not tell" on every surface.
        out["rating"] = "pending"
        out["reasonCode"] = REASON_TREND_INSIDE_NOISE
        out["caption"] = (
            f"{noun} {_r0(level[key])} MB · moves too much over "
            f"{_r1(window_min)}m to call a trend"
        )
        return out
    scored_growth = growth
    if growth <= 0 or growth * window_min < MEM_RISE_FLOOR_MB:
        scored_growth = 0.0
    score = linear_score(scored_growth, good, poor)
    out["score"] = score
    out["rating"] = rating_for(score)
    return out


def read_memory_stability() -> Optional[Dict[str, Any]]:
    """Memory-stability axis, or None while warming up (fewer than MIN samples
    or too short a window). Pure read — never mutates state. Never raises."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        return _read_memory_trend(
            "heapMb", MEM_GROWTH_GOOD, MEM_GROWTH_POOR, "heap"
        )
    except Exception:
        return None


def read_resident_growth() -> Optional[Dict[str, Any]]:
    """Whole-process RSS low-water trend, under the canonical wire key."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        return _read_memory_trend(
            "rssMb", RESIDENT_GROWTH_GOOD, RESIDENT_GROWTH_POOR, "resident"
        )
    except Exception:
        return None


def read_gc_pressure() -> Optional[Dict[str, Any]]:
    """GC-pressure axis, or None until at least one collection has been seen
    over a few seconds of wall time. Pure read. Never raises."""
    if is_boosthis_disabled() or not _started or _gc_callback is None:
        return None
    try:
        if _gc_count < 1:
            return None
        elapsed = _now_ms() - _started_at
        if elapsed < MIN_GC_ELAPSED_MS:
            return None
        gc_pct = (100 * _gc_ms) / elapsed
        score = linear_score(gc_pct, GC_PCT_GOOD, GC_PCT_POOR)
        return {
            "score": score,
            "rating": rating_for(score),
            "gcMs": _r0(_gc_ms),
            "gcCount": _gc_count,
            "gcPct": _r2(gc_pct),
            "windowMin": _r1(elapsed / 60_000.0),
            "caption": f"{_r2(gc_pct)}% time in GC · {_gc_count} collections",
        }
    except Exception:
        return None


def _percentile_ms(values: List[float], p: int) -> float:
    """Nearest-rank p-th percentile of an unsorted list of ms values. Empty
    -> 0.0. Sorts a copy so the caller's ring is never mutated."""
    n = len(values)
    if n == 0:
        return 0.0
    ordered = sorted(values)
    idx = ((p * n + 99) // 100) - 1
    if idx < 0:
        idx = 0
    elif idx >= n:
        idx = n - 1
    return ordered[idx]


def read_gc_pause_tail() -> Optional[Dict[str, Any]]:
    """GC pause-tail axis (Python) — the p99 / max stop-the-world pause of the
    cyclic collector, in ms. Reuses the gc.callbacks pause timing already wired
    for gcPressure (no new hook, no new thread). None until enough collections
    have been timed. Display-only; never feeds the Speed score. Pure read. Never
    raises."""
    if is_boosthis_disabled() or not _started or _gc_callback is None:
        return None
    try:
        n = len(_gc_pause_ring)
        if n < MIN_GC_PAUSE_SAMPLES:
            return None
        p99 = _percentile_ms(_gc_pause_ring, 99)
        mx = max(_gc_pause_ring)
        score = linear_score(p99, GC_PAUSE_P99_GOOD, GC_PAUSE_P99_POOR)
        return {
            "score": score,
            "rating": rating_for(score),
            "p99Ms": _r2(p99),
            "maxMs": _r2(mx),
            "pauseCount": n,
            "caption": f"{_r2(p99)}ms p99 pause · {_gc_count} GCs",
        }
    except Exception:
        return None


def read_reliability() -> Optional[Dict[str, Any]]:
    """Reliability axis (5xx error rate + throughput caption), or None until
    enough requests have been observed. Pure read. Never raises."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        if _req_total < MIN_REQ_FOR_RELIABILITY:
            return None
        error_pct = (100 * _req_errors) / _req_total
        score = linear_score(error_pct, ERR_PCT_GOOD, ERR_PCT_POOR)
        span_ms = max(0, _last_req_at - _first_req_at)
        span_min = span_ms / 60_000.0
        rpm = (_req_total / span_min) if span_min > 0 else _req_total
        return {
            "score": score,
            "rating": rating_for(score),
            "total": _req_total,
            "errors": _req_errors,
            "errorRate": _r2(error_pct / 100),
            "errorPct": _r1(error_pct),
            "rpm": _r1(rpm),
            "caption": (
                f"{_r1(error_pct)}% errors · {_r1(rpm)}/min · {_req_total} requests"
            ),
        }
    except Exception:
        return None


def read_crash_free() -> Optional[Dict[str, Any]]:
    """Crash-free axis (universal). Score from the observed crash rate; warming
    up until a few minutes have elapsed (a crash surfaces immediately, even
    during warm-up). Reads the counter the existing crash hook maintains — no new
    collection. Pure read. Never raises."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        # Lazy import to avoid an import cycle (crash_reporter imports telemetry,
        # which imports this module transitively).
        from . import crash_reporter

        window_min = (_now_ms() - _started_at) // 60_000
        crashes = crash_reporter.crash_count()
        if crashes == 0 and window_min < CRASHFREE_MIN_WINDOW_MIN:
            return None
        hours = max(window_min / 60.0, 1 / 60.0)
        per_hour = crashes / hours
        score = linear_score(per_hour, CRASHFREE_RATE_GOOD, CRASHFREE_RATE_POOR)
        caption = (
            f"crash-free · {window_min}m observed"
            if crashes == 0
            else f"{crashes} crashes · {window_min}m"
        )
        return {
            "score": score,
            "rating": rating_for(score),
            "crashes": crashes,
            "windowMin": int(window_min),
            "caption": caption,
        }
    except Exception:
        return None


def read_gc_generation_balance() -> Optional[Dict[str, Any]]:
    """GC generation-balance axis (Python) — the ratio of full (gen2) collections
    to young (gen0) collections from ``gc.get_stats()``. A low ratio is healthy
    (short-lived allocations, cheap sweeps); a high ratio means promotion
    pressure forcing expensive full collections. None until enough young-gen
    collections have run. Pure read. Never raises."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        if _gc_stats_override is not None:
            young_gcs = int(_gc_stats_override["young"])
            full_gcs = int(_gc_stats_override["full"])
        else:
            stats = gc.get_stats()
            if not stats or len(stats) < 3:
                return None
            young_gcs = int(stats[0].get("collections", 0))
            full_gcs = int(stats[2].get("collections", 0))
        if young_gcs < MIN_YOUNG_GCS:
            return None
        ratio = (full_gcs / young_gcs) if young_gcs > 0 else 0.0
        score = linear_score(ratio, GC_GEN_RATIO_GOOD, GC_GEN_RATIO_POOR)
        return {
            "score": score,
            "rating": rating_for(score),
            "fullCount": full_gcs,
            "youngCount": young_gcs,
            "fullRatio": _round_share(ratio),
            "caption": (
                f"{full_gcs} full / {young_gcs} young GCs · ratio {_round_share(ratio)}"
            ),
        }
    except Exception:
        return None


def read_gc_tax() -> Optional[Dict[str, Any]]:
    """Collector wall-time share over the recent rolling one-minute window."""
    if is_boosthis_disabled() or not _started or _gc_callback is None:
        return None
    try:
        now = _now_ms()
        if now - _started_at < GC_TAX_WINDOW_MS:
            return None
        cutoff = now - GC_TAX_WINDOW_MS
        events = [e for e in _gc_tax_events if e["t"] >= cutoff]
        gc_ms = sum(e["ms"] for e in events)
        gc_pct = 100.0 * gc_ms / GC_TAX_WINDOW_MS
        score = linear_score(gc_pct, GC_TAX_GOOD, GC_TAX_POOR)
        return {
            "score": score,
            "rating": rating_for(score),
            "gcPct": _r2(gc_pct),
            "gcCount": len(events),
            "windowMin": _r1(GC_TAX_WINDOW_MS / 60_000.0),
            "caption": f"{_r2(gc_pct)}% recent GC · {len(events)} collections",
        }
    except Exception:
        return None


def read_finalizer_backlog() -> Optional[Dict[str, Any]]:
    """CPython garbage and cumulative uncollectable objects still outstanding."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        elapsed = _now_ms() - _started_at
        if elapsed < FINALIZER_BACKLOG_MIN_ELAPSED_MS:
            return None
        uncollectable = sum(int(s.get("uncollectable", 0) or 0) for s in gc.get_stats())
        pending = len(gc.garbage) + uncollectable
        score = linear_score(
            pending, FINALIZER_BACKLOG_GOOD, FINALIZER_BACKLOG_POOR
        )
        return {
            "score": score,
            "rating": rating_for(score),
            "pendingCount": pending,
            "elapsedMs": elapsed,
            "caption": f"{pending} finalizers pending",
        }
    except Exception:
        return None


def read_asyncio_loop_lag() -> Optional[Dict[str, Any]]:
    """Asyncio loop-lag axis (Python) — p50/p99 event-loop scheduling delay,
    derived from the SAME request-boundary probe ring as ``eventLoopLag`` (no new
    background task). None until a running loop has produced enough samples
    (sync WSGI apps never accrue any, so the axis is omitted). Scores the p99
    tail delay. Pure read. Never raises."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        from .event_loop_lag import loop_lag_percentiles

        pcts = loop_lag_percentiles()
        if pcts is None:
            return None
        p50 = pcts["p50"]
        p99 = pcts["p99"]
        score = linear_score(p99, LOOP_LAG_GOOD_MS, LOOP_LAG_POOR_MS)
        # Host-suspend honesty: the shared ring already excluded suspend-gap
        # samples at record time; carry the visible discount count here too so
        # the asyncioLoopLag caption/tile can note it (discount, don't hide).
        discounts = int(pcts.get("suspendDiscounts", 0) or 0)
        caption = f"loop lag p50 {p50}ms · p99 {p99}ms"
        if discounts > 0:
            caption += f" · host-suspend gaps discounted ({discounts})"
        axis: Dict[str, Any] = {
            "score": score,
            "rating": rating_for(score),
            "lagP50Ms": p50,
            "lagP99Ms": p99,
            "caption": caption,
        }
        if discounts > 0:
            axis["suspendDiscounts"] = discounts
        return axis
    except Exception:
        return None


def _detect_deadlock() -> bool:
    """Best-effort deadlock detection: True when every live, non-daemon thread is
    blocked waiting on a lock (no thread able to make progress). Conservative —
    only flags when there is more than one thread and NONE are runnable. Uses
    ``sys._current_frames`` to inspect where each thread is parked; if that
    introspection is unavailable it returns False (never a false alarm). Never
    raises."""
    try:
        import sys

        frames = sys._current_frames()
        alive_ids = {t.ident for t in threading.enumerate() if t.is_alive()}
        inspected = [
            fr for tid, fr in frames.items() if tid in alive_ids
        ]
        if len(inspected) < 2:
            return False
        # A thread is "blocked on a lock" when its top frame is parked inside a
        # lock acquire (threading primitives / the C acquire). Code-defined
        # function names only — never any user value.
        blocked = 0
        for fr in inspected:
            name = fr.f_code.co_name
            filename = fr.f_code.co_filename
            if name in ("acquire", "wait", "__enter__") and (
                "threading" in filename or "queue" in filename
            ):
                blocked += 1
        return blocked >= len(inspected)
    except Exception:
        return False


def read_thread_health() -> Optional[Dict[str, Any]]:
    """Thread-health axis (Python) — least-squares growth of the live-thread
    count (a thread-leak signal) plus deadlock detection. ANY detected deadlock
    forces the score to 0. None until enough samples cover a long-enough window.
    Pure read. Never raises."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        if len(_thread_samples) < MIN_THREAD_SAMPLES:
            return None
        first = _thread_samples[0]
        last = _thread_samples[-1]
        span_ms = last["t"] - first["t"]
        if span_ms < MIN_THREAD_SPAN_MS:
            return None
        growth = _slope_per_min(_thread_samples, "count")
        deadlocked = (
            _deadlock_override
            if _deadlock_override is not None
            else _detect_deadlock()
        )
        if deadlocked:
            score = 0
        else:
            score = linear_score(
                max(0.0, growth), THREAD_GROWTH_GOOD, THREAD_GROWTH_POOR
            )
        threads = int(last["count"])
        caption = (
            f"{threads} threads · deadlock detected"
            if deadlocked
            else f"{threads} threads · {_signed(growth)}/min"
        )
        return {
            "score": score,
            "rating": rating_for(score),
            "threads": threads,
            "growthPerMin": _r1(growth),
            # Numeric-only wire (0/1) to match Java's numeric deadlocked count so
            # the server sanitizer keeps it and snapshotView can read it.
            "deadlocked": 1 if deadlocked else 0,
            "sampleCount": len(_thread_samples),
            "caption": caption,
        }
    except Exception:
        return None


def _read_cgroup_mem() -> Optional[Dict[str, int]]:
    """Current cgroup memory usage + limit in bytes: cgroup v2
    (``memory.current`` / ``memory.max``) first, then the v1 fallback. Returns
    None when no cgroup is present, the limit is unset (``max`` / an unlimited
    sentinel), or any read fails, so the caller omits the axis. Never raises."""
    if _cgroup_override == "unavailable":
        return None
    if _cgroup_override is not None:
        return _cgroup_override
    # cgroup v2 — the unified hierarchy.
    try:
        with open("/sys/fs/cgroup/memory.max", "r") as fh:
            max_raw = fh.read().strip()
        if max_raw != "max":
            max_bytes = int(max_raw)
            with open("/sys/fs/cgroup/memory.current", "r") as fh:
                used_bytes = int(fh.read().strip())
            if 0 < max_bytes < _CGROUP_V1_UNLIMITED and used_bytes >= 0:
                return {"usedBytes": used_bytes, "maxBytes": max_bytes}
    except Exception:
        pass
    # cgroup v1 — the legacy per-controller hierarchy.
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r") as fh:
            max_bytes = int(fh.read().strip())
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r") as fh:
            used_bytes = int(fh.read().strip())
        if 0 < max_bytes < _CGROUP_V1_UNLIMITED and used_bytes >= 0:
            return {"usedBytes": used_bytes, "maxBytes": max_bytes}
    except Exception:
        pass
    return None


def read_container_pressure() -> Optional[Dict[str, Any]]:
    """Container-pressure axis — process/cgroup memory in use vs the cgroup
    memory LIMIT (the container OOM-kill wall). Unlike the trend axes there is no
    warm-up: proximity to a hard limit is honest immediately, and the no-cgroup
    case is handled by omission. Pure read — never mutates state. Never raises."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        mem = _read_cgroup_mem()
        if not mem:
            return None
        used = mem["usedBytes"]
        limit = mem["maxBytes"]
        if limit <= 0 or used < 0:
            return None
        used_mb = _r0(used / _BYTES_PER_MB)
        max_mb = _r0(limit / _BYTES_PER_MB)
        used_pct = _r1((100 * used) / limit)
        score = linear_score(used_pct, CONTAINER_PCT_GOOD, CONTAINER_PCT_POOR)
        return {
            "score": score,
            "rating": rating_for(score),
            "usedMb": used_mb,
            "maxMb": max_mb,
            "usedPct": used_pct,
            "caption": f"{used_mb} MB of {max_mb} MB container limit ({used_pct}%)",
        }
    except Exception:
        return None


def _read_cgroup_cpu() -> Optional[Dict[str, int]]:
    """Current cgroup CPU CFS-bandwidth stats — nr_periods + nr_throttled: cgroup
    v2 (``cpu.max`` quota + ``cpu.stat``) first, then the v1 fallback
    (``cpu/cpu.cfs_quota_us`` + ``cpu/cpu.stat``). Returns None when no cgroup is
    present, the quota is unlimited (v2 ``max`` / v1 quota <= 0), or any read
    fails, so the caller omits the axis. Never raises."""
    if _cpu_throttle_override == "unavailable":
        return None
    if _cpu_throttle_override is not None:
        return _cpu_throttle_override
    # cgroup v2 — the unified hierarchy.
    try:
        with open("/sys/fs/cgroup/cpu.max", "r") as fh:
            quota_raw = fh.read().strip()
        if quota_raw.split()[0] != "max":
            periods = throttled = None
            with open("/sys/fs/cgroup/cpu.stat", "r") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    if parts[0] == "nr_periods":
                        periods = int(parts[1])
                    elif parts[0] == "nr_throttled":
                        throttled = int(parts[1])
            if periods is not None and throttled is not None:
                return {"periods": periods, "throttled": throttled}
    except Exception:
        pass
    # cgroup v1 — the legacy per-controller hierarchy.
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "r") as fh:
            quota = int(fh.read().strip())
        if quota > 0:
            periods = throttled = None
            with open("/sys/fs/cgroup/cpu/cpu.stat", "r") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    if parts[0] == "nr_periods":
                        periods = int(parts[1])
                    elif parts[0] == "nr_throttled":
                        throttled = int(parts[1])
            if periods is not None and throttled is not None:
                return {"periods": periods, "throttled": throttled}
    except Exception:
        pass
    return None


def read_cpu_throttling() -> Optional[Dict[str, Any]]:
    """CPU-throttling axis — the share of CFS-bandwidth periods in which the
    container hit its CPU quota and was throttled over the window since the last
    boundary. The first read only plants an anchor; lifetime cgroup totals cannot
    honestly describe current throttling. Never raises."""
    global _cpu_throttle_anchor, _cpu_throttle_last
    if is_boosthis_disabled() or not _started:
        return None
    try:
        with _cpu_throttle_lock:
            cpu = _read_cgroup_cpu()
            if not cpu:
                return None
            periods = cpu["periods"]
            throttled = cpu["throttled"]
            if periods < 0 or throttled < 0:
                return None
            now = _now_ms()
            anchor = _cpu_throttle_anchor
            # A first read, or counters moving backwards after the process was
            # moved behind another container, starts a fresh window and abstains.
            if (
                anchor is None
                or periods < anchor["periods"]
                or throttled < anchor["throttled"]
            ):
                _cpu_throttle_anchor = {
                    "periods": periods,
                    "throttled": throttled,
                    "suspendPeriods": _suspend_periods_discounted,
                    "suspendThrottled": _suspend_throttled_discounted,
                    "atMs": now,
                }
                _cpu_throttle_last = None
                return None

            elapsed = now - anchor["atMs"]
            if elapsed < CPU_THROTTLE_MIN_WINDOW_MS:
                return _cpu_throttle_last

            # Difference the suspend bookkeeping too: only idle activity written
            # off during this window belongs in this window's discount.
            suspend_periods = max(
                0, _suspend_periods_discounted - anchor["suspendPeriods"]
            )
            suspend_throttled = max(
                0, _suspend_throttled_discounted - anchor["suspendThrottled"]
            )
            window_periods = max(
                0, periods - anchor["periods"] - suspend_periods
            )
            window_throttled = min(
                max(0, throttled - anchor["throttled"] - suspend_throttled),
                window_periods,
            )
            # No completed active CPU period says nothing. Keep the anchor so
            # the observation window can continue growing, and say NOTHING:
            # the cached answer belongs to a window that has already closed.
            if window_periods <= 0:
                return None

            throttled_pct = _r1((100 * window_throttled) / window_periods)
            score = linear_score(
                throttled_pct, CPU_THROTTLE_PCT_GOOD, CPU_THROTTLE_PCT_POOR
            )
            window_min = _r1(elapsed / 60_000.0)
            window_caption = (
                "under a minute observed"
                if window_min < 1
                else f"{window_min}m observed"
            )
            caption = (
                f"{throttled_pct}% of CPU periods throttled during traffic "
                f"({window_throttled}/{window_periods}) · {window_caption} "
                f"· {suspend_throttled} idle-suspend throttles discounted"
                if suspend_throttled > 0
                else (
                    f"{throttled_pct}% of CPU periods throttled "
                    f"({window_throttled}/{window_periods}) · {window_caption}"
                )
            )
            axis: Dict[str, Any] = {
                "score": score,
                "rating": rating_for(score),
                "throttledPct": throttled_pct,
                "throttledPeriods": window_throttled,
                "periods": window_periods,
                "windowMin": window_min,
                "caption": caption,
            }
            if suspend_throttled > 0:
                axis["suspendDiscounts"] = suspend_throttled
            _cpu_throttle_anchor = {
                "periods": periods,
                "throttled": throttled,
                "suspendPeriods": _suspend_periods_discounted,
                "suspendThrottled": _suspend_throttled_discounted,
                "atMs": now,
            }
            _cpu_throttle_last = axis
            return axis
    except Exception:
        return None


def _cpu_throttle_idle_start(_gap_ms: float = 0.0) -> None:
    """Suspend-sensor boundary: the loop just went idle — snapshot a clean cgroup
    CPU counter baseline so a later wake can attribute the idle-time delta to
    suspend. Cheap (at most a couple of sysfs reads, only on idle transitions).
    No-op under the kill-switch / before start. Never raises."""
    global _cpu_idle_baseline
    if is_boosthis_disabled() or not _started:
        return
    try:
        baseline = _read_cgroup_cpu()
        with _cpu_throttle_lock:
            _cpu_idle_baseline = baseline
    except Exception:
        _cpu_idle_baseline = None


def _cpu_throttle_suspend_wake(_gap_ms: float = 0.0) -> None:
    """Suspend-sensor boundary: a request arrived after a suspicious idle gap.
    The delta in CFS periods/throttles since the idle baseline accrued while the
    container was SUSPENDED — attribute it to suspend and add it to the discount.
    No-op under the kill-switch / before start. Never raises."""
    global _cpu_idle_baseline, _suspend_periods_discounted
    global _suspend_throttled_discounted, _suspend_throttle_wakes
    if is_boosthis_disabled() or not _started:
        return
    with _cpu_throttle_lock:
        base = _cpu_idle_baseline
        _cpu_idle_baseline = None
    if not base:
        return
    try:
        cur = _read_cgroup_cpu()
        if not cur:
            return
        d_periods = max(0, cur["periods"] - base["periods"])
        d_throttled = max(0, cur["throttled"] - base["throttled"])
        if d_periods > 0 or d_throttled > 0:
            with _cpu_throttle_lock:
                _suspend_periods_discounted += d_periods
                _suspend_throttled_discounted += min(d_throttled, d_periods)
                _suspend_throttle_wakes += 1
    except Exception:
        pass  # never let discount bookkeeping break the request path


# Module wiring (not data): boundaries come from the shared suspend sensor.
try:
    from .suspend_sensor import on_suspend_idle_start, on_suspend_wake

    on_suspend_idle_start(_cpu_throttle_idle_start)
    on_suspend_wake(_cpu_throttle_suspend_wake)
except Exception:  # noqa: BLE001
    pass


def _read_fd_usage() -> Optional[Dict[str, int]]:
    """Current open file-descriptor count + the process's RLIMIT_NOFILE soft
    limit: ``len(os.listdir("/proc/self/fd"))`` for the open count and
    ``resource.getrlimit(RLIMIT_NOFILE)[0]`` for the soft limit. Returns None
    when the count or limit cannot be read, or the limit is unlimited/unknown
    (RLIM_INFINITY or <= 0), so the caller omits the axis. Never raises."""
    if _fd_override == "unavailable":
        return None
    if _fd_override is not None:
        return _fd_override
    try:
        open_fds = len(os.listdir("/proc/self/fd"))
        max_fds = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        if max_fds == resource.RLIM_INFINITY or max_fds <= 0:
            return None  # unlimited / unknown -> omit
        return {"open": open_fds, "max": max_fds}
    except Exception:
        return None


def read_fd_saturation() -> Optional[Dict[str, Any]]:
    """FD-saturation axis — open file descriptors vs the process's RLIMIT_NOFILE
    soft limit (the "too many open files" wall). Unlike the trend axes there is
    no warm-up: proximity to a hard limit is honest immediately, and the
    unreadable / unlimited-limit cases are handled by omission. Pure read —
    never mutates state. Never raises."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        fds = _read_fd_usage()
        if not fds:
            return None
        open_fds = fds["open"]
        max_fds = fds["max"]
        if max_fds <= 0 or open_fds < 0:
            return None
        used_pct = _r1((100 * open_fds) / max_fds)
        score = linear_score(
            used_pct, FD_SATURATION_PCT_GOOD, FD_SATURATION_PCT_POOR
        )
        return {
            "score": score,
            "rating": rating_for(score),
            "openFds": int(open_fds),
            "maxFds": int(max_fds),
            "usedPct": used_pct,
            "scopeCode": 1,
            "caption": (
                f"{int(open_fds)} of {int(max_fds)} file descriptors "
                f"({used_pct}%)"
            ),
        }
    except Exception:
        return None


def _read_process_age_ms() -> Optional[int]:
    """Process age in ms: how long this process has been alive, derived from
    ``/proc/self/stat`` field 22 (starttime, in clock ticks since boot) vs
    ``/proc/uptime`` (seconds since boot). age_ms = (uptime - starttime_secs) *
    1000. Linux-only — returns None on any unreadable piece (non-Linux, /proc
    absent, parse failure), so the caller omits the axis. Never raises.

    CAREFUL: the ``comm`` field (field 2) can contain spaces and parentheses, so
    starttime is parsed relative to the LAST ')' in the stat line, not by naive
    whitespace splitting. This /proc parsing mirrors the kit's cgroup readers."""
    if _cold_start_reader_override is not None:
        try:
            return _cold_start_reader_override()
        except Exception:
            return None
    try:
        with open("/proc/self/stat", "r") as fh:
            stat = fh.read()
        # comm (field 2) is wrapped in parens and may contain spaces/parens;
        # everything after the LAST ')' is the space-separated remainder whose
        # first token is field 3 (state). starttime is field 22 -> index 19 of
        # that remainder.
        rparen = stat.rfind(")")
        if rparen < 0:
            return None
        rest = stat[rparen + 2 :].split()
        # rest[0] == field 3; field 22 == rest[19].
        start_ticks = int(rest[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        if clk_tck <= 0:
            return None
        start_secs = start_ticks / clk_tck
        with open("/proc/uptime", "r") as fh:
            uptime_secs = float(fh.read().split()[0])
        return int((uptime_secs - start_secs) * 1000)
    except Exception:
        return None


def capture_cold_start() -> None:
    """Start watching for readiness, and freeze the enable-time fallback.

    Two things happen here, in this order, because the second must never be able
    to stop the first:

    1. ``ready_to_serve.arm`` begins watching for the first socket in this
       process to start accepting connections — the boundary the coldStart tile's
       name promises. Arming happens on EVERY call, even one that finds the
       fallback already frozen, so a re-enable can never leave the watch off.
    2. The process age at THIS moment (OS start -> enable) is frozen as the
       partial fallback, used only where readiness is never seen. IDEMPOTENT — a
       second call never overwrites the first stored value.

    Guest-safe: never throws; on any read failure it stores nothing and the axis
    is simply omitted. Wired best-effort into ``enable_telemetry`` (a failure
    must never break enable)."""
    global _cold_start_ms
    try:
        ready_to_serve.arm(_read_process_age_ms)
    except Exception:  # noqa: BLE001 - an unwatchable runtime is a partial read
        pass
    try:
        if _cold_start_ms is not None:
            return  # already frozen — never overwrite
        age = _read_process_age_ms()
        if age is not None:
            _cold_start_ms = int(age)
    except Exception:
        pass  # best-effort — never fatal to the host


def read_cold_start() -> Optional[Dict[str, Any]]:
    """Cold-start axis — how long this app took to be ready to serve.

    Two intervals, and which one is reported decides whether it may be rated:

      - Process start -> the first socket began accepting connections
        (``ready_to_serve``). The WHOLE interval the tile's name promises: all
        application startup work is inside it, wherever the enable call sits.
        Rated (measuredTo=1).
      - Process start -> the kit was switched on. A real measurement of a real
        slice of startup, but a PARTIAL one: anything the app did afterwards is
        outside it. Reported as a number with no score (measuredTo=4), never
        rated. Used only where readiness was never seen — a portless worker, a
        pre-forked server whose socket was inherited, a runtime we could not
        watch.

    Both are FROZEN when taken and never re-read here. Sanity guards, applied to
    whichever is used:
      - value <= 0  -> unavailable (omit).
      - value > COLD_START_MAX_MS (10m) -> omit (not a bootstrap signal; a huge
        value scored as "poor" would be a lie).
    Display-only; ADDITIVE — never feeds the Speed score. Pure read. Never
    raises. See docs/cold-start-boundary-contract.md."""
    if is_boosthis_disabled() or not _started:
        return None
    try:

        def usable(value: Optional[int]) -> Optional[int]:
            if value is None:
                return None
            ms = int(value)
            if ms <= 0:
                return None  # unavailable -> omit
            if ms > COLD_START_MAX_MS:
                return None  # too late after boot to be bootstrap -> omit
            return ms

        ready = usable(ready_to_serve.ready_ms())
        if ready is None:
            enable = usable(_cold_start_ms)
            if enable is None:
                return None
            return {
                "score": None,
                "rating": "pending",
                "startupMs": enable,
                "measuredTo": COLD_START_MEASURED_TO_KIT_ENABLE,
            }
        score = linear_score(ready, COLD_START_GOOD_MS, COLD_START_POOR_MS)
        return {
            "score": score,
            "rating": rating_for(score),
            "startupMs": ready,
            "measuredTo": COLD_START_MEASURED_TO_SERVING,
        }
    except Exception:
        return None


def capture_startup_import() -> None:
    """Freeze the import-graph size ONCE at telemetry-enable (the startupImport
    source). IDEMPOTENT — never overwrites. Guest-safe: never raises. Wired
    best-effort into ``enable_telemetry``."""
    global _startup_modules
    try:
        if _startup_modules is not None:
            return  # already frozen — never overwrite
        _startup_modules = len(sys.modules)
    except Exception:
        pass


def read_startup_import() -> Optional[Dict[str, Any]]:
    """Startup import-cost axis — the FROZEN module count captured at enable
    plus the frozen process age when known. One-shot; never re-read. ADDITIVE —
    display-only; never feeds the Speed score. Pure read. Never raises."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        mods = _startup_modules
        if mods is None or mods <= 0:
            return None
        score = linear_score(
            mods, STARTUP_IMPORT_GOOD_MODULES, STARTUP_IMPORT_POOR_MODULES
        )
        out: Dict[str, Any] = {
            "score": score,
            "rating": rating_for(score),
            "moduleCount": int(mods),
            "caption": f"{mods} modules loaded at startup",
        }
        startup_ms = _cold_start_ms
        if startup_ms is not None and 0 < startup_ms <= COLD_START_MAX_MS:
            out["startupMs"] = int(startup_ms)
        return out
    except Exception:
        return None


def read_memory_per_request() -> Optional[Dict[str, Any]]:
    """Memory growth per request — RSS slope (MB/min) divided by requests/min.
    Warm-up: the memory ring must be warm AND at least MEM_PER_REQ_MIN_REQUESTS
    requests must have been served. Flat/shrinking memory reports an honest 0
    KB/request (score 100). ADDITIVE — display-only. Pure read. Never raises."""
    if is_boosthis_disabled() or not _started:
        return None
    try:
        if len(_mem_samples) < MIN_MEM_SAMPLES:
            return None
        span_ms = _mem_samples[-1]["t"] - _mem_samples[0]["t"]
        if span_ms < MIN_MEM_SPAN_MS:
            return None
        if _req_total < MEM_PER_REQ_MIN_REQUESTS or _first_req_at <= 0:
            return None
        req_span_ms = max(1.0, float(_last_req_at - _first_req_at))
        rpm = _req_total / (req_span_ms / 60_000.0)
        if rpm <= 0:
            return None
        slope = max(0.0, _slope_per_min(_mem_samples, "rssMb"))
        kb_per_req = (slope / rpm) * 1024.0
        score = linear_score(kb_per_req, MEM_PER_REQ_GOOD_KB, MEM_PER_REQ_POOR_KB)
        return {
            "score": score,
            "rating": rating_for(score),
            "avgKb": round(kb_per_req, 1),
            "requestCount": _req_total,
            "windowMin": _r1(req_span_ms / 60_000.0),
            "growthMbPerMin": _r1(slope),
            "rpm": _r0(rpm),
            "caption": f"~{round(kb_per_req, 1)} KB per request at {_r0(rpm)} req/min",
        }
    except Exception:
        return None


def read_vitals() -> Dict[str, Dict[str, Any]]:
    """Assemble every currently-available vitals axis. Warming-up axes are
    omitted (the server renders an absent axis as pending) — same contract as
    resilience/eventLoopLag. Pure read. Never raises."""
    axes: Dict[str, Dict[str, Any]] = {}
    mem = read_memory_stability()
    if mem:
        axes["memoryStability"] = mem
    resident = read_resident_growth()
    if resident:
        axes["residentGrowth"] = resident
    gc_ax = read_gc_pressure()
    if gc_ax:
        axes["gcPressure"] = gc_ax
    tax = read_gc_tax()
    if tax:
        axes["gcTax"] = tax
    gp = read_gc_pause_tail()
    if gp:
        axes["gcPauseTail"] = gp
    rel = read_reliability()
    if rel:
        axes["reliability"] = rel
    cf = read_crash_free()
    if cf:
        axes["crashFree"] = cf
    gg = read_gc_generation_balance()
    if gg:
        axes["gcGenerationBalance"] = gg
    fb = read_finalizer_backlog()
    if fb:
        axes["finalizerBacklog"] = fb
    ll = read_asyncio_loop_lag()
    if ll:
        axes["asyncioLoopLag"] = ll
    th = read_thread_health()
    if th:
        axes["threadHealth"] = th
    cp = read_container_pressure()
    if cp:
        axes["containerPressure"] = cp
    cp2 = read_cpu_throttling()
    if cp2 is not None:
        axes["cpuThrottling"] = cp2
    fd = read_fd_saturation()
    if fd is not None:
        axes["fdSaturation"] = fd
    cs = read_cold_start()
    if cs is not None:
        axes["coldStart"] = cs
    si = read_startup_import()
    if si is not None:
        axes["startupImport"] = si
    mpr = read_memory_per_request()
    if mpr is not None:
        axes["memoryPerRequest"] = mpr
    return axes


def clear_vitals() -> None:
    """Remove the GC callback and drop all vitals state (wired into
    ``forget()`` so nothing Boosthis-shaped keeps sampling after erasure).
    Idempotent. Never raises."""
    global _started, _started_at, _last_mem_sample_at
    global _gc_callback, _gc_ms, _gc_count, _gc_start
    global _req_total, _req_errors, _first_req_at, _last_req_at
    global _last_thread_sample_at
    if _gc_callback is not None:
        try:
            gc.callbacks.remove(_gc_callback)
        except Exception:
            pass
        _gc_callback = None
    _started = False
    _started_at = 0
    _mem_samples.clear()
    _last_mem_sample_at = 0
    _gc_ms = 0.0
    _gc_count = 0
    _gc_start = None
    _gc_pause_ring.clear()
    _gc_tax_events.clear()
    _req_total = 0
    _req_errors = 0
    _first_req_at = 0
    _last_req_at = 0
    _thread_samples.clear()
    _last_thread_sample_at = 0
    global _gc_stats_override, _deadlock_override, _cgroup_override
    global _cpu_throttle_override, _fd_override
    global _cold_start_ms, _cold_start_reader_override
    _gc_stats_override = None
    _deadlock_override = None
    _cgroup_override = None
    _cpu_throttle_override = None
    _fd_override = None
    _cold_start_ms = None
    _cold_start_reader_override = None
    global _startup_modules
    _startup_modules = None
    # Wipe the cpuThrottling host-suspend discount state (its sensor-listener
    # wiring survives — module wiring, not data).
    global _cpu_idle_baseline, _cpu_throttle_anchor, _cpu_throttle_last
    global _suspend_periods_discounted
    global _suspend_throttled_discounted, _suspend_throttle_wakes
    with _cpu_throttle_lock:
        _cpu_idle_baseline = None
        _cpu_throttle_anchor = None
        _cpu_throttle_last = None
        _suspend_periods_discounted = 0
        _suspend_throttled_discounted = 0
        _suspend_throttle_wakes = 0


def _reset_reliability() -> None:
    """Drop the finished-response counts behind the reliability reading."""
    global _req_total, _req_errors, _first_req_at, _last_req_at
    _req_total = 0
    _req_errors = 0
    _first_req_at = 0
    _last_req_at = 0


#: Axis key -> how to forget everything that axis has accumulated so far.
#: Read by ``host_surface.forget_foreign_reading`` when a host that cannot take
#: the reading takes over the process; see ``extra_meters.AXIS_STATE_RESETS``
#: for the full reasoning.
AXIS_STATE_RESETS: Dict[str, Any] = {
    "reliability": _reset_reliability,
}


def drop_axis_state(key: str) -> bool:
    """Forget what ONE axis has accumulated. ``True`` when this module owns it.

    Never raises: it runs on the mount path, and the kit may never break the
    host app."""
    reset = AXIS_STATE_RESETS.get(str(key))
    if reset is None:
        return False
    try:
        reset()
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return False
    return True


# ── @internal test hooks ────────────────────────────────────────────────────
def _force_start(at: int) -> None:
    """Force the started flag without a GC callback (deterministic unit tests)."""
    global _started, _started_at
    _started = True
    _started_at = at


def _set_startup_modules_for_test(n) -> None:
    global _startup_modules
    _startup_modules = n


def _set_cpu_suspend_discount_for_test(periods: int, throttled: int) -> None:
    """@internal test hook — inject the cpuThrottling host-suspend discount
    counters directly (exercise the discounted score/caption without driving the
    real suspend sensor + cgroup counters)."""
    global _suspend_periods_discounted, _suspend_throttled_discounted
    with _cpu_throttle_lock:
        _suspend_periods_discounted = periods
        _suspend_throttled_discounted = throttled


def _set_started_at(at: int) -> None:
    global _started_at
    _started_at = at


def _enable_gc_for_test() -> None:
    """Install a no-op GC sentinel so read_gc_pressure's callback guard passes
    deterministically (no dependence on a real collection firing)."""
    global _gc_callback
    _gc_callback = lambda *a, **k: None  # noqa: E731


def _push_mem_sample(t: int, heap_mb: float, rss_mb: float) -> None:
    """Push a synthetic memory sample, bypassing throttle + /proc read."""
    _mem_samples.append({"t": t, "heapMb": heap_mb, "rssMb": rss_mb})
    if len(_mem_samples) > MEM_RING_CAP:
        del _mem_samples[0 : len(_mem_samples) - MEM_RING_CAP]


def _push_thread_sample(t: int, count: int) -> None:
    """Push a synthetic live-thread count sample, bypassing throttle."""
    _thread_samples.append({"t": t, "count": count})
    if len(_thread_samples) > THREAD_RING_CAP:
        del _thread_samples[0 : len(_thread_samples) - THREAD_RING_CAP]


def _set_gc_stats_for_test(young: int, full: int) -> None:
    """Override gc.get_stats() gen0/gen2 collection counts (deterministic tests)."""
    global _gc_stats_override
    _gc_stats_override = {"young": young, "full": full}


def _set_deadlock_for_test(value: Optional[bool]) -> None:
    """Force (or clear, with None) the deadlock verdict for threadHealth tests."""
    global _deadlock_override
    _deadlock_override = value


def _set_cgroup_for_test(value: Optional[Any]) -> None:
    """Inject cgroup memory numbers for containerPressure: a
    {"usedBytes", "maxBytes"} dict, or "unavailable" to force the no-cgroup path.
    Pass None to read the live cgroup again."""
    global _cgroup_override
    _cgroup_override = value


def _set_cpu_throttle_for_test(value: Optional[Any]) -> None:
    """Inject cgroup CPU CFS-bandwidth numbers for cpuThrottling: a
    {"periods", "throttled"} dict, or "unavailable" to force the no-cgroup /
    unlimited-quota path. Pass None to read the live cgroup again."""
    global _cpu_throttle_override
    _cpu_throttle_override = value


def _set_fd_for_test(value: Optional[Any]) -> None:
    """Inject file-descriptor numbers for fdSaturation: a {"open", "max"} dict,
    or "unavailable" to force the unreadable / unlimited-limit path. Pass None
    to read the live process again."""
    global _fd_override
    _fd_override = value


def _set_cold_start_reader_for_test(reader: Optional[Any]) -> None:
    """Inject a fake process-age reader for coldStart: a zero-arg callable that
    returns the age in ms (or raises to exercise the unreadable path). Pass None
    to read live /proc again. The sandbox has a real /proc, so tests use this to
    stay deterministic."""
    global _cold_start_reader_override
    _cold_start_reader_override = reader


def _reset_cold_start_for_test() -> None:
    """Drop BOTH frozen coldStart captures so a fresh captureColdStart can run
    (used between tests to reset the freeze-once state). The readiness watch is
    process-wide, so a socket opened by any other test would otherwise hand the
    next case a mark it never set up."""
    global _cold_start_ms
    _cold_start_ms = None
    ready_to_serve._reset_for_test()


def _add_gc(ms: float) -> None:
    """Record synthetic GC time."""
    global _gc_ms, _gc_count
    _gc_ms += ms
    _gc_count += 1
    _gc_tax_events.append({"t": float(_now_ms()), "ms": ms})


def _push_gc_tax_event(at: int, ms: float) -> None:
    _gc_tax_events.append({"t": float(at), "ms": float(ms)})


def _push_gc_pause(ms: float) -> None:
    """Push a synthetic GC pause duration (ms) into the tail ring + bump the
    collection count, bypassing the real gc.callbacks hook (deterministic tests)."""
    global _gc_count
    _gc_pause_ring.append(ms)
    if len(_gc_pause_ring) > GC_PAUSE_RING_CAP:
        del _gc_pause_ring[0 : len(_gc_pause_ring) - GC_PAUSE_RING_CAP]
    _gc_count += 1


def _note_req(status_code: int, at: int) -> None:
    """Record a synthetic request outcome (drives reliability without a server)."""
    global _req_total, _req_errors, _first_req_at, _last_req_at
    _req_total += 1
    if status_code >= SERVER_ERROR_STATUS:
        _req_errors += 1
    if _first_req_at == 0:
        _first_req_at = at
    _last_req_at = at


def _is_running() -> bool:
    return _started


def _mem_count() -> int:
    return len(_mem_samples)


def _req_total_count() -> int:
    return _req_total


_vitals_internals = {
    "MEM_GROWTH_GOOD": MEM_GROWTH_GOOD,
    "MEM_GROWTH_POOR": MEM_GROWTH_POOR,
    "MIN_MEM_SAMPLES": MIN_MEM_SAMPLES,
    "MIN_MEM_SPAN_MS": MIN_MEM_SPAN_MS,
    "MEM_STARTUP_EXCLUSION_MS": MEM_STARTUP_EXCLUSION_MS,
    "RESIDENT_GROWTH_GOOD": RESIDENT_GROWTH_GOOD,
    "RESIDENT_GROWTH_POOR": RESIDENT_GROWTH_POOR,
    "MEM_THROTTLE_MS": MEM_THROTTLE_MS,
    "MEM_RING_CAP": MEM_RING_CAP,
    "GC_PCT_GOOD": GC_PCT_GOOD,
    "GC_PCT_POOR": GC_PCT_POOR,
    "MIN_GC_ELAPSED_MS": MIN_GC_ELAPSED_MS,
    "GC_TAX_GOOD": GC_TAX_GOOD,
    "GC_TAX_POOR": GC_TAX_POOR,
    "GC_PAUSE_P99_GOOD": GC_PAUSE_P99_GOOD,
    "GC_PAUSE_P99_POOR": GC_PAUSE_P99_POOR,
    "MIN_GC_PAUSE_SAMPLES": MIN_GC_PAUSE_SAMPLES,
    "GC_PAUSE_RING_CAP": GC_PAUSE_RING_CAP,
    "ERR_PCT_GOOD": ERR_PCT_GOOD,
    "ERR_PCT_POOR": ERR_PCT_POOR,
    "MIN_REQ_FOR_RELIABILITY": MIN_REQ_FOR_RELIABILITY,
    "SERVER_ERROR_STATUS": SERVER_ERROR_STATUS,
    "CRASHFREE_RATE_POOR": CRASHFREE_RATE_POOR,
    "CRASHFREE_MIN_WINDOW_MIN": CRASHFREE_MIN_WINDOW_MIN,
    "GC_GEN_RATIO_GOOD": GC_GEN_RATIO_GOOD,
    "GC_GEN_RATIO_POOR": GC_GEN_RATIO_POOR,
    "MIN_YOUNG_GCS": MIN_YOUNG_GCS,
    "FINALIZER_BACKLOG_GOOD": FINALIZER_BACKLOG_GOOD,
    "FINALIZER_BACKLOG_POOR": FINALIZER_BACKLOG_POOR,
    "LOOP_LAG_GOOD_MS": LOOP_LAG_GOOD_MS,
    "LOOP_LAG_POOR_MS": LOOP_LAG_POOR_MS,
    "THREAD_GROWTH_GOOD": THREAD_GROWTH_GOOD,
    "THREAD_GROWTH_POOR": THREAD_GROWTH_POOR,
    "MIN_THREAD_SAMPLES": MIN_THREAD_SAMPLES,
    "MIN_THREAD_SPAN_MS": MIN_THREAD_SPAN_MS,
    "THREAD_THROTTLE_MS": THREAD_THROTTLE_MS,
    "THREAD_RING_CAP": THREAD_RING_CAP,
}
