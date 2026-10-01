"""Boosthis: outbound HTTP made through httpx and aiohttp (Python).

WHY THIS EXISTS. The kit learns about a service's outbound calls from ONE
place: the stdlib ``http.client`` chokepoint, which ``urllib``, ``requests``
and ``urllib3`` all go through. The two most popular modern async clients —
``httpx`` and ``aiohttp`` — bypass it entirely and open their own sockets. A
modern async FastAPI service that uses ``httpx`` (the pairing FastAPI's own
docs recommend) therefore showed an EMPTY outbound-call tile and an EMPTY
repeated-work tile: exactly what an app that makes no calls at all looks like.

This module closes that gap by watching those two clients at their own single
funnel, and feeds the SAME two meters the stdlib hook already feeds — the
``network`` axis (durations + coarse outcome buckets) and the repeated-work
axis (identical calls inside one request). Nothing new is uploaded.

WHERE IT WATCHES, AND WHY THERE.

* ``httpx`` — ``HTTPTransport.handle_request`` and
  ``AsyncHTTPTransport.handle_async_request``. The transport is the layer that
  actually performs one HTTP round trip, so each redirect hop is its own call
  (which is what an app's cost really is), and a caller who passes a custom or
  mock transport is correctly NOT observed: no request left the process.
* ``aiohttp`` — the connector/response pair UNDERNEATH ``ClientSession
  ._request``, because aiohttp runs its redirect loop inside that method: the
  obvious single funnel would file a four-hop redirect chain as one call taking
  as long as four.

WHAT IS NEVER DONE.

* A library is patched only if the app ALREADY imported it. This kit never
  imports an HTTP client the host does not use.
* Calls to hosts registered via ``live_detectors.ignore_host`` — Boosthis's own
  telemetry endpoint — are skipped, so our own uploads can never appear as the
  customer's traffic.
* The observation is reversible: ``disarm_outbound_http_hook()`` restores every
  patched method and records the opt-out, and the wrappers no-op even if the
  restore itself fails.
* A method is restored only while OUR wrapper is still the one in place. If a
  host library patched on top of us since, putting back what we captured at
  arming time would silently delete THEIR work, so our wrapper stays where it
  is instead — inert, because the feed is switched off first.
* A failure inside this module can never reach the host: every observation is
  wrapped, the real call always runs, and the host's own exception is re-raised
  untouched.

PRIVACY. Identical to the stdlib hook: a bounded local rendering of the call
target is hashed inside this process for the repeat count and dropped; the
network meter gets a duration and one of ``ok | error | timeout | stall``.
A host, URL, query, status code or credential never leaves the process — and
because the target is composed from ``urlsplit().hostname``, userinfo embedded
in a URL never reaches even the in-process hash.

HONESTY. A client this kit cannot watch must not read as an app making no
calls. :func:`outbound_watch_report` counts how many known outbound-HTTP client
libraries this process loaded and how many of them are actually being watched,
so both tiles can say "cannot tell" instead of "no calls". Counts only — a
library NAME never leaves the process.
"""

from __future__ import annotations

import sys
import threading
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional, Tuple

from .runtime_flags import is_boosthis_disabled
from .trace_propagation import headers_for_next_hop, host_of, should_propagate_to

# Client libraries this kit knows how to watch, in the order they are armed.
WATCHABLE_CLIENTS: Tuple[str, ...] = ("httpx", "aiohttp")

# Client libraries this kit KNOWS about but has no safe Python-level funnel for.
# Listing them is the honest half of the deal: an app that loaded one of these
# has outbound calls the meters cannot see, and the tiles must say so rather
# than report an empty picture. ``pycurl`` is a thin binding over libcurl —
# there is no Python call site that every request passes through.
UNWATCHABLE_CLIENTS: Tuple[str, ...] = ("pycurl",)

# The stdlib chokepoint, watched by live_detectors' own hook. Counted in the
# same report so "watching is on but the stdlib patch failed" is visible too.
_STDLIB_CLIENT = "http.client"

# library -> our observation is currently in place
_installed: Dict[str, bool] = {}
# library -> we tried to patch it and could not (never retried: a structural
# miss means the library moved, and retrying it every request would only burn
# time on every single call the app makes)
_failed: Dict[str, bool] = {}
# One entry per method we replaced: (library, owner, attribute, original,
# the wrapper we installed). The WRAPPER is kept so a restore can check it is
# still the callable in place before putting the original back — see
# unpatch_client_hooks.
_patches: List[Tuple[str, Any, str, Any, Any]] = []

# The request boundary arms this, so two first requests arriving together both
# reach the installer. Installing twice would capture OUR OWN wrapper as "the
# original": every call counted twice, and a patch that can never be undone.
_client_lock = threading.Lock()

# Whether the kit has ever ATTEMPTED to arm client observation. Before that, it
# claims nothing and the honesty report stays silent — an axis that is simply
# not started yet is not a blind spot.
_arm_attempted = False
# Live switch: a disarm that cannot restore a method still stops the feed.
_active = False


# ═══════════════════════════════════════════════════════════════════════════
# filing one observed call
# ═══════════════════════════════════════════════════════════════════════════
def _db_scope_now() -> Any:
    """The in-flight request's database tally, read on the ISSUING stack.

    Captured where the clock starts and carried to :func:`_file_call`, because
    a pooled or keep-alive connection can answer long after the request that
    issued the call has ended. Never raises.
    """
    try:
        from boosthis.db_work import current_db_scope

        return current_db_scope()
    except Exception:  # noqa: BLE001
        return None


def _file_call(
    target: Optional[str],
    started: float,
    exc: Optional[BaseException],
    ai_claimed: bool = False,
    db_scope: Any = None,
) -> None:
    """File one finished outbound call with both meters. Never raises.

    Mirrors the stdlib hook exactly, including the two rules that keep the
    network tile honest: every call that ENDED banks its duration whatever the
    outcome (a call that failed five times and then worked cost the app all six
    attempts, and reporting only the success hid that), and a loud failure that
    took longer than the stall horizon is reported as the silent-drop class it
    really was.

    ``db_scope`` is the request tally captured when the call was ISSUED. A
    hosted database is filed against it and then refused by the repeated-work
    and dependency meters, so one query is reported by exactly one reading.
    """
    try:
        if not target or not _active or is_boosthis_disabled():
            return
        try:
            from boosthis.request_error_timer_meters import note_connection_unavailable
            note_connection_unavailable()
        except Exception:  # noqa: BLE001
            pass
        from boosthis.live_detectors import (
            _net_outcome_for_error,
            record_outbound_attempt,
        )
        from boosthis.meter_axes import NETWORK_STALL_MS

        elapsed_ms = (perf_counter() - started) * 1000.0
        # httpx's public transport hook and aiohttp's connector/response hooks
        # expose either a whole round trip or pool acquisition, not TCP and TLS
        # setup for NEW connections separately. Record the honest blind spot
        # only once a real call used one of those clients; never substitute the
        # round-trip duration (a slow service is not necessarily a distant one).
        try:
            from boosthis.dependency_distance import note_dependency_distance_unavailable
            note_dependency_distance_unavailable()
        except Exception:  # noqa: BLE001
            pass
        if not ai_claimed:
            # Hosted database: file it against the issuing request. The two
            # meters below drop it at their own doors; the network meter keeps
            # it, because that measures the wire and the wire is the same.
            try:
                from boosthis.db_work import note_hosted_db_call

                note_hosted_db_call(db_scope, target, elapsed_ms)
            except Exception:  # noqa: BLE001
                pass
        if ai_claimed:
            # Known AI calls are claimed by the dedicated prompt-aware meter,
            # not repeated_work (all prompts share one provider endpoint).
            from boosthis.meter_axes import record_network_outcome
            if exc is None:
                record_network_outcome(elapsed_ms, "ok")
            else:
                outcome = _net_outcome_for_error(exc)
                if outcome != "timeout" and elapsed_ms >= NETWORK_STALL_MS:
                    outcome = "stall"
                record_network_outcome(elapsed_ms, outcome)
            return
        if exc is None:
            from boosthis.dependency_work import note_dependency_call
            note_dependency_call(target, elapsed_ms, "ok")
            record_outbound_attempt(target, elapsed_ms, "ok")
            return
        outcome = _net_outcome_for_error(exc)
        if outcome != "timeout" and elapsed_ms >= NETWORK_STALL_MS:
            outcome = "stall"
        from boosthis.dependency_work import note_dependency_call
        note_dependency_call(
            target, elapsed_ms, "timedout" if outcome == "timeout" else "failed"
        )
        record_outbound_attempt(target, elapsed_ms, outcome)
    except Exception:  # noqa: BLE001
        pass  # observation must never disturb the host


def _target(method: Any, url: Any) -> Optional[str]:
    """Compose the shared call target, or None when the call is not ours to
    watch (no host, or a host this kit ignores). Never raises.

    Both clients are watched at a layer where the URL is already absolute, so
    there is nothing to join here. That is not an accident of where the hooks
    landed: an app can open ``ClientSession(base_url=…)`` and then call
    ``session.get("/x")``, and a hook placed above the resolution would see a
    path with no host at all and drop every one of those calls.
    """
    try:
        if not _active or is_boosthis_disabled():
            return None
        from boosthis.live_detectors import outbound_target_from_url

        return outbound_target_from_url(method, str(url) if url is not None else "")
    except Exception:  # noqa: BLE001
        return None


def _attach_trace_headers(
    headers: Any, method: Any, url: Any
) -> Tuple[str, ...]:
    """FORWARD THIS HOP'S TRACE TO THE NEXT SERVICE, on a client's own header map.

    The httpx/aiohttp sibling of ``live_detectors._attach_trace_headers``: the
    kit is already standing inside the outgoing call to measure it, and this is
    what makes a two-service install produce ONE trace rather than two.

    ``_target`` is reused as the gate rather than re-deriving one, so this
    refuses in exactly the cases the measurement refuses in — the hook is not
    active, the kill-switch is on, there is no host, or the destination is
    Boosthis's own upload endpoint (our flushes are not the customer's next hop
    and tagging them would put a customer's trace id on our own wire). What is
    left is judged by the customer's propagation setting, whose default permits
    only destinations that cannot be an outside company.

    A CONNECT IS NOT THE CUSTOMER'S REQUEST, and is refused before the policy
    is asked anything. aiohttp opens an HTTPS tunnel by building an ordinary
    ``ClientRequest``, setting its method to CONNECT and its url to the
    DESTINATION, then sending it down the socket to the PROXY — through this
    hook. Judged on that url the policy answers about the far side and permits
    it, so the headers land on the proxy operator: the one recipient a tunnel
    exists to keep them from, since everything after the CONNECT is encrypted.
    The application request that follows travels inside the tunnel and is
    judged normally, so the trace still crosses; only the control request goes
    bare. httpx cannot reach here with one — httpcore builds its CONNECT below
    the transport this hook wraps — but the refusal is written once, for both,
    where the method is already in hand.

    A header the caller already set is left exactly as it is — they may be
    forwarding a trace from somewhere the kit cannot see, and it is just as
    likely to be THIS trace, forwarded by hand with our own header builder.

    RETURNS THE NAMES IT WROTE, so the caller can put the header map back the
    way it found it once the request is on the wire. See
    :func:`_restore_trace_headers` for why that matters.

    CANNOT BREAK THE CALL, AND CANNOT HALF-CHANGE IT. Every value is built and
    checked before the first is written — but the header map belongs to the
    host's client and pre-validation makes a raise unlikely, not impossible. A
    raise on the second write would otherwise send the request carrying part of
    the set, so a part-finished loop takes back exactly the names it had
    already written. Whatever happens, the request that goes out is either
    exactly what the host built or exactly that plus the whole set, and nothing
    surfaces: at worst the trace stops at this service.
    """
    written: list[str] = []
    try:
        if headers is None or _target(method, url) is None:
            return ()
        # A CONNECT is addressed to the proxy, whatever its url says. See the
        # paragraph above: judged on the far side it reads as permitted, and
        # the headers would reach the one recipient the tunnel is there to
        # keep them from.
        if method is not None and str(method).upper() == "CONNECT":
            return ()
        host = host_of(str(url) if url is not None else "")
        if not should_propagate_to(host):
            return ()
        existing = [str(name).lower() for name in headers.keys()]
        items = headers_for_next_hop(host, existing)
        try:
            for name, value in items:
                headers[name] = value
                written.append(name)
        except Exception:  # noqa: BLE001
            # ALL OF THEM OR NONE. Only the names this call wrote are removed,
            # so a header the caller set by hand is never touched even when it
            # is byte-identical to ours. A map that will not take a deletion
            # either is left as it is: what remains is a strict PREFIX of our
            # own set, which the inbound half reads as an ordinary hop with no
            # parent. Degraded, never malformed.
            for name in written:
                try:
                    del headers[name]
                except Exception:  # noqa: BLE001, PERF203
                    pass
            return ()
    except Exception:  # noqa: BLE001
        pass
    return tuple(written)


def _restore_trace_headers(headers: Any, written: Tuple[str, ...]) -> None:
    """Put the header map back the way we found it, once the request is sent.

    Not a policy decision and not a guess about ownership: ``written`` is the
    list of names THIS call added to THIS object a moment ago, so there is
    nothing to infer. Two reasons it has to happen:

    A REDIRECT INHERITS THE HEADERS. httpx builds the next request by copying
    the answered request's header map, so a permitted first hop that 302s to a
    public host would carry our trace id to that host without the policy ever
    being asked about the second destination. With ours taken back off, every
    hop is judged on its own merits and attached freshly — which is exactly the
    answer the first hop got.

    THE HOST KEEPS THE REQUEST OBJECT. Whatever it reads off those headers
    afterwards should be the request it built, not the one we borrowed.

    Deliberately NOT value-matching. A header carrying the current trace id may
    perfectly well be the caller's: the kit ships a header builder for exactly
    that purpose, so a hand-written forward is indistinguishable from ours by
    value. Removing it would silently undo the developer's own work, so only
    names we know we wrote are removed.

    Never raises. A header map that will not let go of a key keeps it.
    """
    if not written:
        return
    try:
        for name in written:
            try:
                del headers[name]
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def _swap(library: str, owner: Any, attr: str, original: Any, wrapper: Any) -> None:
    """Put ``wrapper`` in place of ``original`` and record BOTH.

    Recording the wrapper is what makes the restore safe: at unpatch time the
    only honest question is "is the callable in place still the one we put
    there?", and that cannot be answered from the original alone.
    """
    _patches.append((library, owner, attr, original, wrapper))
    setattr(owner, attr, wrapper)


# ═══════════════════════════════════════════════════════════════════════════
# httpx
# ═══════════════════════════════════════════════════════════════════════════
def _httpx_request(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
    """The ``Request`` a transport was handed, whichever way it was passed."""
    if "request" in kwargs:
        return kwargs["request"]
    return args[0] if args else None


def _install_httpx() -> bool:
    """Watch httpx's sync + async transports. False if it could not be done."""
    httpx = sys.modules.get("httpx")
    if httpx is None:
        return False
    sync_cls = getattr(httpx, "HTTPTransport", None)
    async_cls = getattr(httpx, "AsyncHTTPTransport", None)
    real_sync = getattr(sync_cls, "handle_request", None) if sync_cls else None
    real_async = (
        getattr(async_cls, "handle_async_request", None) if async_cls else None
    )
    if real_sync is None and real_async is None:
        return False

    def _target_of(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Optional[str]:
        request = _httpx_request(args, kwargs)
        if request is None:
            return None
        return _target(getattr(request, "method", None), getattr(request, "url", None))

    def _httpx_propagate(
        args: Tuple[Any, ...], kwargs: Dict[str, Any]
    ) -> Tuple[Any, Tuple[str, ...]]:
        """Attach this hop's trace headers to a transport-level httpx request.

        The transport is the right layer: the URL is already absolute (so a
        client opened with ``base_url=`` still resolves to a real host) and
        httpx has already merged the caller's own headers into
        ``request.headers``, so a developer who forwards the trace by hand is
        visible here and is left alone.

        Returns the header map and the names written, so the send can be
        followed by the restore that keeps a redirect from inheriting them.
        Never raises.
        """
        try:
            request = _httpx_request(args, kwargs)
            if request is None:
                return (None, ())
            headers = getattr(request, "headers", None)
            written = _attach_trace_headers(
                headers,
                getattr(request, "method", None),
                getattr(request, "url", None),
            )
            return (headers, written)
        except Exception:  # noqa: BLE001
            return (None, ())

    class _CallFiler:
        """Own one transport call until its response stream is finished."""

        def __init__(
            self,
            target: Optional[str],
            started: float,
            ai: Any = None,
            db_scope: Any = None,
        ) -> None:
            self._target = target
            self._started = started
            self._ai = ai
            # Captured on the ISSUING stack, not read here: a streamed response
            # can finish long after its request has ended.
            self._db_scope = db_scope
            self._lock = threading.Lock()
            self._finished = False

        def finish(self, exc: Optional[BaseException]) -> None:
            try:
                with self._lock:
                    if self._finished:
                        return
                    self._finished = True
                try:
                    if self._ai is not None:
                        self._ai.finish(exc)
                except Exception:  # noqa: BLE001
                    pass
                _file_call(
                    self._target,
                    self._started,
                    exc,
                    self._ai is not None,
                    self._db_scope,
                )
            except Exception:  # noqa: BLE001
                pass

    sync_stream_cls = getattr(httpx, "SyncByteStream", None)
    async_stream_cls = getattr(httpx, "AsyncByteStream", None)

    class _SyncObservedStream(sync_stream_cls):  # type: ignore[misc, valid-type]
        def __init__(self, stream: Any, filer: _CallFiler) -> None:
            self._stream = stream
            self._filer = filer

        def __iter__(self):  # type: ignore[no-untyped-def]
            try:
                for chunk in self._stream:
                    try:
                        if self._filer._ai is not None:
                            self._filer._ai.chunk(chunk)
                    except Exception:  # noqa: BLE001
                        pass
                    yield chunk
            except BaseException as exc:  # noqa: BLE001
                self._filer.finish(exc)
                raise
            self._filer.finish(None)

        def close(self) -> None:
            try:
                self._stream.close()
            except BaseException as exc:  # noqa: BLE001
                self._filer.finish(exc)
                raise
            self._filer.finish(None)

        def __del__(self) -> None:
            # An abandoned streaming response must not leave an in-flight call
            # retained forever or turn unknown work into a success.
            try:
                self._filer.finish(RuntimeError("response stream abandoned"))
            except Exception:  # noqa: BLE001
                pass

    class _AsyncObservedStream(async_stream_cls):  # type: ignore[misc, valid-type]
        def __init__(self, stream: Any, filer: _CallFiler) -> None:
            self._stream = stream
            self._filer = filer

        async def __aiter__(self):  # type: ignore[no-untyped-def]
            try:
                async for chunk in self._stream:
                    try:
                        if self._filer._ai is not None:
                            self._filer._ai.chunk(chunk)
                    except Exception:  # noqa: BLE001
                        pass
                    yield chunk
            except BaseException as exc:  # noqa: BLE001
                self._filer.finish(exc)
                raise
            self._filer.finish(None)

        async def aclose(self) -> None:
            try:
                await self._stream.aclose()
            except BaseException as exc:  # noqa: BLE001
                self._filer.finish(exc)
                raise
            self._filer.finish(None)

        def __del__(self) -> None:
            try:
                self._filer.finish(RuntimeError("response stream abandoned"))
            except Exception:  # noqa: BLE001
                pass

    if real_sync is not None:

        def handle_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            # Signature-agnostic on purpose: the host's arguments are passed
            # straight through, so a future httpx that changes them still works
            # and simply goes unobserved rather than breaking the app's call.
            #
            # Trace propagation first and on its own: this is the only thing in
            # the wrapper that touches the host's request rather than merely
            # reading it, and it runs outside the timing so the duration the
            # meter reports stays the host's call and not ours.
            propagated = _httpx_propagate(args, kwargs)
            started = perf_counter()
            # Read on the issuing stack, beside the clock, for the pooled-
            # connection reason spelled out on _file_call.
            db_scope = _db_scope_now()
            try:
                target = _target_of(args, kwargs)
            except Exception:  # noqa: BLE001
                target = None
            ai = None
            try:
                from boosthis.ai_calls import observer_for_httpx
                ai = observer_for_httpx(_httpx_request(args, kwargs))
            except Exception:  # noqa: BLE001
                pass
            try:
                response = real_sync(self, *args, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                if ai is not None:
                    try:
                        ai.finish(exc)
                    except Exception:  # noqa: BLE001
                        pass
                _file_call(target, started, exc, ai is not None, db_scope)
                raise
            finally:
                # The request has been written to the wire (or has failed on
                # the way), so our headers have done their job. Taking them
                # back off is what stops a 3xx from carrying them to a second
                # destination the policy was never asked about.
                _restore_trace_headers(*propagated)
            try:
                if ai is not None:
                    ai.headers(response)
                try:
                    from boosthis.request_error_timer_meters import note_upstream_cache
                    note_upstream_cache(getattr(response, "headers", None))
                except Exception:  # noqa: BLE001
                    pass
                response.stream = _SyncObservedStream(
                    response.stream, _CallFiler(target, started, ai, db_scope)
                )
            except Exception:  # noqa: BLE001
                if ai is not None:
                    ai.finish(None)
                _file_call(target, started, None, ai is not None, db_scope)
            return response

        _swap("httpx", sync_cls, "handle_request", real_sync, handle_request)

    if real_async is not None:

        async def handle_async_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            # Trace propagation first, for the reason on the sync twin above.
            propagated = _httpx_propagate(args, kwargs)
            started = perf_counter()
            # Read on the issuing stack, beside the clock, for the pooled-
            # connection reason spelled out on _file_call.
            db_scope = _db_scope_now()
            try:
                target = _target_of(args, kwargs)
            except Exception:  # noqa: BLE001
                target = None
            ai = None
            try:
                from boosthis.ai_calls import observer_for_httpx
                ai = observer_for_httpx(_httpx_request(args, kwargs))
            except Exception:  # noqa: BLE001
                pass
            try:
                response = await real_async(self, *args, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                if ai is not None:
                    try:
                        ai.finish(exc)
                    except Exception:  # noqa: BLE001
                        pass
                _file_call(target, started, exc, ai is not None, db_scope)
                raise
            finally:
                # The request has been written to the wire (or has failed on
                # the way), so our headers have done their job. Taking them
                # back off is what stops a 3xx from carrying them to a second
                # destination the policy was never asked about.
                _restore_trace_headers(*propagated)
            try:
                if ai is not None:
                    ai.headers(response)
                try:
                    from boosthis.request_error_timer_meters import note_upstream_cache
                    note_upstream_cache(getattr(response, "headers", None))
                except Exception:  # noqa: BLE001
                    pass
                response.stream = _AsyncObservedStream(
                    response.stream, _CallFiler(target, started, ai, db_scope)
                )
            except Exception:  # noqa: BLE001
                if ai is not None:
                    ai.finish(None)
                _file_call(target, started, None, ai is not None, db_scope)
            return response

        _swap(
            "httpx",
            async_cls,
            "handle_async_request",
            real_async,
            handle_async_request,
        )

    return True


# ═══════════════════════════════════════════════════════════════════════════
# aiohttp
# ═══════════════════════════════════════════════════════════════════════════
# The stamp for the hop currently in flight. aiohttp hands the SAME Connection
# object to every stage of one round trip, so it is the natural carrier: there
# is no map to leak, and the stamp disappears when the hop does.
_HOP_ATTR = "_boosthis_hop"


def _hop_started(
    conn: Any, started: float, target: Optional[str], db_scope: Any = None
) -> None:
    """Mark the round trip this connection is about to carry. Never raises.

    The database scope rides the stamp because an aiohttp connection is POOLED:
    the same object carries a later request's hop, so reading the ambient scope
    when the response arrives would credit the call to whichever request happens
    to be standing there.
    """
    try:
        setattr(conn, _HOP_ATTR, (started, target, db_scope))
    except Exception:  # noqa: BLE001
        pass  # a future aiohttp with __slots__: the hop goes unobserved


def _hop_finished(conn: Any, exc: Optional[BaseException]) -> None:
    """File the round trip this connection carries — ONCE. Never raises.

    The stamp is removed before filing, so the two places a hop can end (the
    write failing, or the response arriving) can both call this and only the
    first one counts.
    """
    try:
        hop = getattr(conn, _HOP_ATTR, None)
        if hop is None:
            return
        try:
            delattr(conn, _HOP_ATTR)
        except Exception:  # noqa: BLE001
            return  # could not claim it: better uncounted than counted twice
        # Tolerant of a two-element stamp: a hop in flight across a reload is
        # simply not attributed to a request rather than raising into aiohttp.
        started, target = hop[0], hop[1]
        db_scope = hop[2] if len(hop) > 2 else None
    except Exception:  # noqa: BLE001
        return
    _file_call(target, started, exc, False, db_scope)


def _install_aiohttp() -> bool:
    """Watch aiohttp ONE ROUND TRIP AT A TIME. False if it could not be done.

    NOT ``ClientSession._request``, which is the obvious funnel and the wrong
    one: aiohttp runs its redirect loop INSIDE that method. A call that follows
    three redirects makes four real round trips, and watching the outer method
    files it as ONE call that took as long as four — undercounting the meter
    and inventing a slow call that never happened. httpx puts its redirect loop
    ABOVE the transport, which is why one wrapper is enough there; matching
    behaviour in aiohttp needs the layer underneath the loop.

    Three narrow wrappers, one hop:

    * ``BaseConnector.connect`` opens the hop — first thing to run, so the
      measured time includes connection setup exactly as the stdlib hook and
      the httpx transport do, and a refused connection is a failure with a
      duration rather than a call that never happened;
    * ``ClientRequest.send`` closes it early if the write itself fails;
    * ``ClientResponse.start`` closes it when the response arrives.

    Each is called once per hop, redirects included. A caller with a custom
    connector or a mocked response class is correctly not counted — nothing
    left the process through a layer we watch.
    """
    aiohttp = sys.modules.get("aiohttp")
    if aiohttp is None:
        return False
    connector_cls = getattr(aiohttp, "BaseConnector", None)
    request_cls = getattr(aiohttp, "ClientRequest", None)
    response_cls = getattr(aiohttp, "ClientResponse", None)
    real_connect = getattr(connector_cls, "connect", None) if connector_cls else None
    real_send = getattr(request_cls, "send", None) if request_cls else None
    real_start = getattr(response_cls, "start", None) if response_cls else None
    if real_connect is None or real_send is None or real_start is None:
        # Half of this is not a coverage story worth telling: report the miss
        # and let the blind-spot count say aiohttp is unwatched.
        return False

    def _first(kwargs: Dict[str, Any], key: str, args: Tuple[Any, ...]) -> Any:
        if key in kwargs:
            return kwargs[key]
        return args[0] if args else None

    async def connect(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Signature-agnostic on purpose: a future aiohttp that reshuffles these
        # arguments goes unobserved rather than breaking the app's call.
        started = perf_counter()
        db_scope = _db_scope_now()
        try:
            req = _first(kwargs, "req", args)
            target = _target(getattr(req, "method", None), getattr(req, "url", None))
        except Exception:  # noqa: BLE001
            target = None
        try:
            conn = await real_connect(self, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            _file_call(target, started, exc, False, db_scope)
            raise
        _hop_started(conn, started, target, db_scope)
        return conn

    async def send(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Trace propagation. ``send`` is the last stage that still holds the
        # request's own header map before aiohttp writes it, and it sits BELOW
        # the session's redirect loop — so a redirected call is re-tagged for
        # wherever it actually ended up rather than carrying the first hop's
        # decision to a destination nobody judged.
        _attach_trace_headers(
            getattr(self, "headers", None),
            getattr(self, "method", None),
            getattr(self, "url", None),
        )
        try:
            return await real_send(self, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            _hop_finished(_first(kwargs, "conn", args), exc)
            raise

    async def start(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        conn = _first(kwargs, "connection", args)
        try:
            response = await real_start(self, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            _hop_finished(conn, exc)
            raise
        _hop_finished(conn, None)
        try:
            from boosthis.request_error_timer_meters import note_upstream_cache
            note_upstream_cache(getattr(response, "headers", None))
        except Exception:  # noqa: BLE001
            pass
        return response

    _swap("aiohttp", connector_cls, "connect", real_connect, connect)
    _swap("aiohttp", request_cls, "send", real_send, send)
    _swap("aiohttp", response_cls, "start", real_start, start)
    return True


_INSTALLERS: Dict[str, Callable[[], bool]] = {
    "httpx": _install_httpx,
    "aiohttp": _install_aiohttp,
}


# ═══════════════════════════════════════════════════════════════════════════
# lifecycle
# ═══════════════════════════════════════════════════════════════════════════
def arm_client_hooks() -> None:
    """Watch every known client library this process has already imported.

    Called from :func:`live_detectors.arm_outbound_http_hook` so the kit keeps
    ONE lever for outbound observation. Deliberately cheap and idempotent: it
    re-reads ``sys.modules`` on every arm, which is how a client imported
    lazily — after the first request the app served — is still picked up. Never
    raises; a library that cannot be patched is recorded as unwatched, which is
    what the honesty report exists to surface.
    """
    global _arm_attempted, _active
    if is_boosthis_disabled():
        return
    _arm_attempted = True
    _active = True
    # Nothing left to install is the common case by far (every request after
    # the first), so answer it without touching the lock at all.
    if all(_installed.get(n) or _failed.get(n) or n not in sys.modules for n in WATCHABLE_CLIENTS):
        return
    # One installer at a time; the loser leaves IMMEDIATELY rather than
    # waiting, because the winner is already installing and the loser's very
    # next request will see the flags and skip. Our bookkeeping must never sit
    # in front of the app's request.
    try:
        acquired = _client_lock.acquire(timeout=0.2)
    except Exception:  # noqa: BLE001
        return
    if not acquired:
        return
    try:
        for name in WATCHABLE_CLIENTS:
            if _installed.get(name) or _failed.get(name):
                continue
            if name not in sys.modules:
                continue
            try:
                ok = _INSTALLERS[name]()
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                _installed[name] = True
            else:
                _failed[name] = True
    finally:
        try:
            _client_lock.release()
        except Exception:  # noqa: BLE001
            pass


def unpatch_client_hooks() -> None:
    """Restore every client method this kit replaced. Never raises.

    Undoes the patches WITHOUT recording an opt-out — the public opt-out is
    ``live_detectors.disarm_outbound_http_hook()``, which calls this after
    setting its own sticky flag. ``_active`` is cleared FIRST so a restore that
    fails still leaves a silent wrapper behind rather than a live one.

    ALL OR NOTHING, PER LIBRARY. A method is put back only while OUR wrapper is
    still the callable in place. If a host library patched on top of any of the
    methods we replaced in that client, restoring what we captured at arming
    time would silently delete THEIR work — so every one of that client's
    wrappers stays exactly where it is, inert (the feed is switched off above),
    and the client stays marked installed so a later arming switches it back on
    instead of stacking a second layer. Per library rather than per method
    because re-arming is per library: half a client restored could never be
    completed again.
    """
    global _active
    _active = False
    stranded: Dict[str, bool] = {}
    for library, owner, attr, _original, wrapper in _patches:
        try:
            if getattr(owner, attr, None) is not wrapper:
                stranded[library] = True
        except Exception:  # noqa: BLE001
            stranded[library] = True
    for library, owner, attr, original, _wrapper in reversed(_patches):
        if stranded.get(library):
            continue
        try:
            setattr(owner, attr, original)
        except Exception:  # noqa: BLE001
            stranded[library] = True
    _patches[:] = [p for p in _patches if stranded.get(p[0])]
    _installed.clear()
    _failed.clear()
    _installed.update(stranded)


def clear_outbound_clients() -> None:
    """Wipe all state (wired into ``forget()``). Never raises.

    Erasure, not an opt-out: after this the kit claims nothing and the honesty
    report goes quiet again, exactly as it was before the first arm.
    """
    global _arm_attempted
    unpatch_client_hooks()
    _arm_attempted = False


# ═══════════════════════════════════════════════════════════════════════════
# honesty: what can this kit actually see?
# ═══════════════════════════════════════════════════════════════════════════
def outbound_watch_report() -> Dict[str, int]:
    """``{"watched": n, "unwatched": n}`` — outbound client libraries loaded in
    this process, split by whether the kit can actually observe their calls.

    This is what stops an unwatchable client from reading as an app that makes
    no calls. Both meters carry the unwatched count so their tiles can say
    "cannot tell" rather than report an empty picture as a finding.

    A blind spot only exists while the kit is actually watching: it means "we
    are reporting on this app's calls, and some of them we cannot see". So
    whenever observation is not running, this reports ZERO rather than a blind
    spot — the axes are simply absent, which is the honest, pre-existing
    answer. That covers three situations:

    * observation has never been armed — the kit has not claimed to watch
      anything yet, so there is nothing to be blind about;
    * the app opted OUT. Someone who switched the observation off did not
      create a blind spot, they closed one deliberately, and nagging about it
      on every tile would be noise they already declined;
    * the patches were removed (erasure). Nothing is being watched, so nothing
      is being half-watched either.

    Counts only: a library name never leaves this process. Never raises.
    """
    try:
        from boosthis import live_detectors as ld

        if not _arm_attempted or not _active or ld._http_hook_opted_out:
            return {"watched": 0, "unwatched": 0}
        watched = 0
        unwatched = 0
        if _STDLIB_CLIENT in sys.modules:
            if ld._http_hook_installed:
                watched += 1
            else:
                unwatched += 1
        for name in WATCHABLE_CLIENTS:
            if name not in sys.modules:
                continue
            if _installed.get(name):
                watched += 1
            else:
                unwatched += 1
        for name in UNWATCHABLE_CLIENTS:
            if name in sys.modules:
                unwatched += 1
        return {"watched": watched, "unwatched": unwatched}
    except Exception:  # noqa: BLE001
        return {"watched": 0, "unwatched": 0}


def unwatched_client_count() -> int:
    """Shorthand for the number the two meters put on the wire. Never raises."""
    try:
        return int(outbound_watch_report()["unwatched"])
    except Exception:  # noqa: BLE001
        return 0
