"""Repeated-work detector — the same call, made twice, inside one request.

The rule book already tells developers not to redo work inside a single
request (``n-plus-one-orm-query``, ``python-n-plus-one-query``, and the
fan-out rule whose text says "identical downstream GETs within one request
should share one promise"). Nothing MEASURED it: a span that reaches the
server carries a route label, a duration and a rating — never a query, a URL
or an argument — so the server can see that two calls happened but never that
they were the SAME call.

The counting therefore has to happen HERE, where the real call exists, and
only a number may leave.

WHAT IS WATCHED. Every call whose INPUTS the kit can see:

* ``@track_perf("name")``-decorated functions — the name and the arguments are
  both visible at the wrapper.
* outbound HTTP made through the stdlib ``http.client`` chokepoint — which is
  what ``urllib``, ``requests`` and ``urllib3`` all go through. The observation
  is armed on the first request boundary by
  :func:`live_detectors.arm_outbound_http_hook` (reversible; call
  ``disarm_outbound_http_hook()`` to opt out) and yields method + origin + path
  plus a real duration. Hosts registered via ``ignore_host`` — Boosthis's own
  telemetry endpoint — are skipped, so our flushes can never be reported as
  the app repeating itself.
* outbound HTTP made through ``httpx`` or ``aiohttp``, which bypass
  ``http.client`` and open their own sockets. They are watched at their own
  single funnel by :mod:`boosthis.outbound_clients`, armed and disarmed by the
  SAME lever, and yield the same method + origin + path and the same real
  duration — so an identical downstream GET reads the same however the app
  made it.
* any call the app reports itself via :func:`note_outbound_call` with a full
  URL.

Deliberately NOT watched:

* ``with perf("name")`` — no visible inputs, so two ``perf("db.load_user")``
  blocks in one request may be two DIFFERENT users. Calling that a repeat
  would be a guess, and this meter would rather stay quiet.
* the audit-hook socket feed, which sees only a bare hostname — two
  connections to one host are not necessarily the same call. (This is why the
  stdlib HTTP hook above exists: it is the only Python feed that knows a call's
  full identity rather than just where it went.)
* an HTTP client with no Python funnel every request passes through — a thin
  binding over a native library, such as ``pycurl``. Those calls go unobserved
  rather than half-observed. An app that loaded one is NOT reported as an app
  that made no calls: the axis carries a count of the client libraries the kit
  could not watch, and the tile says "cannot tell" instead of showing a clean
  reading built on calls it never saw.

IDENTITY. A call's identity is a per-process hash of a bounded local rendering
of its target plus its arguments. The rendering is built on the stack, hashed
and dropped: the text, the URL and the argument values are never stored, never
logged and NEVER leave this process. Only counts and durations are reported.

REQUEST SCOPE. A request's calls are collected in a scope the ASGI middleware
opens around the app call, carried by a ContextVar (the same mechanism the
trace id already uses). A declined open returns ``None`` and every later call
for that request — including the close — is a no-op, so the pair can never
half-run.

HONEST WHEN BLIND. A kit that identified no calls at all reports NOTHING: the
axis is omitted and the dashboard says "cannot tell". A kit that watched real
calls and found no repeat reports a real zero.

PRIVACY: counts, durations and one non-reversible in-process hash. No route
label, no host, no URL, no argument value. In-memory, bounded, and a no-op
under the kill-switch.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from boosthis.runtime_flags import is_boosthis_disabled

# Most distinct call identities tracked inside ONE request. Past this the scope
# stops learning new identities (repeats of the ones it already knows still
# count), so a pathological request can never grow memory.
MAX_DISTINCT_CALLS = 512
# Most positional/keyword arguments folded into one identity.
MAX_ARGS = 8
# How deep a container argument is walked. Also the cycle guard: a walk that
# cannot recurse forever cannot hang on a self-referencing argument.
MAX_DEPTH = 2
# Most members read from one container.
MAX_MEMBERS = 12
# Longest string folded in verbatim (longer ones fold in a prefix plus their
# length, which still separates two different long values).
MAX_STRING = 64
# Hard cap on the rendered identity string before hashing.
MAX_IDENTITY_CHARS = 512

# Per-request scope: {identity -> [count, total_ms]}. None outside a request.
_current_work: ContextVar[Optional[Dict[int, List[float]]]] = ContextVar(
    "boosthis_repeated_work", default=None
)

_lock = threading.Lock()

# ── Session totals (the only things that ever leave) ────────────────────────
# Requests in which at least one identifiable call was watched.
_watched_requests = 0
# Of those, how many contained the same call more than once.
_requests_with_repeat = 0
# Worst number of identical calls seen inside a SINGLE request.
_worst_repeats = 0
# Wall ms of the watched requests (the share denominator).
_watched_request_ms = 0.0
# Ms attributable to the redundant occurrences, clamped per request.
_redundant_ms = 0.0


# ── Identity (local, non-reversible, never uploaded) ────────────────────────
def _render(value: Any, depth: int) -> str:
    """Bounded, shallow rendering of one argument. Depth- and width-capped so a
    huge or self-referencing argument costs a fixed amount. The result is
    hashed immediately and discarded."""
    if value is None:
        return "z"
    # bool before int — bool IS an int in Python.
    if isinstance(value, bool):
        return f"b:{value}"
    if isinstance(value, (int, float)):
        return f"n:{value!r}"
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            return f"s{len(value)}:{value[:MAX_STRING]}"
        return f"s:{value}"
    if isinstance(value, (bytes, bytearray)):
        return f"y{len(value)}"
    if depth <= 0:
        return "o"
    try:
        if isinstance(value, (list, tuple)):
            head = list(value)[:MAX_MEMBERS]
            inner = ",".join(_render(x, depth - 1) for x in head)
            return f"[{len(value)}:{inner}]"
        if isinstance(value, (set, frozenset)):
            return f"S{len(value)}"
        if isinstance(value, Mapping):
            pairs = sorted(((str(k), v) for k, v in value.items()), key=lambda kv: kv[0])
            inner = ",".join(
                f"{k}={_render(v, depth - 1)}" for k, v in pairs[:MAX_MEMBERS]
            )
            return "{" + inner + "}"
        return "o:" + type(value).__name__
    except Exception:  # noqa: BLE001
        # A throwing property or an exotic object: fall back to a type tag
        # rather than letting instrumentation break the host's call.
        return "o"


def call_identity(
    target: str,
    args: Sequence[Any] = (),
    kwargs: Optional[Mapping[str, Any]] = None,
) -> int:
    """Local identity for one call: its target plus its arguments, hashed.

    PRIVACY: the rendered string exists only on this stack and is never stored,
    logged or transmitted. The returned number is an in-process grouping key —
    it never leaves the kit either (Python's string hash is salted per process,
    so it is not even stable between runs)."""
    try:
        parts = [str(target)]
        shown = 0
        for a in args:
            if shown >= MAX_ARGS:
                break
            parts.append(_render(a, MAX_DEPTH))
            shown += 1
        if kwargs:
            for k in sorted(kwargs.keys()):
                if shown >= MAX_ARGS:
                    break
                parts.append(f"{k}={_render(kwargs[k], MAX_DEPTH)}")
                shown += 1
        total = len(args) + (len(kwargs) if kwargs else 0)
        # An over-long argument list still separates on its length, so a 9-arg
        # call is never confused with an 8-arg one.
        if total > shown:
            parts.append(f"+{total}")
        rendered = "|".join(parts)[:MAX_IDENTITY_CHARS]
        return hash(rendered) & 0xFFFFFFFF
    except Exception:  # noqa: BLE001
        try:
            return hash(str(target)) & 0xFFFFFFFF
        except Exception:  # noqa: BLE001
            return 0


# ── Recording ───────────────────────────────────────────────────────────────
def _file(scope: Dict[int, List[float]], identity: int, duration_ms: float) -> None:
    ms = float(duration_ms) if duration_ms and duration_ms > 0 else 0.0
    with _lock:
        seen = scope.get(identity)
        if seen is not None:
            seen[0] += 1
            seen[1] += ms
            return
        # Bounded: past the cap we stop LEARNING identities but keep counting
        # the ones already known, so a fan-out already spotted stays correct.
        if len(scope) >= MAX_DISTINCT_CALLS:
            return
        scope[identity] = [1, ms]


def note_watched_call(identity: int, duration_ms: float) -> None:
    """Record one watched call against the request currently in scope. Outside
    a request scope this is a no-op: a call we cannot attribute to a request is
    a call we cannot judge for repetition. Never raises."""
    if is_boosthis_disabled():
        return
    try:
        scope = _current_work.get()
        if scope is None:
            return
        _file(scope, identity, duration_ms)
    except Exception:  # noqa: BLE001
        pass  # instrumentation must never disturb the host app.


def _claimed_by_db_meter(target: Any) -> bool:
    """Is this outbound call a hosted database, and therefore the database
    reading's to report?

    Database repetition is reported by the database meter and ONLY there: a
    hosted database left in this tally would have one repeated query counted
    twice — as an app repeating a downstream API call AND as a repeated
    statement. Never raises; an unanswerable target stays here.
    """
    try:
        from boosthis.db_work import is_hosted_db_target

        return is_hosted_db_target(target)
    except Exception:  # noqa: BLE001
        return False


def note_outbound_call(target: Any, duration_ms: float | None = None) -> None:
    """Record one outbound call, identified by its FULL url.

    A bare hostname is refused: two connections to one host are not
    necessarily the same call, and a guess here would invent repeats that never
    happened. Never raises."""
    if is_boosthis_disabled():
        return
    try:
        if not isinstance(target, str) or "://" not in target:
            return
        if _claimed_by_db_meter(target):
            return
        note_watched_call(call_identity(target), duration_ms or 0.0)
    except Exception:  # noqa: BLE001
        pass


# ── Request lifecycle ───────────────────────────────────────────────────────
def begin_request_work() -> Any:
    """Open a request scope, or DECLINE (``None``) under the kill-switch. The
    returned handle is opaque — pass it straight back to
    :func:`end_request_work`. Never raises."""
    if is_boosthis_disabled():
        return None
    try:
        scope: Dict[int, List[float]] = {}
        token = _current_work.set(scope)
        return (scope, token)
    except Exception:  # noqa: BLE001
        return None


def end_request_work(handle: Any, request_ms: float) -> None:
    """Close a request scope and fold it into the session totals.
    ``request_ms`` is the request's own wall time — the denominator for the
    repeat share. A ``None`` handle (declined open) does nothing. Never
    raises."""
    if not handle:
        return
    global _watched_requests, _requests_with_repeat, _worst_repeats
    global _watched_request_ms, _redundant_ms
    try:
        scope, token = handle
        try:
            _current_work.reset(token)
        except Exception:  # noqa: BLE001
            # Reset from a different context (a task that outlived its parent):
            # the scope is still ours to fold, so keep going.
            pass
        with _lock:
            tallies = list(scope.values())
            scope.clear()
        identified = 0
        worst = 0
        redundant = 0.0
        for count, total_ms in tallies:
            n = int(count)
            identified += n
            if n > worst:
                worst = n
            # Mean duration x the redundant occurrences: we keep a total, not a
            # list.
            if n > 1:
                redundant += (total_ms / n) * (n - 1)
        # Nothing identifiable happened in this request — it teaches the meter
        # nothing, so it is not counted as watched. (An app that NEVER
        # identifies a call therefore reports no axis at all: "cannot tell",
        # not a zero.)
        if identified == 0:
            return
        if is_boosthis_disabled():
            return
        wall = float(request_ms) if request_ms and request_ms > 0 else 0.0
        with _lock:
            _watched_requests += 1
            _watched_request_ms += wall
            if worst > _worst_repeats:
                _worst_repeats = worst
            if worst > 1:
                _requests_with_repeat += 1
                # Clamp to this request's own wall time: parallel repeats can
                # add up to more than the request lasted, and a share above
                # 100% is not a fact.
                _redundant_ms += min(redundant, wall)
    except Exception:  # noqa: BLE001
        pass  # instrumentation must never disturb the host app.


# ── Reading ─────────────────────────────────────────────────────────────────
def get_repeated_work_stats() -> Dict[str, float]:
    """Session totals for the ``repeatedWork`` axis. Numbers only."""
    with _lock:
        return {
            "watchedRequests": _watched_requests,
            "requestsWithRepeat": _requests_with_repeat,
            "worstRepeats": _worst_repeats,
            "redundantMs": _redundant_ms,
            "watchedRequestMs": _watched_request_ms,
        }


def clear_repeated_work() -> None:
    """Wipe all state (wired into ``forget()`` so nothing Boosthis-shaped keeps
    counting after erasure). Idempotent; never raises."""
    global _watched_requests, _requests_with_repeat, _worst_repeats
    global _watched_request_ms, _redundant_ms
    with _lock:
        _watched_requests = 0
        _requests_with_repeat = 0
        _worst_repeats = 0
        _watched_request_ms = 0.0
        _redundant_ms = 0.0


__all__ = [
    "call_identity",
    "note_watched_call",
    "note_outbound_call",
    "begin_request_work",
    "end_request_work",
    "get_repeated_work_stats",
    "clear_repeated_work",
]
