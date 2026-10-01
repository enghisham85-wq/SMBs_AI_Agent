"""Request-path, unseen-error and timer readings shared by every Python host.

The framework adapters feed only facts from their real boundary.  Scoring and
warm-up rules live here so FastAPI, Starlette, Flask, Django, Streamlit and
Gradio cannot acquire subtly different meanings for the same wire key.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Any, Dict, Optional, Sequence, Tuple

from .health_axes import linear_score, rating_for
from .runtime_flags import is_boosthis_disabled

# The kit's existing closed "blocked by the environment" reason code.
REASON_BLOCKED_BY_ENVIRONMENT = 8

_lock = threading.RLock()
_started_wall = time.time()
_started_mono = time.monotonic()
_route_parts: set[str] = set()
_route_observed = _route_failed = _route_untracked = 0
_contained = _collapsed = _all_failed = _unattributed = _watched_data = 0
_unhandled = 0
_connect_attempts = _connects = 0
_connection_unknown = 0
_cache_checked = _cache_hits = _cache_misses = _cache_stale = _cache_bypass = 0
_timer_samples: list[Tuple[float, int]] = []
_hooks_installed = False
_hooks_active = False
_old_excepthook: Any = None
_old_thread_excepthook: Any = None

_CACHE_HEADERS = (
    "x-vercel-cache", "cf-cache-status", "x-nextjs-cache",
    "cache-status", "x-cache",
)


def _pending(**fields: Any) -> Dict[str, Any]:
    return {"score": None, "rating": "pending", **fields}


def _unmeasurable(**fields: Any) -> Dict[str, Any]:
    return _pending(
        measurable=0, reasonCode=REASON_BLOCKED_BY_ENVIRONMENT, **fields
    )


def install_hooks() -> None:
    """Install chain-preserving last-resort exception hooks, once."""
    global _hooks_installed, _hooks_active, _old_excepthook, _old_thread_excepthook
    if is_boosthis_disabled():
        return
    if _hooks_installed:
        _hooks_active = True
        return
    with _lock:
        if _hooks_installed:
            return
        _old_excepthook = sys.excepthook

        def process_hook(exc_type: Any, exc: Any, tb: Any) -> None:
            global _unhandled
            if _hooks_active:
                with _lock:
                    _unhandled += 1
            _old_excepthook(exc_type, exc, tb)

        sys.excepthook = process_hook
        if hasattr(threading, "excepthook"):
            _old_thread_excepthook = threading.excepthook

            def thread_hook(args: Any) -> None:
                global _unhandled
                if _hooks_active:
                    with _lock:
                        _unhandled += 1
                _old_thread_excepthook(args)

            threading.excepthook = thread_hook
        _hooks_installed = True
        _hooks_active = True


def note_response(
    label: str,
    status: Optional[int],
    dependency_handle: Any = None,
) -> None:
    """File one framework response and its request-local dependency outcome."""
    global _route_observed, _route_failed
    global _contained, _collapsed, _all_failed, _unattributed, _watched_data
    if is_boosthis_disabled() or not isinstance(status, int):
        return
    try:
        tally = dependency_handle[0] if dependency_handle else None
        calls = max(0, int(getattr(tally, "count", 0) or 0)) + max(
            0, int(getattr(tally, "nested_count", 0) or 0)
        )
        failed_calls = max(0, int(getattr(tally, "failed", 0) or 0)) + max(
            0, int(getattr(tally, "nested_failed", 0) or 0)
        )
        with _lock:
            if len(_route_parts) < 256 or label in _route_parts:
                _route_parts.add(str(label or "request"))
                _route_observed += 1
                if status >= 500:
                    _route_failed += 1
            else:
                global _route_untracked
                _route_untracked += 1
            if calls > 0:
                _watched_data += 1
                if failed_calls > 0:
                    if status >= 500 and failed_calls >= calls:
                        _all_failed += 1
                    elif status >= 500:
                        _collapsed += 1
                    else:
                        _contained += 1
                elif status >= 500:
                    _unattributed += 1
    except Exception:
        pass


def note_outbound_connection(completed: bool, opened: bool) -> None:
    global _connect_attempts, _connects
    if not completed or is_boosthis_disabled():
        return
    with _lock:
        _connect_attempts += 1
        if opened:
            _connects += 1


def note_connection_unavailable() -> None:
    """Record a completed client call whose pool hides new-vs-reused."""
    global _connection_unknown
    with _lock:
        _connection_unknown += 1


def note_upstream_cache(headers: Any) -> None:
    """Read one dependency-declared cache verdict; retain no header value."""
    global _cache_checked, _cache_hits, _cache_misses, _cache_stale, _cache_bypass
    try:
        pairs = headers.items() if hasattr(headers, "items") else (headers or ())
        values = {
            str(k).lower(): str(v).strip().lower().split(",", 1)[0]
            for k, v in pairs
        }
        verdict: Optional[str] = None
        for name in _CACHE_HEADERS:
            value = values.get(name)
            if value is None:
                continue
            upper = value.upper()
            if name == "cache-status":
                params = [part.strip().lower() for part in value.split(",", 1)[0].split(";")[1:]]
                if "hit" in params:
                    verdict = "hit"
                else:
                    forwarded = next(
                        (p[4:] for p in params if p.startswith("fwd=")), ""
                    )
                    verdict = {
                        "stale": "stale", "request": "stale",
                        "bypass": "bypass", "method": "bypass",
                        "uri-miss": "miss", "vary-miss": "miss", "miss": "miss",
                    }.get(forwarded)
            elif name == "x-cache":
                verdict = {
                    "HIT": "hit", "MISS": "miss", "REFRESHHIT": "stale",
                    "ERROR": "bypass",
                }.get(upper.split()[0] if upper.split() else "")
            elif name == "x-vercel-cache":
                verdict = {
                    "HIT": "hit", "PRERENDER": "hit", "STALE": "stale",
                    "REVALIDATED": "stale", "MISS": "miss", "BYPASS": "bypass",
                }.get(upper)
            elif name == "cf-cache-status":
                verdict = {
                    "HIT": "hit", "MISS": "miss", "EXPIRED": "stale",
                    "STALE": "stale", "UPDATING": "stale",
                    "REVALIDATED": "stale", "BYPASS": "bypass",
                    "DYNAMIC": "bypass", "IGNORED": "bypass",
                }.get(upper)
            else:
                verdict = {"HIT": "hit", "MISS": "miss", "STALE": "stale"}.get(upper)
            if verdict:
                break
        with _lock:
            if verdict == "hit":
                _cache_checked += 1
                _cache_hits += 1
            elif verdict == "miss":
                _cache_checked += 1
                _cache_misses += 1
            elif verdict == "stale":
                _cache_checked += 1
                _cache_stale += 1
            elif verdict == "bypass":
                _cache_checked += 1
                _cache_bypass += 1
    except Exception:
        pass


def sample_timers() -> None:
    """Sample Python ``threading.Timer`` objects; disclose other timer systems."""
    try:
        now = time.monotonic()
        timers = sum(
            1 for thread in threading.enumerate()
            if isinstance(thread, threading.Timer) and thread.is_alive()
        )
        with _lock:
            _timer_samples.append((now, timers))
            # At the 250ms heartbeat cadence a 60-second span needs 241 points
            # (first-to-last), so the ring must be comfortably larger than 240.
            if len(_timer_samples) > 500:
                del _timer_samples[:-500]
    except Exception:
        pass


def _rate_axis(count: int) -> Dict[str, Any]:
    elapsed = max(0.0, time.monotonic() - _started_mono)
    window = elapsed / 60.0
    base = {"count": count, "windowMin": round(window, 2)}
    if elapsed < 300:
        return _pending(**base)
    per_hour = round((count / window) * 60.0, 1) if window > 0 else 0.0
    score = linear_score(per_hour, 0, 5)
    return {**base, "perHour": per_hour, "score": score, "rating": rating_for(score)}


def read_axes() -> Dict[str, Dict[str, Any]]:
    with _lock:
        parts, observed, failed, untracked = (
            len(_route_parts), _route_observed, _route_failed, _route_untracked
        )
        contained, collapsed, all_failed, unattributed, watched = (
            _contained, _collapsed, _all_failed, _unattributed, _watched_data
        )
        attempts, connects, connection_unknown = (
            _connect_attempts, _connects, _connection_unknown
        )
        checked, hits, misses, stale, bypass = (
            _cache_checked, _cache_hits, _cache_misses, _cache_stale, _cache_bypass
        )
        unhandled = _unhandled
        timer_samples = list(_timer_samples)

    out: Dict[str, Dict[str, Any]] = {
        # Python's response APIs expose no "write buffer was full" signal.
        "backpressure": _unmeasurable(total=observed),
        # A normal Python process has no platform invocation deadline.
        "timeoutHeadroom": _unmeasurable(hostKind="python-process", runs=observed),
        # CPython intentionally keeps no all-raised-exceptions counter.  A trace
        # hook could manufacture one, but would put Python callbacks on every
        # executed line; sys.excepthook only counts the separate unseen fate.
        "exceptionChurn": _unmeasurable(thrown=None, perMin=None, windowMin=round(
            max(0.0, time.monotonic() - _started_mono) / 60.0, 2
        )),
        "unhandledErrors": _rate_axis(unhandled),
    }
    if parts > 0 or untracked > 0:
        out["routeFailures"] = {
            "score": None, "rating": "not-scored", "parts": parts,
            "observed": observed, "failed": failed, "untracked": untracked,
        }
    if watched > 0:
        failures = contained + collapsed
        base = {
            "measurable": 1, "collapsedCount": collapsed,
            "containedCount": contained, "allFailedCount": all_failed,
            "unattributedCount": unattributed, "watchedRequests": watched,
            "collapsePct": None,
        }
        if failures < 3:
            out["failureContainment"] = _pending(
                **base, reasonCode=REASON_BLOCKED_BY_ENVIRONMENT
            )
        else:
            pct = round(collapsed / failures * 1000) / 10
            score = linear_score(pct, 5, 50)
            out["failureContainment"] = {
                **base, "collapsePct": pct, "score": score,
                "rating": rating_for(score),
            }
    conn = {"connectCount": connects, "attemptCount": attempts}
    if connection_unknown > 0:
        out["connectionSetup"] = _unmeasurable(**conn)
    elif attempts < 20:
        out["connectionSetup"] = _pending(**conn)
    else:
        pct = round(connects / attempts * 1000) / 10
        score = linear_score(pct, 10, 60)
        out["connectionSetup"] = {
            **conn, "reconnectPct": pct, "score": score, "rating": rating_for(score)
        }
    cache = {
        "checked": checked, "hits": hits, "misses": misses,
        "stale": stale, "bypass": bypass,
    }
    declared = hits + misses + stale
    if checked > 0:
        if declared < 5:
            out["upstreamCache"] = _pending(**cache)
        else:
            pct = round((hits + stale) / declared * 1000) / 10
            score = linear_score(100 - pct, 10, 60)
            out["upstreamCache"] = {
                **cache, "hitPct": pct, "score": score, "rating": rating_for(score)
            }
    observed_min = max(0.0, (time.time() - _started_wall) / 60.0)
    uptime_min = max(0.0, (time.monotonic() - _started_mono) / 60.0)
    stability = {
        "uptimeMin": round(uptime_min, 2), "observedMin": round(observed_min, 2),
        "caption": "process clock drift",
    }
    if observed_min < 5:
        out["uptimeStability"] = _pending(**stability)
    else:
        drift = abs(observed_min - uptime_min) / observed_min * 100
        score = linear_score(drift, 1, 10)
        out["uptimeStability"] = {
            **stability, "driftPct": round(drift, 2),
            "score": score, "rating": rating_for(score),
        }
    timer_base = {
        "timers": timer_samples[-1][1] if timer_samples else None,
        "growthPerMin": None, "sampleCount": len(timer_samples),
        # Loaded timer systems outside threading.Timer are counted, not assumed.
        "unwatchedTimers": sum(
            1 for name in ("asyncio", "sched", "signal") if name in sys.modules
        ),
    }
    if len(timer_samples) < 12 or timer_samples[-1][0] - timer_samples[0][0] < 60:
        out["timerHealth"] = _pending(**timer_base)
    else:
        xs = [(t - timer_samples[0][0]) / 60 for t, _ in timer_samples]
        ys = [n for _, n in timer_samples]
        mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
        denom = sum((x - mean_x) ** 2 for x in xs)
        growth = max(0.0, sum(
            (x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)
        ) / denom) if denom > 0 else 0.0
        growth = round(growth, 1)
        score = linear_score(growth, 1, 30)
        out["timerHealth"] = {
            **timer_base, "growthPerMin": growth,
            "score": score, "rating": rating_for(score),
        }
    return out


def clear() -> None:
    global _route_observed, _route_failed, _route_untracked
    global _contained, _collapsed, _all_failed, _unattributed, _watched_data
    global _unhandled, _connect_attempts, _connects, _connection_unknown
    global _cache_checked, _cache_hits, _cache_misses, _cache_stale, _cache_bypass
    global _hooks_active, _started_wall, _started_mono
    with _lock:
        _route_parts.clear()
        _route_observed = _route_failed = _route_untracked = 0
        _contained = _collapsed = _all_failed = _unattributed = _watched_data = 0
        _unhandled = _connect_attempts = _connects = _connection_unknown = 0
        _cache_checked = _cache_hits = _cache_misses = _cache_stale = _cache_bypass = 0
        _timer_samples.clear()
        _started_wall = time.time()
        _started_mono = time.monotonic()
        # Leave the chain-preserving wrappers in place so another observer
        # installed above us is never disrupted.  Erasure stops their feed;
        # the next mount reactivates the same wrappers instead of stacking.
        _hooks_active = False


def drop_axis_state(key: str) -> bool:
    """Drop only the framework-owned reading named by ``key``."""
    global _route_observed, _route_failed, _route_untracked
    global _contained, _collapsed, _all_failed, _unattributed, _watched_data
    with _lock:
        if key == "routeFailures":
            _route_parts.clear()
            _route_observed = _route_failed = _route_untracked = 0
            return True
        if key == "failureContainment":
            _contained = _collapsed = _all_failed = _unattributed = _watched_data = 0
            return True
    return False