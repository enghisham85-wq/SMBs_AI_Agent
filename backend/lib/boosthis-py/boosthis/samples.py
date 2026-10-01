"""In-process sample buffer.

The tracker (``@track_perf`` / ``perf()``) pushes every measurement into this
thread-safe ring buffer. Anything that wants to read live perf state — the
HTTP server, the MCP server, the ``boosthis context`` command — reads from
here.

This ring itself stays in-process: it is what the local surfaces read, and
nothing reads it off the host. Uploading is a SEPARATE path
(:mod:`boosthis.sample_uploader`), fed from the same record moment in
``tracker._emit`` and inert until the developer authorises meter-sharing — the
same gate the snapshot mirror and trace spans ride.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Deque, Iterable, Literal

Rating = Literal["good", "needs-work", "poor"]

_MAX_SAMPLES = 1000
_lock = threading.Lock()
_samples: Deque["Sample"] = deque(maxlen=_MAX_SAMPLES)

# ── What each route looked like when it ARRIVED ───────────────────────────
#
# The ring above is shared by every route and evicts oldest-first, so what it
# holds for one route is whatever survived the others' traffic. Anything that
# wants a route's FIRST samples — the learned budget's baseline does — cannot
# get them from the ring: on a busy app the oldest surviving sample of a route
# is minutes old, so a "baseline" drawn there is the app compared against
# itself just now, and drift is invisible by construction.
#
# Reading them at arrival is the only way that holds. It is done HERE rather
# than by a reader registering a callback, because a callback only starts
# catching samples when its module happens to be imported: an app that runs
# for an hour before anything reads a budget would hand the first read the
# same minutes-old window. This record exists from the first sample, whoever
# reads it and whenever.
#
# BOUNDED. At most ``MAX_TRACKED_ROUTES`` routes are kept, each with at most
# ``ROUTE_HEAD_SAMPLES`` samples; a long-running process with high-cardinality
# labels drops the least-recently-seen route rather than growing without
# limit. A dropped route's head restarts from its next sample, so it reads as
# learning again — never as a stale yardstick.

#: How many of a route's first samples are kept. Must be at least the warm-up
#: plus baseline the budget reads (``BUDGET_WARMUP_N + BUDGET_BASELINE_N``); a
#: test in budgets holds the two together.
ROUTE_HEAD_SAMPLES = 25

#: How many distinct routes carry a head at once.
MAX_TRACKED_ROUTES = 128


@dataclass(frozen=True)
class Sample:
    """A single perf measurement."""

    name: str
    duration_ms: int
    rating: Rating
    timestamp_ms: int  # epoch ms

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RouteHead:
    """A route's arrival record — see the note above the constants."""

    #: Every sample of this route recorded in this process, counted as it
    #: arrived. Unlike the ring's contents this never goes down, so a route
    #: holding fewer samples than it has been sent is positive evidence that
    #: the shared ring evicted some.
    total: int
    #: This route's first ROUTE_HEAD_SAMPLES samples, in arrival order. Frozen
    #: once full, so anything derived from it answers the same on every read.
    head: list["Sample"]
    #: Last arrival, epoch ms. Decides which head is dropped when full.
    last_seen_ms: int


_route_heads: dict[str, RouteHead] = {}
_routes_dropped = 0


def _note_arrival_locked(s: "Sample") -> None:
    """Update the arrival record. Caller holds ``_lock``."""
    global _routes_dropped
    existing = _route_heads.get(s.name)
    if existing is not None:
        existing.total += 1
        existing.last_seen_ms = s.timestamp_ms
        if len(existing.head) < ROUTE_HEAD_SAMPLES:
            existing.head.append(s)
        return
    if len(_route_heads) >= MAX_TRACKED_ROUTES:
        oldest = min(_route_heads, key=lambda k: _route_heads[k].last_seen_ms)
        del _route_heads[oldest]
        _routes_dropped += 1
    _route_heads[s.name] = RouteHead(total=1, head=[s], last_seen_ms=s.timestamp_ms)


def route_head(name: str) -> RouteHead | None:
    """This route's arrival record, or ``None`` where the route has never been
    seen in this process (or its head was dropped to stay within the bound)."""
    with _lock:
        return _route_heads.get(name)


def tracked_routes() -> list[str]:
    """Every route with an arrival record, including ones the ring no longer
    holds a single sample of. A surface listing routes must union this with
    the ring's own names, or a fully evicted route silently disappears."""
    with _lock:
        return list(_route_heads.keys())


def dropped_route_count() -> int:
    """How many routes' heads were dropped to stay within MAX_TRACKED_ROUTES."""
    with _lock:
        return _routes_dropped


def record(name: str, duration_ms: int, rating: Rating) -> Sample:
    """Append a sample to the buffer, and queue it for upload.

    Called on EVERY measurement, from all four places one can be taken: the
    request tracker, a background job, an MCP tool call, and the kit's own
    ``/record`` endpoint. The upload queue is fed from here, not from the
    tracker, because a queue fed from one caller leaves the other three
    measuring locally while the dashboard still says "registered, no
    measurements yet" — which is exactly the failure this queue exists to end.

    Uploading is inert (retains nothing) until :mod:`boosthis.telemetry` wires a
    submitter under effective meter-sharing, applies its own PII guard to the
    label, and never blocks or raises.
    """
    s = Sample(
        name=name,
        duration_ms=duration_ms,
        rating=rating,
        timestamp_ms=int(time.time() * 1000),
    )
    with _lock:
        _samples.append(s)
        _note_arrival_locked(s)
    try:
        # Imported at call time: the uploader is a leaf module, but keeping the
        # import out of this module's body means the in-process ring can never
        # be made unimportable by a change in the upload path.
        from boosthis import sample_uploader

        sample_uploader.enqueue_sample(
            {"routeLabel": name, "durationMs": duration_ms, "rating": rating}
        )
    except Exception:  # noqa: BLE001
        pass  # instrumentation must never disturb the host app.
    return s


def recent(limit: int = 100, name: str | None = None) -> list[Sample]:
    """Return the most recent samples, optionally filtered by route name.

    Snapshots the deque under the lock before iterating — a deque iterator
    captured but not consumed under the lock can raise ``RuntimeError:
    deque mutated during iteration`` when the tracker is appending from
    another thread (which is exactly the HTTP/MCP-server scenario).
    """
    with _lock:
        snapshot = list(_samples)
    items: Iterable[Sample] = reversed(snapshot)
    if name:
        items = (s for s in items if s.name == name)
    out: list[Sample] = []
    for s in items:
        out.append(s)
        if len(out) >= limit:
            break
    return out


def summary() -> dict:
    """Aggregate stats across the entire buffer — useful for AI handoff."""
    with _lock:
        snap = list(_samples)
    if not snap:
        return {
            "total": 0,
            "good": 0,
            "needsWork": 0,
            "poor": 0,
            "p50_ms": None,
            "p75_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "byRoute": {},
        }
    durations = sorted(s.duration_ms for s in snap)
    by_rating = {"good": 0, "needsWork": 0, "poor": 0}
    by_route: dict[str, dict] = {}
    for s in snap:
        key = "needsWork" if s.rating == "needs-work" else s.rating
        by_rating[key] += 1
        r = by_route.setdefault(s.name, {"count": 0, "max_ms": 0, "worst_rating": "good"})
        r["count"] += 1
        r["max_ms"] = max(r["max_ms"], s.duration_ms)
        # Track the worst rating per route (poor > needs-work > good)
        order = {"good": 0, "needs-work": 1, "poor": 2}
        if order[s.rating] > order[r["worst_rating"]]:
            r["worst_rating"] = s.rating

    def pct(p: float) -> int:
        idx = min(len(durations) - 1, int(len(durations) * p))
        return durations[idx]

    return {
        "total": len(snap),
        **by_rating,
        "p50_ms": pct(0.50),
        "p75_ms": pct(0.75),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "byRoute": by_route,
    }


_generation = 0


def epoch() -> int:
    """Bumped every time the ring is emptied.

    A reader that CACHES something derived from the ring needs to know when
    the ring it derived from stopped existing — the learned budget freezes a
    baseline that has to outlive eviction, so it cannot re-derive one on every
    read to notice a :func:`clear`. Exposing a counter keeps that knowledge
    here without this module having to import its readers, which would put the
    leaf of the module graph at the head of a cycle.
    """
    with _lock:
        return _generation


def clear() -> None:
    """Empty the buffer. Mainly for tests."""
    global _generation, _routes_dropped
    with _lock:
        _samples.clear()
        _route_heads.clear()
        _routes_dropped = 0
        _generation += 1


# ── Circuit summary (local-only session_summary enrichment) ────────────────
# Pure, on-device computation over the same in-process ring. Surfaces two
# "circuit integrity" signals for the developer's own AI:
#
#   1. LOOP SUSPECTS — one route hit again and again in a tight cadence
#      (≥8 hits inside a 30s window with a median gap ≤2s). That's the
#      inbound signature of a retry/redirect loop round-tripping to itself
#      (rule: py-retry-redirect-loop).
#   2. FAN-OUT BURST — the max number of requests landing inside any 1s
#      window (≥6 flags it). A page/aggregator driving N concurrent calls
#      shows up here as a spike (rule: py-fanout-overload).
#
# Counts + already-sanitized route labels only — computed on demand, never
# uploaded, never part of any snapshot/telemetry payload. Thresholds are
# byte-parity with the Node/Java/Go siblings.

_CIRCUIT_LOOP_MIN_HITS = 8
_CIRCUIT_LOOP_WINDOW_MS = 30_000
_CIRCUIT_LOOP_MAX_MEDIAN_GAP_MS = 2_000
_CIRCUIT_BURST_WINDOW_MS = 1_000
_CIRCUIT_BURST_MIN = 6
_CIRCUIT_MAX_LOOP_SUSPECTS = 5


def _median(sorted_vals: list[int]) -> int:
    if not sorted_vals:
        return 0
    mid = len(sorted_vals) // 2
    if len(sorted_vals) % 2 == 1:
        return sorted_vals[mid]
    # Integer half-up (Python's round() is banker's rounding; the JS/Java/Go
    # siblings round half-up, so mirror them exactly).
    return (sorted_vals[mid - 1] + sorted_vals[mid] + 1) // 2


def circuit_summary() -> dict:
    """Loop/fan-out circuit signals over the ring. Local-only, closed shape."""
    with _lock:
        snap = list(_samples)
    out: dict = {
        "loop_suspects": [],
        "burst_max_1s": 0,
        "burst_route_count": 0,
        "related_rules": [],
    }
    if not snap:
        return out

    # ── Loop suspects: per-route sliding 30s window ──
    by_route: dict[str, list[int]] = {}
    for s in snap:
        by_route.setdefault(s.name, []).append(s.timestamp_ms)
    for route, ts in by_route.items():
        ts = sorted(ts)
        if len(ts) < _CIRCUIT_LOOP_MIN_HITS:
            continue
        best_start = 0
        best_count = 0
        lo = 0
        for hi in range(len(ts)):
            while ts[hi] - ts[lo] > _CIRCUIT_LOOP_WINDOW_MS:
                lo += 1
            count = hi - lo + 1
            if count > best_count:
                best_count = count
                best_start = lo
        if best_count < _CIRCUIT_LOOP_MIN_HITS:
            continue
        window_ts = ts[best_start : best_start + best_count]
        gaps = sorted(
            window_ts[i] - window_ts[i - 1] for i in range(1, len(window_ts))
        )
        median_gap = _median(gaps)
        if median_gap > _CIRCUIT_LOOP_MAX_MEDIAN_GAP_MS:
            continue
        out["loop_suspects"].append(
            {
                "route": route,
                "hits": best_count,
                "window_s": max(1, (window_ts[-1] - window_ts[0] + 500) // 1000),
                "median_gap_ms": median_gap,
            }
        )
    out["loop_suspects"].sort(key=lambda d: -d["hits"])
    del out["loop_suspects"][_CIRCUIT_MAX_LOOP_SUSPECTS:]

    # ── Fan-out burst: densest 1s window across ALL samples ──
    all_ts = sorted(((s.timestamp_ms, s.name) for s in snap), key=lambda t: t[0])
    burst_max = 0
    burst_lo = 0
    burst_hi = 0
    lo = 0
    for hi in range(len(all_ts)):
        while all_ts[hi][0] - all_ts[lo][0] > _CIRCUIT_BURST_WINDOW_MS:
            lo += 1
        count = hi - lo + 1
        if count > burst_max:
            burst_max = count
            burst_lo = lo
            burst_hi = hi
    out["burst_max_1s"] = burst_max
    if burst_max > 0:
        out["burst_route_count"] = len(
            {all_ts[i][1] for i in range(burst_lo, burst_hi + 1)}
        )

    if out["loop_suspects"]:
        out["related_rules"].append("py-retry-redirect-loop")
    if out["burst_max_1s"] >= _CIRCUIT_BURST_MIN:
        out["related_rules"].append("py-fanout-overload")
    return out
