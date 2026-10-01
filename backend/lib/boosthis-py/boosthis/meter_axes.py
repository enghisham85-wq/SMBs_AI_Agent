"""Boosthis: server-side additive meter axes (Python).

Python parity for the extra meters the React Native kit ships in
``lib/boosthis-runtime-rn/src/meterAxes.ts``. Each meter here is ADDITIVE and
display-only: it enriches the dashboard + the uploaded snapshot but NEVER feeds
the composite Speed score (TTFF/TTI/FID). Every threshold, warm-up gate, and
rating band mirrors the RN source (and, for schedulerLatency, the Go sibling in
``lib/boosthis-go/runtime_vitals.go``) so the same number means the same thing
in every language.

Six meters live here, alongside the ones already shipped by
``runtime_vitals.py`` / ``event_loop_lag.py``:

- **confidence**    — how much real data backs the numbers (min sample count of
  the least-sampled SCORED route). Emitted as FOUR scalar top-level axis keys.
- **baseline**      — each route graded against ITS OWN earlier samples (recent
  window vs. baseline window), so a route that doubled is flagged even while
  nominally "good". Mirrors RN ``computeBaselineScore``.
- **latencyFloor**  — the server analogue of RN's Frame Floor: request latency
  cut into fixed 10s windows, worst window's p95 scored so one bad minute is
  never averaged away.
- **network**       — OUTBOUND call reliability (stall / timeout / failure rate +
  p75 latency), reusing the retry-storm detector's outbound observation point.
  Mirrors RN ``computeNetworkScore``.
- **idle**          — CPU burned while NO request is in flight (busy CPU ms /
  idle wall ms), accumulated over idle windows. Mirrors RN
  ``computeIdleEfficiency``.
- **schedulerLatency** — a very-low-cost daemon-thread heartbeat measuring how
  late the interpreter actually wakes the thread (p99 lateness in ms). Works on
  BOTH sync/WSGI and async apps — the real stall blind-spot for sync apps, where
  ``asyncioLoopLag`` never fires. Mirrors Go's schedulerLatency bands.

PRIVACY: counts, durations, ratios, scores + fixed rating buckets only. Route
identity NEVER enters an axis object — the confidence/baseline/latencyFloor
readers consume already-sanitized route labels from the sample ring purely to
GROUP by route, and only numbers leave. Everything is in-memory and bounded.

GUEST SAFETY: no reader ever raises (instrumentation must never take down the
host); the heartbeat is a single daemon thread that sleeps a fixed interval and
never blocks interpreter shutdown; all state is bounded by fixed ring caps.
"""

from __future__ import annotations

import threading
import time
import math
from typing import Any, Callable, Dict, List, Optional

from .runtime_flags import is_boosthis_disabled
from .candidates import read_recurrence_rows, recurrence_memory_durable
from .health_axes import MIN_SAMPLES_FOR_AXES, linear_score, rating_for
from .thresholds import SCORE_THRESHOLDS
from .suspend_sensor import sample_in_wake_window
from boosthis import samples
from boosthis import mcp_measure

# ── confidence bands (mirror RN CONFIDENCE_CUTOFFS) ─────────────────────────
# <=0 none/pending · <5 low/poor · <20 medium/needs-work · >=20 high/good.
CONFIDENCE_MEDIUM = 5
CONFIDENCE_HIGH = 20

_CONFIDENCE_CAPTIONS = {
    "none": "no samples yet",
    "low": "few samples",
    "medium": "moderate samples",
    "high": "well-sampled",
}

# A route needs at least this many samples to be "scored" (mirrors the axis
# honesty gate used across the kit). Routes below this are excluded from the
# confidence sample count, exactly as RN excludes insufficient-data screens.
CONFIDENCE_MIN_SCORED_SAMPLES = 5

# ── baseline bands ──────────────────────────────────────────────────────────
BASELINE_GOOD = 1.2
BASELINE_POOR = 2.0
#: Combined floor: the frozen baseline's samples plus the recent window's.
BASELINE_MIN_SAMPLES = 6
#: How many of the most-recent samples form the "current" window.
BASELINE_RECENT_N = 3
#: How many FROZEN baseline samples a route needs before the axis will judge
#: it. Named separately from the recent window's floor because the two windows
#: are separate inputs, and a reader must be able to see which side is short.
BASELINE_BASELINE_N = BASELINE_MIN_SAMPLES - BASELINE_RECENT_N
BASELINE_MIN_DELTA_MS = 50
#: Which algorithm produced a Baseline axis reading — the same name the
#: learned budget publishes, because it is now the same derivation over the
#: same two windows.
BASELINE_ALGORITHM = "device-ring-frozen-baseline"

# ── latencyFloor config (server analogue of RN Frame Floor) ─────────────────
# Fixed wall-clock window width; each window's p95 latency is the reading, and
# the WORST completed window is scored so a single bad minute can't hide.
LATENCY_FLOOR_WINDOW_MS = 10_000
LATENCY_FLOOR_MIN_WINDOWS = 3
# Latency-floor bands (ms) — a SERVER REQUEST bar, chosen here and named here.
# This axis used to score on the shared TTI band (good 500ms), which is the bar
# for a PAGE becoming interactive; borrowed onto a server API it rated a
# worst-window p95 of 401ms as 100/good. These are the numbers the web kit
# already justifies for SERVER-side processing: 200ms leaves room inside a
# one-second page for the network and the render it still has to pay for, and a
# second of server work has spent the whole budget before anything reaches the
# browser. A worst-window p95 stops being "good" at ~320ms.
_LATENCY_FLOOR_GOOD = 200
_LATENCY_FLOOR_POOR = 1_000

# ── network bands (mirror RN computeNetworkScore) ───────────────────────────
NETWORK_MIN_ATTEMPTS = 3
NETWORK_P75_GOOD_MS = 800
NETWORK_P75_POOR_MS = 3000
NETWORK_STALL_RATE_GOOD = 0.01
NETWORK_STALL_RATE_POOR = 0.1
# A call that ends without ever producing a response, having waited at least
# this long, is the SILENT-DROP class this axis exists for (a "stall") rather
# than a loud failure. Mirrors the Node sampler's NETWORK_STALL_MS horizon.
NETWORK_STALL_MS = 10_000
# Bounded ring of outbound-call durations so a long-lived process can never grow
# this unbounded.
NETWORK_RING_CAP = 500

# ── idle bands (mirror RN IDLE_EFFICIENCY_THRESHOLDS) ───────────────────────
# RN scores a fraction (good 0.02 / poor 0.2); the server reports the same
# quantity as a percentage, so the bands are the fraction × 100.
IDLE_BUSY_PCT_GOOD = 2.0
IDLE_BUSY_PCT_POOR = 20.0
# Minimum idle windows before the axis leaves pending — one stray idle window
# must not define the reading (mirrors RN's minimum-sample gate).
IDLE_MIN_WINDOWS = 3

# ── MCP tool speed bands (mirror Node MCP_TOOLS_THRESHOLDS) ─────────────────
# "Which of this project's MCP tools is slow?" Fed by the closed-set
# ``mcp.call.*`` sample labels (see mcp_measure.py). Each TOOL is a scored unit
# once it has MCP_TOOLS_MIN_SAMPLES samples (success + error samples both
# count). Latency sub-score = the WORST scored tool's p95 on the shared TTI
# thresholds; error sub-score = overall failed-call rate (good <=1% · poor
# >=10%). Overall = the WORSE of the two. Pending (axis OMITTED) until at least
# one tool is scored. Label-free: only ordered duration arrays + error counts
# reach the scorer.
MCP_TOOLS_ERR_RATE_GOOD = 0.01
MCP_TOOLS_ERR_RATE_POOR = 0.1
MCP_TOOLS_MIN_SAMPLES = 5
_MCP_TTI_GOOD = SCORE_THRESHOLDS["tti"]["good"]
_MCP_TTI_POOR = SCORE_THRESHOLDS["tti"]["poor"]

# ── schedulerLatency bands (mirror Go schedP99Good / schedP99Poor) ──────────
SCHED_P99_GOOD_MS = 1.0
SCHED_P99_POOR_MS = 50.0
# Interval the heartbeat thread sleeps between wake-lateness probes. 250ms is
# frequent enough to build a p99 quickly yet effectively free (a sleeping thread
# costs nothing).
SCHED_INTERVAL_MS = 250.0
# Minimum probes before the axis leaves pending (mirrors the warm-up gates on
# the other meters — a couple of reads on a just-booted process mean nothing).
SCHED_MIN_SAMPLES = 20
# Bounded ring of wake-lateness samples (ms).
SCHED_RING_CAP = 500

# worstFreeze — high-water stall observed by the SAME scheduler heartbeat.
WORST_FREEZE_GOOD_MS = 250.0
WORST_FREEZE_POOR_MS = 2_000.0
WORST_FREEZE_MIN_SAMPLES = 1

# ── backgroundWork bands (mirror Node jobWork.ts / meterAxes.ts) ────────────
JOB_FAIL_PCT_GOOD = 1
JOB_FAIL_PCT_POOR = 20
JOB_MISSED_GOOD = 0
JOB_MISSED_POOR = 3
JOB_OVERLAP_GOOD = 0
JOB_OVERLAP_POOR = 5
JOB_WAIT_MS_GOOD = 1_000
JOB_WAIT_MS_POOR = 60_000
JOB_WORK_MIN_RUNS = MIN_SAMPLES_FOR_AXES


def compute_background_work(stats: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Score job reliability without pretending unmeasured dimensions are zero.

    Duration is reported but deliberately not graded: a healthy nightly import
    can take minutes. The verdict is the worst supported part among failures,
    overlaps, known cadence misses, and queue wait when the queue reported it.
    """
    if not stats:
        return None
    try:
        unattached = max(0, round(stats.get("unattachedSystems", 0) or 0))
        runs = max(0, round(stats.get("runs", 0) or 0))
        recurring = max(0, round(stats.get("recurring", 0) or 0))
        missed = (
            max(0, round(stats.get("missed", 0) or 0)) if recurring > 0 else None
        )
        if runs < JOB_WORK_MIN_RUNS:
            if runs == 0 and unattached == 0:
                return None
            return {
                "score": None,
                "rating": "pending",
                "measurable": 0,
                "runs": runs,
                "jobNames": stats.get("jobNames", 0) or 0,
                "otherNames": stats.get("otherNames", 0) or 0,
                "failed": stats.get("failed", 0) or 0,
                "failPct": 0,
                "retried": stats.get("retried", 0) or 0,
                "retryWorst": max(1, round(stats.get("retryWorst", 1) or 1)),
                "overlaps": stats.get("overlaps", 0) or 0,
                "p95Ms": 0,
                "worstMs": 0,
                "waitRuns": stats.get("waitRuns", 0) or 0,
                "waitP95Ms": None,
                "missed": missed,
                "recurring": recurring,
                "hostedRuns": stats.get("hostedRuns", 0) or 0,
                "manualRuns": stats.get("manualRuns", 0) or 0,
                "attachedSystems": stats.get("attachedSystems", 0) or 0,
                "unattachedSystems": unattached,
            }

        failed = max(0, round(stats.get("failed", 0) or 0))
        fail_pct = round((failed / runs) * 1000) / 10
        overlaps = max(0, round(stats.get("overlaps", 0) or 0))
        parts = [
            linear_score(fail_pct, JOB_FAIL_PCT_GOOD, JOB_FAIL_PCT_POOR),
            linear_score(overlaps, JOB_OVERLAP_GOOD, JOB_OVERLAP_POOR),
        ]
        if missed is not None:
            parts.append(linear_score(missed, JOB_MISSED_GOOD, JOB_MISSED_POOR))
        wait_runs = max(0, round(stats.get("waitRuns", 0) or 0))
        raw_wait = stats.get("waitP95Ms")
        wait_p95 = (
            max(0, round(raw_wait))
            if isinstance(raw_wait, (int, float)) and wait_runs > 0
            else None
        )
        if wait_p95 is not None:
            parts.append(linear_score(wait_p95, JOB_WAIT_MS_GOOD, JOB_WAIT_MS_POOR))
        score = min(parts)
        return {
            "score": score,
            "rating": rating_for(score),
            "measurable": 1,
            "runs": runs,
            "jobNames": stats.get("jobNames", 0) or 0,
            "otherNames": stats.get("otherNames", 0) or 0,
            "failed": failed,
            "failPct": fail_pct,
            "retried": max(0, round(stats.get("retried", 0) or 0)),
            "retryWorst": max(1, round(stats.get("retryWorst", 1) or 1)),
            "overlaps": overlaps,
            "p95Ms": max(0, round(stats.get("p95Ms", 0) or 0)),
            "worstMs": max(0, round(stats.get("worstMs", 0) or 0)),
            "waitRuns": wait_runs,
            "waitP95Ms": wait_p95,
            "missed": missed,
            "recurring": recurring,
            "hostedRuns": max(0, round(stats.get("hostedRuns", 0) or 0)),
            "manualRuns": max(0, round(stats.get("manualRuns", 0) or 0)),
            "attachedSystems": max(
                0, round(stats.get("attachedSystems", 0) or 0)
            ),
            "unattachedSystems": unattached,
        }
    except Exception:  # noqa: BLE001
        return None


def _read_background_work() -> Optional[Dict[str, Any]]:
    try:
        from boosthis.job_work import get_job_work_stats

        axis = compute_background_work(get_job_work_stats())
        if axis is None:
            return None
        # Unknown is omission on the wire, not a zero. Keep compute_* parity
        # useful to callers while making the assembled snapshot exact.
        return {key: value for key, value in axis.items() if value is not None}
    except Exception:  # noqa: BLE001
        return None

# ═══════════════════════════════════════════════════════════════════════════
# confidence — FOUR scalar top-level axis keys
# ═══════════════════════════════════════════════════════════════════════════
def _confidence_level(sample_count: int) -> str:
    if not sample_count or sample_count <= 0:
        return "none"
    if sample_count < CONFIDENCE_MEDIUM:
        return "low"
    if sample_count < CONFIDENCE_HIGH:
        return "medium"
    return "high"


def _confidence_rating(level: str) -> str:
    return {
        "high": "good",
        "medium": "needs-work",
        "low": "poor",
        "none": "pending",
    }[level]


def _least_scored_route_samples(summary: Dict[str, Any]) -> int:
    """Sample count of the LEAST-sampled SCORED route (a route with at least
    ``CONFIDENCE_MIN_SCORED_SAMPLES`` samples). 0 when nothing is scored yet —
    routes without enough samples are excluded, mirroring RN."""
    counts = [
        r.get("count", 0)
        for r in summary.get("byRoute", {}).values()
        if r.get("count", 0) >= CONFIDENCE_MIN_SCORED_SAMPLES
    ]
    return min(counts) if counts else 0


def compute_confidence(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Return the four scalar confidence keys for the uploaded ``axes`` map.

    Always returned (never omitted) — a "none/pending" confidence with a
    ``0`` sample count IS honest information ("no samples yet"), and the four
    keys are scalars, not an axis object, so the honesty-gate "omit an empty
    axis" rule does not apply. Pure read. Never raises."""
    mounts = _least_scored_route_samples(summary)
    level = _confidence_level(mounts)
    return {
        "confidence": level,
        "confidenceMounts": mounts,
        "confidenceRating": _confidence_rating(level),
        "confidenceCaption": _CONFIDENCE_CAPTIONS[level],
    }


# ═══════════════════════════════════════════════════════════════════════════
# baseline — each route graded against its own earlier samples
# ═══════════════════════════════════════════════════════════════════════════
def _median(values: List[int]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2.0
    return float(s[mid])


def _route_series() -> Dict[str, Dict[str, List[int]]]:
    """Per-route pair of NON-OVERLAPPING windows for the Baseline axis.

    ``baseline`` is the SAME frozen baseline the learned budget uses: the
    route's first samples in this process, warm-up excluded, read from the
    ring module's arrival record. ``recent`` is drawn from the ring and holds
    only samples stamped strictly after that baseline closed, so the two can
    never share a sample.

    Building both halves out of the ring — which is what this did — is why a
    busy route could read as steady here while its own budget called it
    regressed: the shared 1000-sample ring holds only the last few minutes of
    a busy app, so the "earlier" half was the app measured against itself just
    now. See docs/learned-budget-contract.md.

    The route KEY is used only to group; it never leaves this module.
    """
    from . import budgets as _budgets

    buf = samples.recent(limit=1000)
    rows: Dict[str, List[Any]] = {}
    for s in reversed(buf):  # reversed → chronological
        rows.setdefault(s.name, []).append(s)

    series: Dict[str, Dict[str, List[int]]] = {}
    need = _budgets.BUDGET_WARMUP_N + _budgets.BUDGET_BASELINE_N
    for name, ordered in rows.items():
        rec = samples.route_head(name)
        # No completed baseline for this route (never enough samples, or its
        # arrival record was dropped): it is not judged here at all.
        if rec is None or len(rec.head) < need:
            continue
        head_slice = rec.head[_budgets.BUDGET_WARMUP_N : need]
        closed_at_ms = head_slice[-1].timestamp_ms
        series[name] = {
            "baseline": [s.duration_ms for s in head_slice],
            "recent": [
                s.duration_ms for s in ordered if s.timestamp_ms > closed_at_ms
            ],
        }
    return series


def compute_baseline(
    series: Optional[Dict[str, Dict[str, List[int]]]] = None,
) -> Optional[Dict[str, Any]]:
    """Baseline anomaly axis, or None while pending (no route has a completed
    baseline plus a separate recent window yet). Pure read; never raises.
    Only counts / ratios / durations leave — no route label.

    Each route arrives as two NON-OVERLAPPING windows (``baseline`` and
    ``recent``) — see :func:`_route_series`. The React Native kit still splits
    one window in its own copy of this scorer; that difference is recorded in
    docs/learned-budget-contract.md rather than papered over here.
    """
    if is_boosthis_disabled():
        return None
    try:
        s = series if series is not None else _route_series()
        scored_routes = 0
        unscored_routes = 0
        anomaly_count = 0
        worst_ratio: Optional[float] = None
        worst_baseline_ms: Optional[int] = None
        worst_current_ms: Optional[int] = None

        for windows in s.values():
            base = list((windows or {}).get("baseline") or [])
            later = list((windows or {}).get("recent") or [])
            # Each side is gated on its OWN floor. A route without a completed
            # baseline is not judged here — falling back to splitting the
            # recent window is the comparison this axis stopped making.
            if len(base) < BASELINE_BASELINE_N or len(later) < BASELINE_RECENT_N:
                continue
            recent = later[-BASELINE_RECENT_N:]
            base_med = _median(base)
            recent_med = _median(recent)
            # A sub-millisecond handler records every duration as 0 (whole-ms
            # capture), so its earlier window medians to 0 and there is no ratio
            # to take. Abstain for this route and SAY SO: counting it as scored
            # here is what let a hundred-fold regression on a fast route report
            # "steady · 1 routes".
            if base_med <= 0:
                unscored_routes += 1
                continue
            scored_routes += 1
            ratio = recent_med / base_med
            delta = recent_med - base_med
            # An anomaly must clear BOTH the relative and the absolute floor.
            if ratio < BASELINE_GOOD or delta < BASELINE_MIN_DELTA_MS:
                continue
            anomaly_count += 1
            if worst_ratio is None or ratio > worst_ratio:
                worst_ratio = ratio
                worst_baseline_ms = round(base_med)
                worst_current_ms = round(recent_med)

        if scored_routes == 0:
            if unscored_routes == 0:
                return None  # pending — no route has enough samples yet
            # Every route with enough samples was too fast to divide by: the
            # axis ABSTAINS (uploaded with measurable 0 + rating "pending") so
            # the tile can say "cannot tell" — never a perfect score, and never
            # omitted, which would read as "warming up" forever.
            return {
                "score": None,
                "rating": "pending",
                "worstRatio": None,
                "worstBaselineMs": None,
                "worstCurrentMs": None,
                "anomalyCount": 0,
                "scoredRoutes": 0,
                "unscoredRoutes": unscored_routes,
                "measurable": 0,
            }
        if worst_ratio is None:
            # Enough data, but nothing cleared the anomaly floors → steady.
            return {
                "score": 100,
                "rating": "good",
                "worstRatio": None,
                "worstBaselineMs": None,
                "worstCurrentMs": None,
                "anomalyCount": 0,
                "scoredRoutes": scored_routes,
                "unscoredRoutes": unscored_routes,
                "measurable": 1,
            }
        score = linear_score(worst_ratio, BASELINE_GOOD, BASELINE_POOR)
        return {
            "score": score,
            "rating": rating_for(score),
            "worstRatio": round(worst_ratio * 100) / 100,
            "worstBaselineMs": worst_baseline_ms,
            "worstCurrentMs": worst_current_ms,
            "anomalyCount": anomaly_count,
            "scoredRoutes": scored_routes,
            "unscoredRoutes": unscored_routes,
            "measurable": 1,
        }
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════════
# latencyFloor — worst 10s window's p95 (one terrible minute is never averaged)
# ═══════════════════════════════════════════════════════════════════════════
def _percentile(sorted_asc: List[int], p: float) -> int:
    if not sorted_asc:
        return 0
    idx = min(len(sorted_asc) - 1, int(len(sorted_asc) * p))
    return sorted_asc[idx]


def compute_latency_floor(buf: Optional[List[Any]] = None) -> Optional[Dict[str, Any]]:
    """Latency-floor axis, or None while pending (fewer than
    ``LATENCY_FLOOR_MIN_WINDOWS`` completed 10s windows with samples). Cuts the
    sample ring into fixed 10s wall-clock windows, takes each window's p95, and
    scores the WORST window on this axis's own SERVER-REQUEST band
    (``_LATENCY_FLOOR_GOOD``/``_POOR`` above, never the page-interaction TTI
    band). Pure read; never
    raises. Only counts + a duration leave — no route label, no timestamp of
    user activity (windows are relative buckets).

    HOST-SUSPEND HONESTY: a sample that landed inside a post-suspend wake window
    reflects the host RESUMING a suspended container, not the app's own latency.
    Such samples are EXCLUDED from the floor and COUNTED so the score reflects
    traffic time only, while the discount (count + worst discounted ms) stays
    visible on the axis via ``suspendDiscounts`` / ``suspendWorstMs`` — discount,
    don't hide. A genuine stall DURING traffic is not in any wake window, so it
    still lands in a window and still turns the tile red."""
    if is_boosthis_disabled():
        return None
    try:
        all_rows = buf if buf is not None else samples.recent(limit=1000)
        if not all_rows:
            return None
        # Host-suspend discount: split out samples that landed inside a recorded
        # post-wake window (resume cost, not the app's own code). They never
        # enter the floor windows; only the count + worst discounted duration
        # ride along on the axis.
        rows: List[Any] = []
        suspend_discounts = 0
        suspend_worst_ms = 0
        for s in all_rows:
            if sample_in_wake_window(s.timestamp_ms):
                suspend_discounts += 1
                if s.duration_ms > suspend_worst_ms:
                    suspend_worst_ms = int(s.duration_ms)
                continue
            rows.append(s)
        if not rows:
            return None
        # Bucket by fixed 10s windows anchored at the earliest sample so the
        # window boundaries are relative, not tied to wall-clock timestamps.
        ts = [s.timestamp_ms for s in rows]
        t0 = min(ts)
        # Only windows whose END has passed are "completed" — an in-progress
        # window would understate the p95. The newest sample's window is only
        # completed once a full window has elapsed past its start.
        t_now = max(ts)
        windows: Dict[int, List[int]] = {}
        for s in rows:
            widx = int((s.timestamp_ms - t0) // LATENCY_FLOOR_WINDOW_MS)
            windows.setdefault(widx, []).append(s.duration_ms)
        completed_worst_p95: Optional[int] = None
        completed_count = 0
        for widx, durs in windows.items():
            window_end = t0 + (widx + 1) * LATENCY_FLOOR_WINDOW_MS
            if window_end > t_now:
                continue  # window not yet complete
            completed_count += 1
            p95 = _percentile(sorted(durs), 0.95)
            if completed_worst_p95 is None or p95 > completed_worst_p95:
                completed_worst_p95 = p95
        if completed_count < LATENCY_FLOOR_MIN_WINDOWS or completed_worst_p95 is None:
            return None
        score = linear_score(
            completed_worst_p95, _LATENCY_FLOOR_GOOD, _LATENCY_FLOOR_POOR
        )
        axis: Dict[str, Any] = {
            "score": score,
            "rating": rating_for(score),
            "worstMs": completed_worst_p95,
            "windowCount": completed_count,
        }
        # Attach the suspend discount only when > 0 (discount, don't hide).
        # Numeric-only fields — the server allowlists exactly these two.
        if suspend_discounts > 0:
            axis["suspendDiscounts"] = suspend_discounts
            axis["suspendWorstMs"] = suspend_worst_ms
        try:
            from boosthis.held_open import get_held_open_stats

            held = get_held_open_stats()
            if held["heldOpenExcluded"] > 0:
                axis.update(held)
        except Exception:  # noqa: BLE001
            pass
        return axis
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════════
# network — outbound-call reliability (reuses the retry-storm observation point)
# ═══════════════════════════════════════════════════════════════════════════
_net_lock = threading.Lock()
_net_durations: List[int] = []
_net_attempts = 0
_net_completed = 0
_net_failed = 0
_net_timeout = 0
_net_stall = 0
_net_worst_ms = 0


def record_network_outcome(
    duration_ms: Optional[float], outcome: str = "ok"
) -> None:
    """Record ONE outbound call's duration + coarse outcome bucket.

    Fed from the SAME stdlib ``http.client`` observation point that feeds the
    repeated-work meter (``live_detectors``'s outbound hook, which is the only
    place the kit learns how a call ENDED), and from any client the app reports
    explicitly through ``live_detectors.record_outbound_attempt`` — this adds no
    second interception layer. ``outcome`` is one of ``"ok" | "error" |
    "timeout" | "stall"``.

    ``duration_ms`` is banked for EVERY outcome, not just ``"ok"``: a failed
    attempt cost the app the time it took, and a call that failed five times
    before working cost all six attempts. Reporting only the success made that
    call read as one measurement and hid the retries entirely. It is None only
    where the caller genuinely has no measurement — a call still in flight past
    the horizon, or a feed that fires at call START and never learns the end.

    Counts + durations only — never a host, URL, or status. No-op under the
    kill-switch. Never raises."""
    if is_boosthis_disabled():
        return
    try:
        with _net_lock:
            global _net_attempts, _net_completed, _net_failed
            global _net_timeout, _net_stall, _net_worst_ms
            _net_attempts += 1
            if outcome == "timeout":
                _net_timeout += 1
            elif outcome == "stall":
                _net_stall += 1
            elif outcome == "error":
                _net_failed += 1
            else:
                _net_completed += 1
            if duration_ms is not None:
                d = int(round(max(0.0, float(duration_ms))))
                _net_durations.append(d)
                if len(_net_durations) > NETWORK_RING_CAP:
                    del _net_durations[0 : len(_net_durations) - NETWORK_RING_CAP]
                if d > _net_worst_ms:
                    _net_worst_ms = d
    except Exception:  # noqa: BLE001
        pass  # observation must never disturb the host


def _unwatched_clients() -> int:
    """How many outbound-HTTP client libraries this process loaded that the kit
    cannot watch. 0 when everything the app uses is observed. Never raises."""
    try:
        from boosthis.outbound_clients import unwatched_client_count

        return unwatched_client_count()
    except Exception:  # noqa: BLE001
        return 0


def read_network() -> Optional[Dict[str, Any]]:
    """Network-reliability axis, or None while pending (fewer than
    ``NETWORK_MIN_ATTEMPTS`` attempts observed). Scores the WORSE of p75 latency
    and stall rate, mirroring RN ``computeNetworkScore``. Absent forever if the
    app makes no outbound calls — that is correct. Pure read; never raises.

    "No calls observed" and "calls we cannot see" are NOT the same finding.
    When nothing has been observed but the app loaded an HTTP client this kit
    cannot watch, the axis is reported as an explicit ``measurable: 0``
    can't-tell rather than left absent, so the tile says why it is empty
    instead of reading as an app that never calls anything."""
    if is_boosthis_disabled():
        return None
    try:
        with _net_lock:
            attempts = _net_attempts
            failed = _net_failed
            timeout = _net_timeout
            stall = _net_stall
            worst = _net_worst_ms
            durs = sorted(_net_durations)
        unwatched = _unwatched_clients()
        if attempts < NETWORK_MIN_ATTEMPTS:
            if unwatched <= 0:
                return None
            return {
                "score": None,
                "rating": "pending",
                "measurable": 0,
                "unwatchedClients": unwatched,
                "attemptCount": attempts,
            }
        p75 = _percentile(durs, 0.75) if durs else 0
        stall_rate = (timeout + stall) / attempts if attempts else 0.0
        latency_score = linear_score(p75, NETWORK_P75_GOOD_MS, NETWORK_P75_POOR_MS)
        stall_score = linear_score(
            stall_rate, NETWORK_STALL_RATE_GOOD, NETWORK_STALL_RATE_POOR
        )
        # Either slowness OR silent stalls should turn the tile red → the worse.
        score = min(latency_score, stall_score)
        return {
            "score": score,
            "rating": rating_for(score),
            "stallPct": round(stall_rate * 1000) / 10,
            "attemptCount": attempts,
            "failedCount": failed,
            "timeoutCount": timeout,
            "stallCount": stall,
            "p75Ms": p75,
            "worstMs": worst,
            # A real reading can still be partial: an unwatchable client sitting
            # alongside a watched one means these numbers cover some of the
            # app's calls, not all of them. The tile says so.
            "unwatchedClients": unwatched,
        }
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════════
# idle — CPU burned while NO request is in flight
# ═══════════════════════════════════════════════════════════════════════════
_idle_lock = threading.Lock()
_idle_busy_cpu_ms = 0.0
_idle_wall_ms = 0.0
_idle_windows = 0
_idle_long_tasks = 0

# An idle window whose busy CPU cleared this counts as a "long task" (sustained
# wasted work) — mirrors RN's idleLongTaskCount as a coarse ≥50ms block signal.
_IDLE_LONG_TASK_MS = 50.0


def record_idle_window(wall_gap_ms: float, cpu_gap_ms: float) -> None:
    """Record ONE completed idle window (no request in flight) with its wall
    duration and the CPU it burned. Fed from the SAME request-boundary the
    idle-burn detector already samples (``live_detectors.note_request_start``)
    — no new poller. Counts + durations only. No-op under the kill-switch.
    Never raises."""
    if is_boosthis_disabled():
        return
    try:
        if wall_gap_ms <= 0:
            return
        with _idle_lock:
            global _idle_busy_cpu_ms, _idle_wall_ms, _idle_windows, _idle_long_tasks
            busy = max(0.0, min(cpu_gap_ms, wall_gap_ms))
            _idle_busy_cpu_ms += busy
            _idle_wall_ms += wall_gap_ms
            _idle_windows += 1
            if busy >= _IDLE_LONG_TASK_MS:
                _idle_long_tasks += 1
    except Exception:  # noqa: BLE001
        pass


def read_idle() -> Optional[Dict[str, Any]]:
    """Idle-efficiency axis, or None while pending (fewer than
    ``IDLE_MIN_WINDOWS`` idle windows observed). ``idleBusyPct`` = busy CPU ms /
    idle wall ms × 100, scored on the RN thresholds. Pure read; never raises."""
    if is_boosthis_disabled():
        return None
    try:
        with _idle_lock:
            windows = _idle_windows
            busy = _idle_busy_cpu_ms
            wall = _idle_wall_ms
            long_tasks = _idle_long_tasks
        if windows < IDLE_MIN_WINDOWS or wall <= 0:
            return None
        busy_pct = 100.0 * busy / wall
        score = linear_score(busy_pct, IDLE_BUSY_PCT_GOOD, IDLE_BUSY_PCT_POOR)
        return {
            "score": score,
            "rating": rating_for(score),
            "idleBusyPct": round(busy_pct * 10) / 10,
            "idleLongTaskCount": long_tasks,
        }
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════════
# schedulerLatency — daemon-thread heartbeat wake-lateness (sync + async apps)
# ═══════════════════════════════════════════════════════════════════════════
_sched_lock = threading.Lock()
_sched_samples: List[float] = []
_sched_thread: Optional[threading.Thread] = None
_sched_stop = threading.Event()
_worst_freeze_ms = 0.0
_worst_freeze_samples = 0


def _sched_loop() -> None:
    """Daemon heartbeat: sleep a fixed interval, measure how much LATER than
    scheduled the interpreter actually woke us. Under a busy/GIL-contended or
    CPU-starved process that lateness grows; on a quiet one it is ~0. Works
    identically on sync/WSGI and async apps. Costs effectively nothing (a
    sleeping thread). Never raises; exits promptly on stop."""
    global _worst_freeze_ms, _worst_freeze_samples
    interval_s = SCHED_INTERVAL_MS / 1000.0
    previous_tick = time.monotonic()
    while not _sched_stop.is_set():
        t0 = time.monotonic()
        # Event.wait returns True if set (stop requested) → exit at once.
        if _sched_stop.wait(interval_s):
            return
        try:
            tick = time.monotonic()
            elapsed_ms = (tick - t0) * 1000.0
            lateness_ms = elapsed_ms - SCHED_INTERVAL_MS
            if lateness_ms < 0:
                lateness_ms = 0.0
            freeze_ms = (tick - previous_tick) * 1000.0 - SCHED_INTERVAL_MS
            previous_tick = tick
            if freeze_ms < 0:
                freeze_ms = 0.0
            with _sched_lock:
                _sched_samples.append(lateness_ms)
                _worst_freeze_samples += 1
                _worst_freeze_ms = max(_worst_freeze_ms, freeze_ms)
                if len(_sched_samples) > SCHED_RING_CAP:
                    del _sched_samples[0 : len(_sched_samples) - SCHED_RING_CAP]
            try:
                from boosthis.request_error_timer_meters import sample_timers
                sample_timers()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass


def start_scheduler_heartbeat() -> None:
    """Start the scheduler-latency heartbeat thread. Idempotent, no-op under the
    kill-switch. The thread is a daemon (never blocks interpreter shutdown).
    Called from ``enable_telemetry``. Never raises."""
    global _sched_thread
    if is_boosthis_disabled():
        return
    with _sched_lock:
        if _sched_thread is not None and _sched_thread.is_alive():
            return
        _sched_stop.clear()
        try:
            t = threading.Thread(
                target=_sched_loop, name="boosthis-sched-heartbeat", daemon=True
            )
            t.start()
            _sched_thread = t
        except Exception:  # noqa: BLE001
            _sched_thread = None


def read_scheduler_latency() -> Optional[Dict[str, Any]]:
    """Scheduler-latency axis (p99 wake-lateness in ms), or None while pending
    (fewer than ``SCHED_MIN_SAMPLES`` probes). Scored on the Go bands so the
    meter reads identically across languages. Pure read; never raises."""
    if is_boosthis_disabled():
        return None
    try:
        with _sched_lock:
            n = len(_sched_samples)
            if n < SCHED_MIN_SAMPLES:
                return None
            ordered = sorted(_sched_samples)
        idx = min(n - 1, int(round(0.99 * (n - 1))))
        p99 = ordered[idx]
        score = linear_score(p99, SCHED_P99_GOOD_MS, SCHED_P99_POOR_MS)
        return {
            "score": score,
            "rating": rating_for(score),
            "p99Ms": round(p99 * 100) / 100,
        }
    except Exception:  # noqa: BLE001
        return None


def read_worst_freeze() -> Optional[Dict[str, Any]]:
    if is_boosthis_disabled():
        return None
    try:
        with _sched_lock:
            count = _worst_freeze_samples
            worst = _worst_freeze_ms
        if count < WORST_FREEZE_MIN_SAMPLES:
            return None
        score = linear_score(worst, WORST_FREEZE_GOOD_MS, WORST_FREEZE_POOR_MS)
        return {
            "score": score,
            "rating": rating_for(score),
            "worstMs": round(worst * 100) / 100,
            "sampleCount": count,
            # Contract uses this field to disclose the timer's coarse cadence.
            "windowMin": round((SCHED_INTERVAL_MS / 60_000.0) * 100_000) / 100_000,
            "caption": f"{round(worst * 100) / 100} ms worst stall",
        }
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════════
# mcpTools — per-tool latency + error rate (MCP servers only)
# ═══════════════════════════════════════════════════════════════════════════
def _mcp_p95(values: List[int]) -> int:
    """p95 with the same index rule as the sample-ring summary: ascending sort,
    idx = min(len-1, floor(len*0.95))."""
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return ordered[idx]


def build_mcp_tool_series(
    buf: Optional[List[Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Group ``mcp.call.*`` samples per tool for the MCP axis. Success and
    ``.error`` samples fold into the SAME tool (the suffix is stripped for
    grouping and counted as an error). Label-free past this point — only
    duration arrays + error counts leave. Pure read; never raises."""
    rows = buf if buf is not None else samples.recent(limit=1000)
    by_tool: Dict[str, Dict[str, Any]] = {}
    for s in rows:
        name = s.name
        if not name.startswith(mcp_measure.MCP_CALL_PREFIX):
            continue
        is_error = name.endswith(mcp_measure.MCP_ERROR_SUFFIX)
        key = name[: -len(mcp_measure.MCP_ERROR_SUFFIX)] if is_error else name
        t = by_tool.setdefault(key, {"durations": [], "errorCount": 0})
        t["durations"].append(s.duration_ms)
        if is_error:
            t["errorCount"] += 1
    return by_tool


def compute_mcp_tools_score(
    series: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """MCP tool-speed axis, or None while pending (no tool has enough samples).
    Mirrors Node ``computeMcpToolsScore``. Scores the WORSE of the worst scored
    tool's p95 latency (TTI thresholds) and the overall failed-call rate. Pure
    read; never raises. Only counts / durations leave — no tool label."""
    if is_boosthis_disabled():
        return None
    try:
        s = series if series is not None else build_mcp_tool_series()
        tool_count = 0
        sample_count = 0
        error_count = 0
        worst_ms: Optional[int] = None
        for tool in s.values():
            durations = tool.get("durations") or []
            if not isinstance(durations, list) or len(durations) < MCP_TOOLS_MIN_SAMPLES:
                continue
            tool_count += 1
            sample_count += len(durations)
            error_count += max(0, round(tool.get("errorCount", 0) or 0))
            p95 = _mcp_p95(durations)
            if worst_ms is None or p95 > worst_ms:
                worst_ms = p95
        if tool_count == 0 or worst_ms is None:
            return None  # pending — omitted from the upload
        latency_score = linear_score(worst_ms, _MCP_TTI_GOOD, _MCP_TTI_POOR)
        err_rate = (error_count / sample_count) if sample_count > 0 else 0.0
        err_score = linear_score(
            err_rate, MCP_TOOLS_ERR_RATE_GOOD, MCP_TOOLS_ERR_RATE_POOR
        )
        # A slow tool OR a failing tool should turn the tile red → the worse.
        score = min(latency_score, err_score)
        return {
            "score": score,
            "rating": rating_for(score),
            "worstMs": round(worst_ms),
            "toolCount": tool_count,
            "errorPct": round(err_rate * 1000) / 10,
            "sampleCount": sample_count,
        }
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════════
# repeatedWork — the same call, made twice, inside ONE request
# ═══════════════════════════════════════════════════════════════════════════
# "Did this app do the same work twice inside one request?" The rule book has
# always given this advice (n-plus-one-orm-query, python-n-plus-one-query, and
# the fan-out rule: "identical downstream GETs within one request should share
# one promise") and nothing measured it, because a span reaching the server
# carries a route label and a duration but never the call's identity.
# ``repeated_work.py`` counts it INSIDE the kit, where the real call exists,
# and hands this reader nothing but numbers.
#
# Two sub-scores, worse one wins:
#   * WASTED TIME — share of watched request time spent on the redundant
#     occurrences. Good at <=1%, poor at >=20%.
#   * WORST BURST — largest number of identical calls in one request. Good at 1
#     (nothing repeated), poor at >=10 (a classic n+1 fan-out).
REPEATED_WORK_TIME_PCT_GOOD = 1.0
REPEATED_WORK_TIME_PCT_POOR = 20.0
REPEATED_WORK_BURST_GOOD = 1.0
REPEATED_WORK_BURST_POOR = 10.0

# Watched requests needed before the axis leaves "pending" — below this a
# single unlucky request would define the whole reading. Mirrors the Node
# sibling's MIN_SAMPLES_FOR_AXES gate.
REPEATED_WORK_MIN_REQUESTS = 5

# ── AI calls / spend / rate-limit headroom (mirror Node meterAxes.ts) ───────
AI_WORK_MIN_REQUESTS = 5
AI_FAIL_PCT_GOOD = 0.0
AI_FAIL_PCT_POOR = 10.0
AI_TTFT_GOOD_MS = 1000.0
AI_TTFT_POOR_MS = 8000.0
AI_USAGE_MISSING_PCT_GOOD = 0.0
AI_USAGE_MISSING_PCT_POOR = 50.0
AI_PROMPT_REPEAT_GOOD = 1.0
AI_PROMPT_REPEAT_POOR = 4.0
AI_HEADROOM_GOOD_PCT = 40.0
AI_HEADROOM_POOR_PCT = 5.0

# Outside services remain display-only: failure and waiting are separate
# sub-scores because a fast refusal must never read as healthy.
DEP_WAIT_PCT_GOOD = 25.0
DEP_WAIT_PCT_POOR = 75.0
DEP_FAIL_PCT_GOOD = 0.5
DEP_FAIL_PCT_POOR = 10.0
DEPENDENCY_MIN_REQUESTS = 5


def _ai_stats(
    presence_probe: Optional[Callable[[str], bool]] = None,
) -> Dict[str, Any]:
    from boosthis.ai_calls import get_ai_call_stats
    return (
        get_ai_call_stats(presence_probe)
        if presence_probe is not None
        else get_ai_call_stats()
    )


def _read_ai_calls(
    presence_probe: Optional[Callable[[str], bool]] = None,
) -> Optional[Dict[str, Any]]:
    """Node computeAiCalls, byte-compatible field names and omission gates."""
    if is_boosthis_disabled():
        return None
    try:
        # Keep the no-argument production seam stable for callers and tests
        # that replace the stats reader.
        s = _ai_stats(presence_probe) if presence_probe is not None else _ai_stats()
        base = {
            key: max(0, round(float(s.get(key, 0) or 0)))
            for key in (
                "callCount", "watchedRequests", "providerCount", "topProvider",
                "p75Ms", "worstMs", "streamCount", "ttftP75Ms", "stallCount",
                "failCount", "rateLimitedCount", "quotaCount", "timeoutCount",
                "truncatedCount", "filteredCount", "serverMsP75", "serverMsCalls",
                "unclassifiedCalls",
            )
        }
        if "unwatchedClients" in s:
            base["unwatchedClients"] = max(
                0, round(float(s.get("unwatchedClients", 0) or 0))
            )
        # What this app KEEPS doing with its AI calls. Counts the kit
        # remembered on the device — separate runs, separate releases, how
        # long ago, and whether this run has seen it at all. The words are the
        # server's; the kit only ever sends numbers and a shared-vocabulary
        # kind. `patternMemory` is what stops a kit with nowhere to remember
        # from reading as a kit that remembered and found no repeats.
        memory: Dict[str, Any] = {
            "patternMemory": 1 if recurrence_memory_durable() else 0
        }
        if memory["patternMemory"] == 1:
            rows = read_recurrence_rows()
            if rows:
                memory["patterns"] = rows
        common = {"waitPct": 0, **base, **memory}
        # No AI call has EVER been seen: this is not an app with a slow AI
        # layer, it is an app with no AI layer, and the honest answer is
        # silence. An AI-SHAPED call to a host we could not classify is a
        # different thing: it is evidence, and it is the one case where
        # silence would be a lie. Nothing is scored off it -- the axis comes
        # back unmeasurable, carrying the count and nothing else.
        #
        # A THIRD thing breaks the silence, and it is not a reading either:
        # a pattern this device remembers from an earlier run. The release
        # that stops an anti-pattern usually stops it by removing the AI work,
        # so the run that most needs to say "not seen since" is the run with
        # no AI call in it. Staying silent there would throw the memory away
        # at exactly the moment it is worth something. Unscored, unmeasurable,
        # every count a real zero for this run -- Node does the same.
        remembered = bool(memory.get("patterns"))
        if base["callCount"] == 0:
            if (
                base["unclassifiedCalls"] == 0
                and base.get("unwatchedClients", 0) == 0
                and not remembered
            ):
                return None
            return {"score": None, "rating": "pending", "measurable": 0, **common}
        if base["watchedRequests"] < AI_WORK_MIN_REQUESTS:
            if (
                base.get("unwatchedClients", 0) == 0
                and base["unclassifiedCalls"] == 0
                and not remembered
            ):
                return None
            return {"score": None, "rating": "pending", "measurable": 0, **common}
        wall = float(s.get("watchedRequestMs", 0) or 0)
        raw = float(s.get("aiMs", 0) or 0) / wall * 100 if wall > 0 else 0
        common["waitPct"] = max(0, min(100, round(raw * 10) / 10))
        fail_pct = base["failCount"] / base["callCount"] * 100
        terms = [linear_score(fail_pct, AI_FAIL_PCT_GOOD, AI_FAIL_PCT_POOR)]
        if base["streamCount"] > 0 and base["ttftP75Ms"] > 0:
            terms.append(linear_score(base["ttftP75Ms"], AI_TTFT_GOOD_MS, AI_TTFT_POOR_MS))
        score = min(terms)
        return {"score": score, "rating": rating_for(score), "measurable": 1, **common}
    except Exception:  # noqa: BLE001
        return None

def _read_dependency_distance() -> Optional[Dict[str, Any]]:
    """Data Distance — how far away each kind of outside service really is.

    A thin local wrapper around ``dependency_distance.read_dependency_distance``
    for the same reason every other reader on this registry is one: the
    registry is a table of ``("axisKey", reader)`` tuples, and the coverage
    catalogue reads that table's SHAPE out of this file to learn which axes the
    kit attaches. An inline ``__import__`` in the tuple is invisible to it, and
    an invisible attach looks exactly like a meter the kit never took.
    """
    if is_boosthis_disabled():
        return None
    try:
        from boosthis.dependency_distance import read_dependency_distance

        return read_dependency_distance()
    except Exception:  # noqa: BLE001
        return None
def _read_dependencies() -> Optional[Dict[str, Any]]:
    """Mirror Node computeDependencies, omitting shares Python cannot know."""
    if is_boosthis_disabled():
        return None
    try:
        from boosthis.dependency_work import get_dependency_stats
        s = get_dependency_stats()
        in_app_signin = 1 if s.get("inAppSignin") == 1 else 0
        groups = s.get("groups") if isinstance(s.get("groups"), list) else []
        watched = max(0, int(s.get("watchedRequests", 0) or 0))
        calls = max(0, int(s.get("callCount", 0) or 0))
        if watched < DEPENDENCY_MIN_REQUESTS:
            if not in_app_signin:
                return None
            pending = {
                "score": None, "rating": "pending", "measurable": 0,
                "waitPct": 0, "selfPct": 0,
                "callCount": calls, "watchedRequests": watched,
                "failCount": 0, "timeoutCount": 0, "failPct": 0,
                "p75Ms": 0, "worstMs": 0, "kindCount": 0,
                "flakyKinds": 0, "unknownCalls": 0, "signinCalls": 0,
                "signinP75Ms": None, "signinFailed": 0,
                "inAppSignin": in_app_signin, "groups": [],
            }
            if isinstance(s.get("aiMs"), (int, float)) and not isinstance(s.get("aiMs"), bool):
                pending["aiPct"] = 0
            if isinstance(s.get("dbMs"), (int, float)) and not isinstance(s.get("dbMs"), bool):
                pending["dbPct"] = 0
            return pending

        wall = float(s.get("watchedRequestMs", 0) or 0)
        def pct(value: Any) -> Optional[float]:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None
            raw = float(value) / wall * 100 if wall > 0 else 0
            return max(0, min(100, round(raw * 10) / 10))

        wait_pct = pct(s.get("depMs")) or 0
        ai_pct = pct(s.get("aiMs"))
        db_pct = pct(s.get("dbMs"))
        # The app's OWN work is the remainder: whatever the three outside waits
        # did not account for. A share this kit could not measure is left out of
        # the subtraction — which OVERSTATES self time rather than inventing a
        # wait, the only direction an unknown may be resolved in.
        known_shares = (
            wait_pct
            + (ai_pct if ai_pct is not None else 0)
            + (db_pct if db_pct is not None else 0)
        )
        self_pct = max(0, round((100 - known_shares) * 10) / 10)
        fail_count = timeout_count = p75_ms = worst_ms = 0
        flaky_kinds = unknown_calls = signin_calls = signin_failed = 0
        signin_p75_ms = None
        for group in groups:
            if not isinstance(group, dict):
                continue
            failed = int(group.get("failed", 0) or 0)
            timed_out = int(group.get("timedOut", 0) or 0)
            fail_count += failed
            timeout_count += timed_out
            p75_ms = max(p75_ms, int(group.get("p75Ms", 0) or 0))
            worst_ms = max(worst_ms, int(group.get("worstMs", 0) or 0))
            flaky_kinds += 1 if group.get("flaky") == 1 else 0
            if group.get("kind") == "other":
                unknown_calls += int(group.get("calls", 0) or 0)
            if group.get("kind") == "signin":
                signin_calls = int(group.get("calls", 0) or 0)
                signin_p75_ms = int(group.get("p75Ms", 0) or 0)
                signin_failed = failed + timed_out
        fail_pct = max(0, min(100, round(
            ((fail_count + timeout_count) / calls * 100) * 10
        ) / 10)) if calls else 0
        score = min(
            linear_score(wait_pct, DEP_WAIT_PCT_GOOD, DEP_WAIT_PCT_POOR),
            linear_score(fail_pct, DEP_FAIL_PCT_GOOD, DEP_FAIL_PCT_POOR),
        )
        result = {
            "score": score, "rating": rating_for(score), "measurable": 1,
            "waitPct": wait_pct, "selfPct": self_pct,
            "callCount": calls, "watchedRequests": watched,
            "failCount": fail_count, "timeoutCount": timeout_count,
            "failPct": fail_pct, "p75Ms": round(p75_ms), "worstMs": round(worst_ms),
            "kindCount": len(groups), "flakyKinds": flaky_kinds,
            "unknownCalls": unknown_calls, "signinCalls": signin_calls,
            "signinP75Ms": signin_p75_ms, "signinFailed": signin_failed,
            "inAppSignin": in_app_signin, "groups": groups,
        }
        # Absence and zero mean different things. Both shares are borrowed from
        # their live request tallies, and either can be genuinely unknown (an
        # adapter that opens no such tally) — in which case the key is omitted
        # rather than published as a zero.
        if ai_pct is not None:
            result["aiPct"] = ai_pct
        if db_pct is not None:
            result["dbPct"] = db_pct
        return result
    except Exception:  # noqa: BLE001
        return None


def _read_ai_spend() -> Optional[Dict[str, Any]]:
    """Node computeAiSpend."""
    if is_boosthis_disabled():
        return None
    try:
        from boosthis.ai_usage import AI_PRICE_TABLE_DAY
        s = _ai_stats()
        calls = max(0, round(float(s.get("callCount", 0) or 0)))
        if calls == 0:
            return None
        val = lambda key: max(0, round(float(s.get(key, 0) or 0)))
        tokens_in, tokens_out, cached = val("tokensIn"), val("tokensOut"), val("cachedIn")
        missing, watched, cost = val("usageMissingCalls"), val("watchedRequests"), val("costMicros")
        missing_pct = round(missing / calls * 100 * 10) / 10
        shape = {
            "calls": calls, "tokensIn": tokens_in, "tokensOut": tokens_out, "cachedIn": cached,
            "cacheHitPct": max(0, min(100, round(cached / tokens_in * 1000) / 10)) if tokens_in > 0 else None,
            "costMicros": cost, "reportedCostCalls": val("reportedCostCalls"),
            "pricedCalls": val("pricedCalls"), "unpricedCalls": val("unpricedCalls"),
            "declaredCalls": val("declaredCalls"),
            # A SUBSET of unpricedCalls. Every surface subtracts one from the
            # other to tell "we could not price this" from "we chose not to",
            # so a pair that cannot both be true sends that subtraction
            # negative on a real dashboard. Clamped where the numbers leave.
            "declaredUnpricedCalls": min(
                val("unpricedCalls"), val("declaredUnpricedCalls")
            ),
            # Per inbound REQUEST that called AI (never per call, never over
            # all traffic): a request making three calls is charged once, at
            # what the three cost together. Per-call money is cost/pricedCalls,
            # derived where it is shown. Mirrors the Node kit exactly.
            "costPerRequestMicros": round(cost / watched) if watched > 0 else 0,
            "worstRequestCostMicros": val("worstRequestCostMicros"),
            "windowMs": val("windowMs"), "usageMissingCalls": missing,
            "usageMissingPct": missing_pct, "streamUsageMissingCalls": val("streamUsageMissingCalls"),
            "repeatRequests": val("promptRepeatRequests"), "priceTableDay": round(AI_PRICE_TABLE_DAY),
        }
        if watched < AI_WORK_MIN_REQUESTS:
            return {"score": None, "rating": "pending", "measurable": 0, "repeatWorst": None, **shape}
        repeat = max(1, val("promptRepeatWorst"))
        score = min(
            linear_score(missing_pct, AI_USAGE_MISSING_PCT_GOOD, AI_USAGE_MISSING_PCT_POOR),
            linear_score(repeat, AI_PROMPT_REPEAT_GOOD, AI_PROMPT_REPEAT_POOR),
        )
        return {"score": score, "rating": rating_for(score), "measurable": 1, "repeatWorst": repeat, **shape}
    except Exception:  # noqa: BLE001
        return None


def _read_ai_headroom() -> Optional[Dict[str, Any]]:
    """Node computeAiHeadroom."""
    if is_boosthis_disabled():
        return None
    try:
        s = _ai_stats()
        reads = max(0, round(float(s.get("headroomReads", 0) or 0)))
        refusals = max(0, round(float(s.get("rateLimitedCount", 0) or 0)))
        if reads == 0 and refusals == 0:
            return None
        retry = max(0, round(float(s.get("worstRetryAfterMs", 0) or 0)))
        def pct(value: Any) -> Optional[float]:
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                return None
            return max(0, min(100, round(value * 10) / 10))
        req, tok = pct(s.get("worstRequestsPct")), pct(s.get("worstTokensPct"))
        if req is None and tok is None:
            return {"score": None, "rating": "pending", "measurable": 0, "reads": reads,
                    "worstRequestsPct": 0, "worstTokensPct": 0, "refusals": refusals,
                    "worstRetryAfterMs": retry}
        worst = min(req if req is not None else 100, tok if tok is not None else 100)
        score = linear_score(100 - worst, 100 - AI_HEADROOM_GOOD_PCT, 100 - AI_HEADROOM_POOR_PCT)
        return {"score": score, "rating": rating_for(score), "measurable": 1, "reads": reads,
                "worstRequestsPct": req if req is not None else 100,
                "worstTokensPct": tok if tok is not None else 100, "refusals": refusals,
                "worstRetryAfterMs": retry}
    except Exception:  # noqa: BLE001
        return None


def read_repeated_work() -> Optional[Dict[str, Any]]:
    """Repeated-work axis, or None while pending (fewer than
    ``REPEATED_WORK_MIN_REQUESTS`` watched requests). Absent forever if the app
    wraps no identifiable calls — an honest "cannot tell", never a zero. An app
    with real calls and no repeats reports a real zero. Pure read; never
    raises.

    When there is nothing to report AND the app loaded an HTTP client this kit
    cannot watch, the silence has a known cause, so the axis is reported as an
    explicit ``measurable: 0`` can't-tell instead of being omitted — an unseen
    call must never look like a call that was seen and found clean."""
    if is_boosthis_disabled():
        return None
    try:
        from boosthis.repeated_work import get_repeated_work_stats

        stats = get_repeated_work_stats()
        watched = int(stats.get("watchedRequests", 0))
        unwatched = _unwatched_clients()
        if watched < REPEATED_WORK_MIN_REQUESTS:
            if unwatched <= 0:
                return None
            return {
                "score": None,
                "rating": "pending",
                "measurable": 0,
                "unwatchedClients": unwatched,
                "watchedRequests": watched,
            }
        wall = float(stats.get("watchedRequestMs", 0.0))
        redundant = float(stats.get("redundantMs", 0.0))
        pct = (100.0 * redundant / wall) if wall > 0 else 0.0
        repeat_time_pct = max(0.0, min(100.0, round(pct * 10) / 10))
        # A watched request always made at least one call, so the worst burst
        # is at least 1 — "1" is the honest reading for an app that never
        # repeated itself.
        worst = max(1, int(round(float(stats.get("worstRepeats", 0)))))
        score = min(
            linear_score(
                repeat_time_pct,
                REPEATED_WORK_TIME_PCT_GOOD,
                REPEATED_WORK_TIME_PCT_POOR,
            ),
            linear_score(worst, REPEATED_WORK_BURST_GOOD, REPEATED_WORK_BURST_POOR),
        )
        return {
            "score": score,
            "rating": rating_for(score),
            "worstRepeats": worst,
            "requestsWithRepeat": int(stats.get("requestsWithRepeat", 0)),
            "watchedRequests": watched,
            "repeatTimePct": repeat_time_pct,
            # Partial coverage is still partial: an unwatchable client means a
            # repeat could be hiding in calls these numbers never saw.
            "unwatchedClients": unwatched,
        }
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════════
# database work (how much of a request was spent waiting on the DB)
# ═══════════════════════════════════════════════════════════════════════════
# "Of the time this request took, how much was the database?" — and "did one
# request run the same statement over and over?" Both are advice the rule book
# already gives (``python-n-plus-one-query``, ``unbounded-result-set``); neither
# was ever measured here, because a traditional driver talks over its own socket
# and a hosted database over HTTP looked exactly like any other API call.
# :mod:`boosthis.db_work` watches both INSIDE the kit and hands this scorer
# nothing but numbers.
#
# Two sub-scores, worse one wins:
#   * DATABASE WAIT — the share of watched request time spent waiting on the
#     database. Good at <=20% (a normal read-backed endpoint), poor at >=70%
#     (the request is essentially the query). Deliberately far more generous
#     than the repeated-work bands: database time is WORK, not waste.
#   * WORST REPEAT — the largest number of identical statements in one request.
#     Good at 1 (nothing repeated), poor at >=10 (a classic n+1).
#
# The bands are the Node reader's, verbatim, so one app scores the same on
# either runtime. ADDITIVE + DISPLAY-ONLY: never feeds the composite Speed
# score, and never moves the repeated-work axis.
DB_WAIT_PCT_GOOD = 20.0
DB_WAIT_PCT_POOR = 70.0
DB_REPEAT_BURST_GOOD = 1
DB_REPEAT_BURST_POOR = 10
# The point this kit calls a single query's result set UNBOUNDED. A result size
# is reported ONLY once it crosses this line, so the number's presence is the
# judgement and nothing downstream has to invent a threshold of its own.
DB_LARGE_RESULT_ROWS = 1000
# Watched requests needed before the axis leaves "pending". Below this a single
# unlucky request would define the whole reading.
DB_WORK_MIN_REQUESTS = MIN_SAMPLES_FOR_AXES
DB_WAVE_BURST_GOOD = 2
DB_WAVE_BURST_POOR = 10
DB_ROWS_WORST_GOOD = 500
DB_ROWS_WORST_POOR = 10000
DB_ROWS_TYPICAL_GOOD = 100
DB_ROWS_TYPICAL_POOR = 2000
DB_POOL_USED_PCT_GOOD = 60.0
DB_POOL_USED_PCT_POOR = 95.0


def read_db_work() -> Optional[Dict[str, Any]]:
    """Database-work axis, or None when there is nothing honest to say yet.

    THREE OUTCOMES, deliberately distinct — measurability is decided BEFORE any
    score, rating or caption exists:

    * ``None`` — not enough watched requests AND no blind spot. The axis is
      simply absent from the snapshot, which every surface reads as "cannot
      tell". An app with no database and a runtime with no reading look the
      same, and neither looks like a zero.
    * ``measurable: 0`` — too few watched requests to score, but this app
      imported a database library the kit cannot watch. Reporting nothing would
      let a real blind spot pass for a clean app.
    * ``measurable: 1`` — a real reading.

    Pure read; never raises.
    """
    if is_boosthis_disabled():
        return None
    try:
        from boosthis.db_work import get_db_work_stats

        stats = get_db_work_stats()
        unwatched = max(0, int(round(float(stats.get("unwatchedClients", 0) or 0))))
        watched = int(stats.get("watchedRequests", 0) or 0)
        if watched < DB_WORK_MIN_REQUESTS:
            if unwatched <= 0:
                return None
            return {
                "score": None,
                "rating": "pending",
                "measurable": 0,
                "waitPct": 0.0,
                "callCount": int(stats.get("callCount", 0) or 0),
                "watchedRequests": watched,
                "hostedCalls": int(stats.get("hostedCalls", 0) or 0),
                "repeatWorst": None,
                "repeatRequests": int(stats.get("repeatRequests", 0) or 0),
                "repeatWastePct": 0.0,
                "unwatchedClients": unwatched,
            }
        wall = float(stats.get("watchedRequestMs", 0.0) or 0.0)

        def pct_of(ms: Any) -> float:
            raw = (100.0 * float(ms or 0.0) / wall) if wall > 0 else 0.0
            return max(0.0, min(100.0, round(raw * 10) / 10))

        wait_pct = pct_of(stats.get("dbMs"))
        repeat_waste_pct = pct_of(stats.get("repeatMs"))
        # A watched request always made at least one database call, so the worst
        # repeat is at least 1 — "1" is the honest reading for an app that never
        # ran the same statement twice.
        repeat_worst = max(1, int(round(float(stats.get("repeatWorst", 0) or 0))))
        score = min(
            linear_score(wait_pct, DB_WAIT_PCT_GOOD, DB_WAIT_PCT_POOR),
            linear_score(repeat_worst, DB_REPEAT_BURST_GOOD, DB_REPEAT_BURST_POOR),
        )
        rows_worst_raw = int(round(float(stats.get("rowsWorst", 0) or 0)))
        axis = {
            "score": score,
            "rating": rating_for(score),
            "measurable": 1,
            "waitPct": wait_pct,
            "callCount": int(stats.get("callCount", 0) or 0),
            "watchedRequests": watched,
            "hostedCalls": int(stats.get("hostedCalls", 0) or 0),
            "repeatWorst": repeat_worst,
            "repeatRequests": int(stats.get("repeatRequests", 0) or 0),
            "repeatWastePct": repeat_waste_pct,
            # Deliberately outside the score: an unbounded result set is a
            # finding to hand a developer with the rule that caps it, not a
            # number that quietly marks a legitimately large query as poor.
            "unwatchedClients": unwatched,
        }
        if rows_worst_raw >= DB_LARGE_RESULT_ROWS:
            axis["rowsWorst"] = rows_worst_raw
        return axis
    except Exception:  # noqa: BLE001
        return None


def read_db_sequencing() -> Optional[Dict[str, Any]]:
    if is_boosthis_disabled():
        return None
    try:
        from boosthis.db_work import get_db_sequencing_stats

        stats = get_db_sequencing_stats()
        unwatched = max(0, int(stats.get("unwatchedClients", 0) or 0))
        judged = max(0, int(stats.get("waveRequests", 0) or 0))
        if judged < DB_WORK_MIN_REQUESTS:
            if unwatched == 0:
                return None
            return {
                "score": None, "rating": "pending", "measurable": 0,
                "wavesWorst": None, "wavesAvg": 0.0, "seriesPct": 0,
                "watchedRequests": judged, "unwatchedClients": unwatched,
            }
        worst = max(1, int(stats.get("wavesWorst", 0) or 0))
        avg = round((float(stats.get("wavesTotal", 0) or 0) / judged) * 10) / 10
        sum_ms = float(stats.get("sumMs", 0) or 0)
        series = 100
        if sum_ms > 0:
            import math
            series = int(math.floor(
                100 * float(stats.get("spanMs", 0) or 0) / sum_ms + 0.5
            ))
            series = max(0, min(100, series))
        score = linear_score(worst, DB_WAVE_BURST_GOOD, DB_WAVE_BURST_POOR)
        return {
            "score": score, "rating": rating_for(score), "measurable": 1,
            "wavesWorst": worst, "wavesAvg": avg, "seriesPct": series,
            "watchedRequests": judged, "unwatchedClients": unwatched,
        }
    except Exception:  # noqa: BLE001
        return None


def read_db_row_volume() -> Optional[Dict[str, Any]]:
    if is_boosthis_disabled():
        return None
    try:
        from boosthis.db_work import get_db_row_volume_stats

        stats = get_db_row_volume_stats()
        unwatched = max(0, int(stats.get("unwatchedClients", 0) or 0))
        watched = max(0, int(stats.get("watchedRequests", 0) or 0))
        sized = max(0, int(stats.get("rowsCalls", 0) or 0))
        if watched < DB_WORK_MIN_REQUESTS or sized == 0:
            if unwatched == 0:
                return None
            return {
                "score": None, "rating": "pending", "measurable": 0,
                "worstRows": None, "rowsAvg": 0, "sizedCalls": sized,
                "watchedRequests": watched, "unwatchedClients": unwatched,
            }
        import math
        worst = max(0, int(stats.get("rowsWorst", 0) or 0))
        avg = max(0, int(math.floor(
            float(stats.get("rowsSum", 0) or 0) / sized + 0.5
        )))
        score = min(
            linear_score(worst, DB_ROWS_WORST_GOOD, DB_ROWS_WORST_POOR),
            linear_score(avg, DB_ROWS_TYPICAL_GOOD, DB_ROWS_TYPICAL_POOR),
        )
        return {
            "score": score, "rating": rating_for(score), "measurable": 1,
            "worstRows": worst, "rowsAvg": avg, "sizedCalls": sized,
            "watchedRequests": watched, "unwatchedClients": unwatched,
        }
    except Exception:  # noqa: BLE001
        return None


def read_db_pool_pressure() -> Optional[Dict[str, Any]]:
    if is_boosthis_disabled():
        return None
    try:
        from boosthis.db_work import get_db_pool_pressure

        pool = get_db_pool_pressure()
        if pool is None:
            return None
        busy = max(0, int(pool["busy"]))
        idle = max(0, int(pool["idle"]))
        size = max(0, int(pool["size"]))
        used = max(0.0, min(100.0, round(
            ((100.0 * busy / size) if size > 0 else 0.0) * 10
        ) / 10))
        score = linear_score(used, DB_POOL_USED_PCT_GOOD, DB_POOL_USED_PCT_POOR)
        axis = {
            "score": score, "rating": rating_for(score), "usedPct": used,
            "busy": busy, "idle": idle, "size": size,
        }
        # Absence means the pool has no public waiter counter; zero would make
        # that blind spot indistinguishable from a proven empty wait queue.
        if "waiting" in pool:
            axis["waiting"] = max(0, int(pool["waiting"]))
        return axis
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════════
# assembly + lifecycle
# ═══════════════════════════════════════════════════════════════════════════
def read_meter_axes(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble every server-side meter this module owns into a flat dict ready
    to merge into the uploaded ``axes`` map. Warming-up axes are OMITTED (the
    server renders an absent axis as pending); the four scalar confidence keys
    are always present. Pure read; never raises."""
    out: Dict[str, Any] = {}
    try:
        out.update(compute_confidence(summary))
    except Exception:  # noqa: BLE001
        pass
    try:
        from boosthis.request_error_timer_meters import read_axes
        out.update(read_axes())
    except Exception:  # noqa: BLE001
        pass
    for key, reader in (
        ("baseline", compute_baseline),
        ("latencyFloor", compute_latency_floor),
        ("network", read_network),
        ("idle", read_idle),
        ("schedulerLatency", read_scheduler_latency),
        ("worstFreeze", read_worst_freeze),
        ("mcpTools", compute_mcp_tools_score),
        ("repeatedWork", read_repeated_work),
        ("dbWork", read_db_work),
        ("dbSequencing", read_db_sequencing),
        ("dbRowVolume", read_db_row_volume),
        ("dbPoolPressure", read_db_pool_pressure),
        ("aiCalls", _read_ai_calls),
        ("dependencies", _read_dependencies),
        ("dependencyDistance", _read_dependency_distance),
        ("aiSpend", _read_ai_spend),
        ("aiHeadroom", _read_ai_headroom),
        ("backgroundWork", _read_background_work),
        ("liveConnections", _read_live_connections),
    ):
        try:
            axis = reader()  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            axis = None
        if axis:
            out[key] = dict(axis)
    return out


def clear_meter_axes() -> None:
    """Stop the heartbeat + wipe all state (wired into ``forget()`` so nothing
    Boosthis-shaped keeps sampling after erasure). Idempotent. Never raises."""
    global _sched_thread, _worst_freeze_ms, _worst_freeze_samples
    global _net_attempts, _net_completed, _net_failed
    global _net_timeout, _net_stall, _net_worst_ms
    global _idle_busy_cpu_ms, _idle_wall_ms, _idle_windows, _idle_long_tasks
    _sched_stop.set()
    with _sched_lock:
        _sched_samples.clear()
        _sched_thread = None
        _worst_freeze_ms = 0.0
        _worst_freeze_samples = 0
    with _net_lock:
        _net_durations.clear()
        _net_attempts = 0
        _net_completed = 0
        _net_failed = 0
        _net_timeout = 0
        _net_stall = 0
        _net_worst_ms = 0
    with _idle_lock:
        _idle_busy_cpu_ms = 0.0
        _idle_wall_ms = 0.0
        _idle_windows = 0
        _idle_long_tasks = 0
    try:
        from boosthis.held_open import clear_held_open

        clear_held_open()
    except Exception:  # noqa: BLE001
        pass
    # repeatedWork keeps its own module state (per-request scopes + session
    # totals); wipe it through the same lever so erasure really erases.
    try:
        from boosthis.repeated_work import clear_repeated_work

        clear_repeated_work()
    except Exception:  # noqa: BLE001
        pass
    try:
        from boosthis.ai_calls import clear_ai_calls
        clear_ai_calls()
    except Exception:  # noqa: BLE001
        pass
    # dbWork also keeps its own module state — and, unlike the others, a live
    # patch inside the host's database driver. Clearing it detaches that too, so
    # erasure really does stop the feed rather than only forgetting its totals.
    try:
        from boosthis.db_work import clear_db_work
        clear_db_work()
    except Exception:  # noqa: BLE001
        pass
    try:
        from boosthis.dependency_work import clear_dependency_work
        clear_dependency_work()
    except Exception:  # noqa: BLE001
        pass
    try:
        from boosthis.job_work import clear_job_work

        clear_job_work()
    except Exception:  # noqa: BLE001
        pass
    try:
        from boosthis.dependency_distance import clear_dependency_distance
        clear_dependency_distance()
    except Exception:  # noqa: BLE001
        pass
    try:
        from boosthis.live_connections import clear_live_connections
        clear_live_connections()
    except Exception:  # noqa: BLE001
        pass
    try:
        from boosthis.request_error_timer_meters import clear
        clear()
    except Exception:  # noqa: BLE001
        pass


# ── @internal test hooks ────────────────────────────────────────────────────
def _reset_for_tests() -> None:
    clear_meter_axes()
    _sched_stop.clear()


def _push_sched_sample_for_tests(lateness_ms: float) -> None:
    global _worst_freeze_ms, _worst_freeze_samples
    with _sched_lock:
        _sched_samples.append(float(lateness_ms))
        _worst_freeze_samples += 1
        _worst_freeze_ms = max(_worst_freeze_ms, float(lateness_ms))
        if len(_sched_samples) > SCHED_RING_CAP:
            del _sched_samples[0 : len(_sched_samples) - SCHED_RING_CAP]


__all__ = [
    "compute_confidence",
    "compute_baseline",
    "compute_latency_floor",
    "record_network_outcome",
    "read_network",
    "record_idle_window",
    "read_idle",
    "start_scheduler_heartbeat",
    "read_scheduler_latency",
    "build_mcp_tool_series",
    "compute_mcp_tools_score",
    "read_repeated_work",
    "read_db_work",
    "_read_ai_calls", "_read_dependencies",
    "_read_dependency_distance", "_read_live_connections",
    "_read_ai_spend",
    "_read_ai_headroom",
    "compute_background_work",
    "read_meter_axes",
    "clear_meter_axes",
]

def _read_live_connections() -> Optional[Dict[str, Any]]:
    """Live Connections — the streams and sockets this app holds open.

    Wrapped for the same reason as ``_read_dependency_distance`` above.
    """
    if is_boosthis_disabled():
        return None
    try:
        from boosthis.live_connections import read_live_connections

        return read_live_connections()
    except Exception:  # noqa: BLE001
        return None
