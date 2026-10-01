"""Privacy-safe, bounded accounting for ASGI and explicitly reported streams."""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from .health_axes import linear_score, rating_for
from .runtime_flags import is_boosthis_disabled

QUIET_MS = 30_000
CADENCE_GAPS = 5
DEAD_GAP_MULTIPLE = 6
RECONNECT_LINK_MS = 60_000
STORM_WINDOW_MS = 60_000
STORM_THRESHOLD = 5
STORM_BACKOFF_MS = 2_000
MIN_WINDOW_MS = 30_000
RATE_WINDOW_MS = 300_000
LEAK_AGE_MS = 300_000
LEAK_MIN_OPEN = 8
MAX_TRACKED = 500
MAX_GAPS = 64
MAX_ENDPOINTS = 500
MAX_REOPENS = 32

_lock = threading.RLock()
_connections: List["LiveConnection"] = []
_last_end: Dict[int, float] = {}
_reopens: Dict[int, List[float]] = {}
_first_seen = 0.0
_opened = _closed = _drops = _reconnects = _messages = _peak = _storm_peak = 0
_clock = time.monotonic


def _fold(value: Any) -> int:
    h = 0x811C9DC5
    try:
        for ch in str(value if value is not None else ""):
            h ^= ord(ch)
            h = (h * 0x01000193) & 0xFFFFFFFF
    except Exception:
        return 0
    return h


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


class LiveConnection:
    """Opaque handle returned by :func:`begin_live_connection`."""

    def __init__(self, endpoint: int, flow_measurable: bool, opened_at: float) -> None:
        self._endpoint = endpoint
        self._flow_measurable = flow_measurable
        self._opened_at = opened_at
        self._last_seen = opened_at
        self._gaps: List[float] = []
        self._closed_at: Optional[float] = None

    def activity(self, messages: int = 1) -> None:
        """Mark an app write/message boundary. Never accepts or retains content."""
        global _messages
        try:
            at = _clock() * 1000
            with _lock:
                if self._closed_at is not None:
                    return
                gap = at - self._last_seen
                if gap > 0:
                    self._gaps.append(gap)
                    del self._gaps[:-MAX_GAPS]
                self._last_seen = at
                if self._flow_measurable:
                    _messages += max(0, int(messages))
        except Exception:
            pass

    def close(self, clean: bool = True) -> None:
        """End this connection; ``clean=False`` records an unexpected drop."""
        global _closed, _drops
        try:
            at = _clock() * 1000
            with _lock:
                if self._closed_at is not None:
                    return
                self._closed_at = at
                _closed += 1
                if not clean:
                    _drops += 1
                _last_end[self._endpoint] = at
                while len(_last_end) > MAX_ENDPOINTS:
                    _last_end.pop(next(iter(_last_end)))
        except Exception:
            pass


def begin_live_connection(endpoint: Any, flow_measurable: bool = False) -> Optional[LiveConnection]:
    """Begin one held-open connection, retaining only a folded endpoint number."""
    global _first_seen, _opened, _reconnects, _peak, _storm_peak
    if is_boosthis_disabled():
        return None
    try:
        at = _clock() * 1000
        folded = _fold(endpoint)
        with _lock:
            if not _first_seen:
                _first_seen = at
            _opened += 1
            previous = _last_end.get(folded)
            if previous is not None and at - previous <= RECONNECT_LINK_MS:
                _reconnects += 1
                stamps = _reopens.setdefault(folded, [])
                stamps.append(at)
                cutoff = at - STORM_WINDOW_MS
                stamps[:] = [stamp for stamp in stamps if stamp >= cutoff][-MAX_REOPENS:]
                gaps = [stamps[i] - stamps[i - 1] for i in range(1, len(stamps))]
                if len(stamps) >= STORM_THRESHOLD and _median(gaps) < STORM_BACKOFF_MS:
                    _storm_peak = max(_storm_peak, len(stamps))
                while len(_reopens) > MAX_ENDPOINTS:
                    _reopens.pop(next(iter(_reopens)))
            if len(_connections) >= MAX_TRACKED:
                for index, old in enumerate(_connections):
                    if old._closed_at is not None:
                        del _connections[index]
                        break
                else:
                    return None
            conn = LiveConnection(folded, bool(flow_measurable), at)
            _connections.append(conn)
            current = sum(1 for item in _connections if item._closed_at is None)
            _peak = max(_peak, current)
            return conn
    except Exception:
        return None


def _caption(open_count: int, drops: int, named_drop: bool, stalled: int,
             undecided: int, storm: int, never_closed: int, pending_drop: bool) -> str:
    if storm:
        return f"{storm} reconnects in a minute with no widening gap — add back-off"
    if stalled:
        return f"{stalled} open but silent past its own rhythm — likely dead"
    if never_closed:
        return f"{never_closed} connections open for minutes and never closed"
    if undecided:
        return f"{undecided} quiet, and nothing arrives regularly enough to tell dead from resting"
    if named_drop:
        return f"{open_count} open · {drops} dropped without a goodbye"
    if pending_drop:
        return f"{open_count} open · {drops} dropped — too early to say whether that is a normal rate"
    if drops:
        return f"{open_count} open · {drops} brief drops, within the normal rate"
    return f"{open_count} open · none dropped"


def read_live_connections() -> Optional[Dict[str, Any]]:
    """Return the contract wire shape, or ``None`` before the 30-second window."""
    if is_boosthis_disabled():
        return None
    try:
        at = _clock() * 1000
        with _lock:
            if not _first_seen or at - _first_seen < MIN_WINDOW_MS:
                return None
            elapsed = max(0.0, at - _first_seen)
            live = [c for c in _connections if c._closed_at is None]
            ended = [c for c in _connections if c._closed_at is not None]
            quiet = stalled = undecided = 0
            for conn in live:
                silence = at - conn._last_seen
                if silence < QUIET_MS:
                    continue
                quiet += 1
                if len(conn._gaps) < CADENCE_GAPS:
                    undecided += 1
                else:
                    typical = _median(conn._gaps)
                    if typical <= 0:
                        undecided += 1
                    elif silence > typical * DEAD_GAP_MULTIPLE:
                        stalled += 1
            storm = _storm_peak if _storm_peak >= STORM_THRESHOLD else 0
            aged = sum(1 for c in live if at - c._opened_at >= LEAK_AGE_MS)
            never_closed = aged if len(live) >= LEAK_MIN_OPEN and aged >= LEAK_MIN_OPEN else 0
            lives = [(c._closed_at or at) - c._opened_at for c in ended]
            longest = max(((c._closed_at or at) - c._opened_at for c in _connections), default=0)
            opaque = any(not c._flow_measurable for c in _connections)
            drops = _drops
            drops_per_hour = (
                round((drops / (elapsed / 3_600_000)) * 10) / 10
                if elapsed >= RATE_WINDOW_MS else None
            )
            reconnect_rate = (
                round((_reconnects / (elapsed / 3_600_000)) * 10) / 10
                if elapsed >= RATE_WINDOW_MS else None
            )
            named_drop = drops_per_hour is not None and drops > 0 and drops_per_hour > 0.5
            score = None
            if drops_per_hour is not None:
                score = round(
                    .35 * linear_score(drops_per_hour, .5, 12)
                    + .30 * (100 if stalled == 0 else 40 if stalled == 1 else 0)
                    + .20 * (100 if storm == 0 else 0)
                    + .15 * (100 if never_closed == 0 else 0)
                )
            rating = "pending" if score is None else rating_for(score)
            if rating == "good" and (named_drop or stalled or storm or never_closed):
                rating = "needs-work"
            axis: Dict[str, Any] = {
                "score": score, "rating": rating,
                "caption": _caption(len(live), drops, named_drop, stalled, undecided,
                                    storm, never_closed, drops > 0 and drops_per_hour is None),
                "open": len(live), "peakOpen": _peak, "opened": _opened,
                "closed": _closed, "drops": drops, "reconnects": _reconnects,
                "reconnectsPerHour": reconnect_rate,
                "medianLifeMs": round(_median(lives)), "longestMs": round(longest),
                "stormCount": storm, "quiet": quiet, "stalled": stalled,
                "undecided": undecided, "neverClosed": never_closed, "perScreen": 0,
                "flowMeasurable": 0 if opaque else 1, "backlog": 0,
                "windowMin": int(elapsed / 6_000) / 10,
                "measurable": 0 if score is None else 1,
            }
            if not opaque:
                axis["msgsPerMin"] = (
                    round((_messages / (elapsed / 60_000)) * 10) / 10
                    if elapsed >= 5_000 else None
                )
            return axis
    except Exception:
        return None


async def watch_asgi(scope: Any, receive: Any, send: Any, app: Any) -> None:
    """Observe WebSocket and SSE at the ASGI protocol boundary."""
    conn: Optional[LiveConnection] = None
    accepted = False
    content_type = ""
    endpoint = scope.get("path", "") if isinstance(scope, dict) else ""

    async def watched_receive() -> Any:
        nonlocal conn
        message = await receive()
        try:
            if scope.get("type") == "websocket" and message.get("type") == "websocket.disconnect":
                if conn:
                    conn.close(clean=message.get("code", 1000) in (1000, 1001))
        except Exception:
            pass
        return message

    async def watched_send(message: Any) -> None:
        nonlocal conn, accepted, content_type
        try:
            kind = message.get("type")
            if kind == "websocket.accept" and not accepted:
                accepted = True
                conn = begin_live_connection(endpoint, flow_measurable=True)
            elif kind in ("websocket.send",) and conn:
                conn.activity()
            elif kind == "websocket.close" and conn:
                conn.close(clean=True)
            elif kind == "http.response.start":
                for name, value in message.get("headers", []):
                    if bytes(name).lower() == b"content-type":
                        content_type = bytes(value).decode("latin-1", "ignore").lower()
            elif kind == "http.response.body" and "text/event-stream" in content_type:
                if conn is None and message.get("more_body"):
                    conn = begin_live_connection(endpoint, flow_measurable=True)
                if conn and message.get("body"):
                    conn.activity()
                if conn and not message.get("more_body", False):
                    conn.close(clean=True)
        except Exception:
            pass
        await send(message)

    try:
        await app(scope, watched_receive, watched_send)
    finally:
        try:
            if conn and conn._closed_at is None:
                conn.close(clean=False if scope.get("type") == "websocket" else True)
        except Exception:
            pass


def clear_live_connections() -> None:
    global _first_seen, _opened, _closed, _drops, _reconnects, _messages, _peak, _storm_peak
    try:
        with _lock:
            _connections.clear()
            _last_end.clear()
            _reopens.clear()
            _first_seen = 0
            _opened = _closed = _drops = _reconnects = _messages = _peak = _storm_peak = 0
    except Exception:
        pass


def _set_clock_for_tests(clock: Any) -> None:
    global _clock
    _clock = clock or time.monotonic
