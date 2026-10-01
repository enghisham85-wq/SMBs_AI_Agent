"""Privacy-safe per-kind outside-service work.

Only counts, durations, and the six code-defined kind words are retained.
``dbMs`` and ``aiMs`` are both READ from the already-open request tallies of the
database and AI meters, before either close resets its ContextVar — so the
outside-service share divides by one denominator and no wait is measured twice.
Either can still be ``None``: a constituent this kit could not measure makes the
aggregate share unknown rather than the dishonest claim of zero.

A hosted database reached over the web is claimed by the database reader and
DROPPED here — it has its own axis, and grouping it here as well would report
one wait twice.
"""
from __future__ import annotations

import math
import sys
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .dependency_kinds import DEPENDENCY_KINDS, IN_APP_SIGNIN_PACKAGES, dependency_kind_for
from .runtime_flags import is_boosthis_disabled

KIND_RING = 128


@dataclass
class DependencyRequestTally:
    by_kind: Dict[str, int] = field(default_factory=dict)
    count: int = 0
    ms: float = 0.0
    failed: int = 0
    nested_count: int = 0
    nested_failed: int = 0


def _empty_kind() -> Dict[str, Any]:
    return {"calls": 0, "ok": 0, "failed": 0, "timed_out": 0, "requests": 0,
            "ms": [], "ms_at": 0, "worst_ms": 0.0}


_current_dependency: ContextVar[Optional[DependencyRequestTally]] = ContextVar(
    "boosthis_dependency_work", default=None
)
_lock = threading.RLock()
_totals: Dict[str, Dict[str, Any]] = {}
_watched_requests = 0
_watched_request_ms = 0.0
_dep_ms = 0.0
_ai_ms = 0.0
_ai_ms_known = True
_db_ms = 0.0
_db_ms_known = True
_call_count = 0


def begin_dependency_work() -> Any:
    if is_boosthis_disabled():
        return None
    try:
        tally = DependencyRequestTally()
        return tally, _current_dependency.set(tally)
    except Exception:  # noqa: BLE001
        return None


def end_dependency_work(handle: Any, request_ms: float) -> None:
    """Fold one request, BEFORE the AI and database closes.

    Both of those readings are borrowed from their still-open request tallies
    here, so the outside-service share and the self share divide by the same
    denominator. Close either of them first and this borrows a zero — the share
    is then published on numbers that are a guess.
    """
    global _watched_requests, _watched_request_ms, _dep_ms, _ai_ms, _ai_ms_known
    global _db_ms, _db_ms_known, _call_count
    if not handle:
        return
    try:
        tally, token = handle
        try:
            _current_dependency.reset(token)
        except Exception:  # noqa: BLE001
            pass
        # A framework may nest its developer-facing unit inside the transport
        # request (Gradio interaction inside FastAPI). Preserve only the two
        # counts the outer response needs for failure containment; the ordinary
        # dependency totals are still folded exactly once by this inner unit.
        try:
            parent = _current_dependency.get()
            if isinstance(parent, DependencyRequestTally):
                parent.nested_count += tally.count + tally.nested_count
                parent.nested_failed += tally.failed + tally.nested_failed
        except Exception:  # noqa: BLE001
            pass
        if not isinstance(tally, DependencyRequestTally) or tally.count == 0:
            return
        wall = max(0.0, float(request_ms or 0))
        ai: Optional[float] = None
        try:
            from .ai_calls import _current_ai, AiRequestTally
            current_ai = _current_ai.get()
            if isinstance(current_ai, AiRequestTally):
                ai = max(0.0, float(current_ai.ms))
        except Exception:  # noqa: BLE001
            pass
        db: Optional[float] = None
        try:
            from .db_work import current_db_ms
            db = current_db_ms()
        except Exception:  # noqa: BLE001
            db = None
        with _lock:
            _watched_requests += 1
            _watched_request_ms += wall
            _call_count += tally.count
            _dep_ms += min(tally.ms, wall) if wall > 0 else 0
            if ai is None:
                # Some non-ASGI adapters do not open the AI tally. A zero here
                # would claim those requests did no AI work, so one unknown
                # constituent makes the aggregate share unknown.
                _ai_ms_known = False
            else:
                _ai_ms += min(ai, wall) if wall > 0 else 0
            if db is None:
                # The database reader is not running here (no boundary tally, or
                # observation never armed). Same rule as AI: unknown, not zero.
                _db_ms_known = False
            else:
                _db_ms += min(db, wall) if wall > 0 else 0
            for kind in tally.by_kind:
                _totals.setdefault(kind, _empty_kind())["requests"] += 1
            tally.by_kind.clear()
    except Exception:  # noqa: BLE001
        pass


def note_dependency_call(target: Any, duration_ms: float, outcome: str) -> None:
    """Classify and drop ``target``, retaining only coarse numeric aggregates."""
    if is_boosthis_disabled():
        return
    try:
        from .ai_providers import ai_provider_code
        from .db_work import is_hosted_db_target
        from .live_detectors import _ignored_hosts, host_of
        host = host_of(target)
        if not host or host in _ignored_hosts or ai_provider_code(host):
            return
        # A hosted database has its own reading, which owns it outright. Left
        # here as well, one repeated query would be reported twice — as an app
        # waiting on an outside service AND as a repeated statement — and the
        # developer would be sent to fix the same thing in two places. The
        # classification reads the hostname and the path PREFIX only, and
        # nothing about the call leaves this stack either way.
        if is_hosted_db_target(target):
            return
        kind = dependency_kind_for(host)
        del host
        ms = float(duration_ms)
        if not math.isfinite(ms) or ms <= 0:
            ms = 0.0
        coarse = outcome if outcome in ("failed", "timedout") else "ok"
        with _lock:
            totals = _totals.setdefault(kind, _empty_kind())
            totals["calls"] += 1
            totals["timed_out" if coarse == "timedout" else coarse] += 1
            totals["worst_ms"] = max(totals["worst_ms"], ms)
            ring = totals["ms"]
            if len(ring) < KIND_RING:
                ring.append(ms)
            else:
                ring[totals["ms_at"]] = ms
                totals["ms_at"] = (totals["ms_at"] + 1) % KIND_RING
            tally = _current_dependency.get()
            if tally is not None:
                tally.count += 1
                tally.ms += ms
                if coarse != "ok":
                    tally.failed += 1
                tally.by_kind[kind] = tally.by_kind.get(kind, 0) + 1
    except Exception:  # noqa: BLE001
        pass


def in_app_signin_detected() -> bool:
    """Read module-cache presence only; never import or resolve auth code."""
    if is_boosthis_disabled():
        return False
    try:
        return any(name in sys.modules for name in IN_APP_SIGNIN_PACKAGES)
    except Exception:  # noqa: BLE001
        return False


def _p75(values: list[float]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.floor(len(ordered) * 0.75))])


def get_dependency_stats() -> Dict[str, Any]:
    """Return aggregates. ``dbMs`` and ``aiMs`` are real when their readers were
    running and ``None`` otherwise — never a fabricated zero."""
    try:
        with _lock:
            groups = []
            for kind in DEPENDENCY_KINDS:
                t = _totals.get(kind)
                if not t or not t["calls"]:
                    continue
                groups.append({
                    "kind": kind, "calls": t["calls"], "failed": t["failed"],
                    "timedOut": t["timed_out"], "requests": t["requests"],
                    "p75Ms": _p75(t["ms"]), "worstMs": round(t["worst_ms"]),
                    "flaky": 1 if t["ok"] > 0 and t["failed"] + t["timed_out"] > 0 else 0,
                })
            return {
                "watchedRequests": _watched_requests,
                "watchedRequestMs": _watched_request_ms,
                "depMs": _dep_ms,
                "dbMs": _db_ms if _db_ms_known else None,
                "aiMs": _ai_ms if _ai_ms_known else None,
                "callCount": _call_count,
                "groups": groups,
                "inAppSignin": 1 if in_app_signin_detected() else 0,
            }
    except Exception:  # noqa: BLE001
        return {"watchedRequests": 0, "watchedRequestMs": 0, "depMs": 0,
                "dbMs": None, "aiMs": None, "callCount": 0, "groups": [], "inAppSignin": 0}


def clear_dependency_work() -> None:
    global _watched_requests, _watched_request_ms, _dep_ms, _ai_ms, _ai_ms_known
    global _db_ms, _db_ms_known, _call_count
    try:
        with _lock:
            _totals.clear()
            _watched_requests = 0
            _watched_request_ms = 0.0
            _dep_ms = 0.0
            _ai_ms = 0.0
            _ai_ms_known = True
            _db_ms = 0.0
            _db_ms_known = True
            _call_count = 0
    except Exception:  # noqa: BLE001
        pass


__all__ = ["begin_dependency_work", "end_dependency_work", "note_dependency_call",
           "get_dependency_stats", "in_app_signin_detected", "clear_dependency_work"]