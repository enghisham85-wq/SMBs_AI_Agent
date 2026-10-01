"""Full-stack trace tag — Python sibling of the RN + Node trace primitives.

Stage 1 of the flagship cross-runtime trace: PROPAGATION + PROOF. This module
mints / validates / forwards a privacy-safe correlation id so one user action
can be stitched across all three runtimes (RN -> Node -> Python). It stores and
uploads nothing — that is a later stage.

The contract is byte-for-byte identical to ``trace.ts`` in the RN and Node
runtimes:

  * Header name: ``x-boosthis-trace`` (lowercase constant).
  * ID format: exactly 32 lowercase hex chars (128 random bits) —
    ``^[0-9a-f]{32}$``.
  * Adopt-or-mint sanitize-on-receive: adopt an incoming id only if it matches
    the format, otherwise mint a fresh one, so a caller can never smuggle PII
    (or a header-injection payload) through the trace header.

PRIVACY CONTRACT (verified against pii.py): a 32-hex value cannot match the
guard's email / JWT / bearer / IPv4 / IPv6 / phone value patterns, and the keys
``trace_id`` / ``x-boosthis-trace`` are guard-clean.

WARNING: a trace id must NEVER be used as (or embedded in) a route/screen label
— ``check_route_label``'s long-numeric check would flag a digit-edged id. Keep
trace ids in their own header/contextvar only.
"""

from __future__ import annotations

import re
import secrets
import time

import json

from .event_loop_lag import sample_event_loop_lag
from .runtime_vitals import note_request
from .live_detectors import (
    _auto_arm_outbound_http_hook,
    current_in_flight,
    note_request_end,
    note_request_start,
)
from . import extra_meters
from . import mcp_measure
from . import repeated_work
from . import ai_calls
from . import dependency_work
from . import db_work
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Mapping, Optional

from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.span_scope import (
    PARENT_HEADER,
    adopt_parent_span_id,
    current_span_id as current_ambient_span_id,
    new_span_id,
    sanitize_span_id,
)

#: Canonical lowercase header name carried on every instrumented request.
TRACE_HEADER = "x-boosthis-trace"

#: A valid trace id is exactly 32 lowercase hex characters (128 bits).
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: The current request's trace id, if any. Set by ``BoosthisTraceMiddleware``
#: (or manually via ``set_trace_id``) so ``@track_perf`` code deep in the call
#: stack can read it without threading it through every function signature.
current_trace_id: ContextVar[Optional[str]] = ContextVar(
    "boosthis_current_trace_id", default=None
)

# ─── Elapsed chain (Stage 2 waterfall offsets) ──────────────────────────────
# A companion header carrying a RELATIVE elapsed-ms integer: how long after the
# trace root STARTED this hop was reached, chained from each hop's own
# monotonic delta so the server can stagger the waterfall WITHOUT any absolute
# client timestamp or cross-machine clock comparison. A bounded non-negative
# int carries no PII; a malformed value only degrades the OFFSET (-> 0), never
# the trace correlation. Byte-identical to the RN + Node trace primitives.

#: Max request-body bytes buffered for the auto-HTTP MCP relabel. A JSON-RPC
#: envelope (method + params.name) is tiny; anything past this cap can only be
#: tool ARGUMENTS we never read, so we stop teeing once we have enough.
_MCP_BODY_CAP_BYTES = 64 * 1024

#: Companion header carrying the relative elapsed-ms offset for the next hop.
ELAPSED_HEADER = "x-boosthis-trace-elapsed"

#: A propagated elapsed value is 1-6 digits (0..999999); clamped on receive.
TRACE_ELAPSED_RE = re.compile(r"^\d{1,6}$")

#: Upper clamp for a propagated elapsed value (mirrors MAX_SPAN_DURATION_MS).
MAX_TRACE_ELAPSED_MS = 600000

#: Elapsed-ms base adopted from the incoming request's elapsed header (how long
#: after the trace root started this hop was reached). 0 when absent/malformed.
current_trace_elapsed_base: ContextVar[int] = ContextVar(
    "boosthis_current_trace_elapsed_base", default=0
)

#: ``time.monotonic()`` captured when the middleware began handling the current
#: request, so elapsed can be advanced by this process's own time-in-request.
current_trace_request_start: ContextVar[Optional[float]] = ContextVar(
    "boosthis_current_trace_request_start", default=None
)

#: This request's own span identity and the caller identity adopted from its
#: parent header. They share the trace contextvars' exact set/reset lifecycle.
current_request_span_id: ContextVar[Optional[str]] = ContextVar(
    "boosthis_current_request_span_id", default=None
)
current_request_parent_span_id: ContextVar[Optional[str]] = ContextVar(
    "boosthis_current_request_parent_span_id", default=None
)


def sanitize_trace_elapsed(value: Any) -> int:
    """Sanitize-on-receive for the elapsed chain.

    Parse a bounded non-negative integer from the incoming header value, or 0
    if it is missing/malformed. Never raises; unlike the id, a bad value only
    degrades the OFFSET (-> 0) — it never re-mints or drops the correlation.
    """
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("latin-1")
        except Exception:
            return 0
    if isinstance(value, str) and TRACE_ELAPSED_RE.match(value):
        return min(MAX_TRACE_ELAPSED_MS, int(value))
    return 0


def _elapsed_since_request_ms() -> float:
    """Milliseconds since the middleware began the current request (0 if no
    request context — e.g. a worker/CLI process). Monotonic, never negative."""
    start = current_trace_request_start.get()
    if not isinstance(start, float):
        return 0.0
    return max(0.0, (time.monotonic() - start) * 1000.0)


def current_trace_elapsed() -> int:
    """Elapsed ms since the trace root started, advanced to NOW.

    ``base adopted on receive + this process's own monotonic time-in-request``,
    clamped to [0, MAX_TRACE_ELAPSED_MS]. This is what the NEXT hop should
    receive on the elapsed header. Never raises.
    """
    try:
        base = max(0, current_trace_elapsed_base.get())
        return max(
            0,
            min(
                MAX_TRACE_ELAPSED_MS,
                int(round(base + _elapsed_since_request_ms())),
            ),
        )
    except Exception:
        return 0


def span_start_offset(duration_ms: int) -> int:
    """Offset from the trace root at which a just-finished measurement STARTED.

    The measurement ended NOW and ran for ``duration_ms``, so it started at
    ``base + max(0, time-in-request - duration_ms)``. Clamped, never raises;
    0 outside any request context.
    """
    try:
        base = max(0, current_trace_elapsed_base.get())
        started = base + max(0.0, _elapsed_since_request_ms() - max(0, duration_ms))
        return max(0, min(MAX_TRACE_ELAPSED_MS, int(round(started))))
    except Exception:
        return 0


def is_valid_trace_id(value: Any) -> bool:
    """True if ``value`` matches the locked trace-id format exactly."""
    return isinstance(value, str) and TRACE_ID_RE.match(value) is not None


def new_trace_id() -> str:
    """Mint a fresh 32-hex-char trace id using the CSPRNG.

    ``secrets.token_hex(16)`` yields 16 random bytes as 32 lowercase hex chars.
    """
    return secrets.token_hex(16)


def sanitize_trace_id(value: Any) -> str:
    """Adopt-or-mint: return ``value`` if it is a valid trace id, else mint.

    The sanitize-on-receive trust boundary — anything not matching
    ``^[0-9a-f]{32}$`` is discarded, so no PII (or CRLF header-injection) can
    ride the trace header into the process.
    """
    return value if is_valid_trace_id(value) else new_trace_id()


def read_trace_id(headers: Mapping[str, str]) -> str:
    """Adopt the incoming trace id from a header mapping (or mint a fresh one).

    Accepts a plain ``dict`` of string headers (case-sensitive lookup on the
    lowercase ``TRACE_HEADER``).
    """
    return sanitize_trace_id(headers.get(TRACE_HEADER))


def trace_headers(trace_id: Optional[str] = None) -> dict[str, str]:
    """Build the outbound header map for a request.

    Pass an existing id to propagate it (validated first), or omit to use the
    current contextvar trace, or mint a fresh one.
    """
    tid = trace_id if trace_id is not None else current_trace_id.get()
    headers = {
        TRACE_HEADER: sanitize_trace_id(tid),
        ELAPSED_HEADER: str(current_trace_elapsed()),
    }
    # The innermost synchronous scope is the immediate caller. Outside one,
    # this request itself is the caller of the next hop.
    parent = sanitize_span_id(
        current_ambient_span_id() or current_request_span_id.get()
    )
    if parent is not None:
        headers[PARENT_HEADER] = parent
    return headers


def set_trace_id(trace_id: str) -> str:
    """Set the current trace id (adopt-or-mint) and return the effective id.

    Handy for non-ASGI entry points (workers, CLI jobs) that receive a trace id
    out of band and want ``@track_perf`` code to pick it up.
    """
    tid = sanitize_trace_id(trace_id)
    current_trace_id.set(tid)
    return tid


def get_trace_id() -> Optional[str]:
    """Return the current request's trace id, or None if none is set."""
    return current_trace_id.get()

def get_span_id() -> Optional[str]:
    """Return this request's own span identity, or None outside a request."""
    try:
        return sanitize_span_id(current_request_span_id.get())
    except Exception:
        return None
class BoosthisTraceMiddleware:
    """Pure-ASGI middleware that adopts/mints a trace id per request.

    Framework-agnostic (Starlette / FastAPI / any ASGI app) — no framework
    import required. For each ``http`` request it:

      1. Adopts the incoming ``x-boosthis-trace`` header (or mints a fresh id).
      2. Sets the ``current_trace_id`` contextvar for the duration of the
         request (reset in ``finally``), so ``@track_perf`` code can read it.
      3. Echoes the trace id back in the response headers.
      4. Records the request's TIMING SAMPLE — the reading the dashboard counts
         and the trigger for the snapshot upload. This middleware is the ASGI
         measuring half: without the sample the app registers and reports
         nothing, so the two must never be separated.

    Non-``http`` scopes (``websocket``, ``lifespan``) pass straight through.
    Only Boosthis's own logic is wrapped in try/except — the app's exceptions
    are never swallowed.
    """

    def __init__(
        self, app: Callable[..., Awaitable[Any]], kit_prefix: Optional[str] = None
    ) -> None:
        self.app = app
        # The mount tells us where the kit's OWN pages live so their traffic is
        # never filed as the app's. Optional: a hand-installed middleware (or an
        # older mount) simply measures everything it sees. An EMPTY string is
        # not "nothing was passed" — it is what a root mount's prefix normalises
        # to, and reading it as unknown lets the panel's own polling arrive as
        # the app's busiest route.
        self.kit_prefix = kit_prefix if isinstance(kit_prefix, str) else None

    def _is_kit_path(self, path: str) -> bool:
        """Is this path one of the KIT's own pages?

        A real prefix answers it directly. Mounted at the ROOT the kit's pages
        sit at their bare suffixes beside the app's own routes, so the kit's own
        route table answers instead — testing ``startswith("/")`` there would
        fold the whole app away, which is the very "registers, then reports
        nothing" failure this boundary exists to prevent. A hand-installed
        middleware was told nothing about a mount, so it excludes nothing.
        """
        prefix = self.kit_prefix
        if prefix is None:
            return False
        if prefix not in ("", "/"):
            return path == prefix or path.startswith(prefix + "/")
        try:
            from boosthis import kit_pages as _kit_pages

            if path.startswith(_kit_pages.RULE_PREFIX):
                return True
            return any(path == (suffix or "/") for suffix, _method, _guard in _kit_pages.ROUTES)
        except Exception:
            return False

    def _kit_base(self) -> Optional[str]:
        """The prefix this door speaks for when NO mount registered the pages.

        A hand-installed middleware was told nothing about a mount, so it speaks
        for the address the setup block, the install guide and the kit's own
        comments print — the default prefix. A ROOT mount ("") is never answered
        for here: at the root every host path looks like a kit path, and the
        mount that put us there has already registered the real routes.
        """
        try:
            from boosthis import kit_pages as _kit_pages

            prefix = self.kit_prefix
            base = _kit_pages.KIT_BASE_PATH if prefix is None else prefix
            return _kit_pages.normalise_base(base) or None
        except Exception:
            return None

    async def _answer_kit_read(self, scope: Mapping[str, Any], send: Any) -> bool:
        """Answer one kit-owned address this app registered no route for.

        ``boosthis.mount(app)`` is the call that registers the kit's pages, and
        it is optional: an app can install this middleware by hand and be
        measured perfectly without it. Until now every read the mount would have
        registered — the advertised ``/_boosthis`` page included — fell through
        to the HOST's 404, a reply byte-identical to having no kit installed.

        So this door answers for itself: the two closed badge reads and the
        loopback-only status page are served outright, and the rest of the
        prefix gets the kit's own 404 naming the missing call (see
        ``kit_pages.answer_unmounted_kit_read``). Returns True when the reply
        went out and the host must not see the request.

        Answered BEFORE the instrumentation below: a read of the kit's own pages
        is not the app's traffic, and returning here keeps it out of every meter
        — the same reason ``_is_kit_path`` exists for a mounted install.
        """
        try:
            path = scope.get("path")
            if not isinstance(path, str):
                return False
            if str(scope.get("method") or "GET").upper() != "GET":
                # A non-GET on the prefix is the host's business: the kit's own
                # POST route (/api/match) only exists once a mount registers it.
                return False
            from boosthis import kit_pages as _kit_pages

            base = self._kit_base()
            if base is None or not path.startswith(base):
                return False
            if _kit_pages.are_kit_paths_registered(base):
                return False
            suffix = _kit_pages.kit_read_suffix_for(path, base)
            if suffix is None:
                return False
            if _kit_pages.middleware_served(suffix):
                reply = _kit_pages.handle(
                    suffix,
                    method="GET",
                    remote_addr=_client_host_from_scope(scope),
                    headers=_headers_map_from_scope(scope),
                )
            else:
                registered = _kit_pages.registered_kit_base_paths()
                reply = _kit_pages.answer_unmounted_kit_read(
                    suffix,
                    base=base,
                    elsewhere=registered[0] if registered else None,
                )
            if reply is None:
                return False
        except Exception:
            # Nothing has been written yet — let the host answer as it always
            # did rather than break a request over a diagnostic.
            return False
        return await _send_kit_reply(send, reply)

    def _sample_label(self, scope: Mapping[str, Any]) -> Optional[str]:
        """The label this request files under, or None to record nothing.

        Kit pages return None: a kit's own traffic is not the app's reading.
        """
        path = scope.get("path")
        if isinstance(path, str) and self._is_kit_path(path):
            return None
        return _route_label_from_scope(scope)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> Any:
        # ASGI exposes WebSockets as their own scope. Observe at this already-
        # mounted boundary; the watcher forwards every protocol message and
        # never retains an endpoint, frame, header or payload.
        if (
            not is_boosthis_disabled()
            and isinstance(scope, Mapping)
            and scope.get("type") == "websocket"
        ):
            try:
                from boosthis.live_connections import watch_asgi
            except Exception:
                await self.app(scope, receive, send)
                return
            await watch_asgi(scope, receive, send, self.app)
            return
        # Kill-switch AND non-http scopes: pure pass-through. Never mint, echo,
        # or touch the contextvar when silenced from outside the process.
        if (
            is_boosthis_disabled()
            or not isinstance(scope, Mapping)
            or scope.get("type") != "http"
        ):
            await self.app(scope, receive, send)
            return

        # Idle-burn boundary: a request is now in flight. Detects any CPU burn
        # during the just-ended idle window. This increment is paired with the
        # note_request_end() in the OUTERMOST finally below: once we have counted
        # a start, the end runs on EVERY exit path — an early return, a thrown
        # error, or an exception inside our own instrumentation — so the shared
        # in-flight counter can never leak upward.
        # An address we print, answered by the only door this app installed.
        # Before every meter below: the kit's own pages are not the app's work.
        if await self._answer_kit_read(scope, send):
            return

        note_request_start()
        # req_start is stamped BEFORE the guarded body so the deflection duration
        # (and the trace elapsed chain) always has a start mark, even if some
        # instrumentation below raises.
        req_start = time.monotonic()
        # loadDeflection: how many requests were in flight the moment THIS one
        # started (read AFTER note_request_start bumps the counter, so this
        # request counts itself — 1 means "alone"). Filed with the measured
        # duration at completion. A bare int read; cannot affect the response.
        defl_in_flight = current_in_flight()
        # Repeated-work boundary: open the scope every watched call inside this
        # request files into. Self-guarded (returns None under the kill-switch),
        # and its UNCONDITIONAL partner is in the OUTERMOST finally below — so a
        # throw anywhere in our own instrumentation can never leak a scope.
        work_handle = repeated_work.begin_request_work()
        ai_handle = ai_calls.begin_ai_work()
        dependency_handle = dependency_work.begin_dependency_work()
        # Database boundary: open the tally every watched query files into, and
        # the scope a pooled driver's query is attributed to. Same
        # guaranteed-exit contract as the three above.
        db_handle = db_work.begin_db_work()
        # Arm the stdlib HTTP observation on the FIRST request boundary, the
        # same way the Node kit arms its outbound observer: importing the
        # package must cost nothing until the app actually serves traffic.
        # Idempotent after the first call (a bool read), self-guarded, and
        # reversible via disarm_outbound_http_hook(). Without it a Python app
        # can only ever contribute wrapped calls to the repeated-work meter —
        # identical downstream GETs, the case the fan-out rule names, would go
        # unseen.
        _auto_arm_outbound_http_hook()
        ai_calls.arm_ai_calls()
        # …and the database driver the app has already imported, for the same
        # reason and on the same boundary: a driver imported lazily on the first
        # request is still picked up, and this is a throttled no-op afterwards.
        db_work.arm_db_clients()
        token = None
        base_token = None
        start_token = None
        span_token = None
        parent_span_token = None
        try:
            # Event-loop lag probe: measure how long a ready callback waits to be
            # dispatched right now (a running asyncio loop is guaranteed here —
            # this is an async ASGI call). No-op when telemetry is off/disarmed.
            sample_event_loop_lag()

            try:
                trace_id = _trace_id_from_scope(scope)
            except Exception:
                trace_id = new_trace_id()
            try:
                elapsed_base = _trace_elapsed_from_scope(scope)
            except Exception:
                elapsed_base = 0
            try:
                parent_span_id = _parent_span_id_from_scope(scope)
            except Exception:
                parent_span_id = None
            try:
                request_span_id = new_span_id()
            except Exception:
                request_span_id = None

            token = current_trace_id.set(trace_id)
            # Elapsed chain: remember the base adopted on receive plus a monotonic
            # request-start mark, so trace_headers()/span_start_offset() can
            # advance the offset by this process's own time-in-request.
            base_token = current_trace_elapsed_base.set(elapsed_base)
            start_token = current_trace_request_start.set(req_start)
            span_token = current_request_span_id.set(request_span_id)
            parent_span_token = current_request_parent_span_id.set(parent_span_id)

            # MCP per-tool breakdown (auto for HTTP): for a `POST <any-path>/mcp`
            # request we tee the request body (cap 64KB — the JSON-RPC envelope is
            # tiny) WITHOUT consuming it away from the app, then at response
            # completion derive ONE bounded `mcp.*` sample from method +
            # params.name. Labels are a CLOSED set (protocol allowlist + developer
            # roster); tool ARGUMENTS are never read. Everything is guarded so it
            # can never break or slow the host request path.
            mcp_watch = False
            try:
                mcp_watch = _is_mcp_post(scope)
            except Exception:
                mcp_watch = False
            mcp_body = bytearray() if mcp_watch else None
            mcp_status: dict[str, Any] = {"code": None}
            response_shape: dict[str, Any] = {"status": None, "headers": None}
            realtime: dict[str, Any] = {"contentType": "", "connection": None}
            try:
                if not scope.get(_QUEUE_MEASURED_KEY):
                    scope[_QUEUE_MEASURED_KEY] = True  # type: ignore[index]
                    extra_meters.note_queue_start(scope.get("headers") or [])
            except Exception:
                pass

            receive_for_app = receive
            if mcp_watch:
                async def receive_teeing() -> Any:
                    message = await receive()
                    try:
                        if (
                            isinstance(message, Mapping)
                            and message.get("type") == "http.request"
                            and mcp_body is not None
                            and len(mcp_body) < _MCP_BODY_CAP_BYTES
                        ):
                            chunk = message.get("body") or b""
                            if isinstance(chunk, (bytes, bytearray)):
                                room = _MCP_BODY_CAP_BYTES - len(mcp_body)
                                mcp_body.extend(bytes(chunk)[:room])
                    except Exception:
                        pass  # never disturb the app's body stream
                    return message

                receive_for_app = receive_teeing

            async def send_with_trace(message: Any) -> None:
                if isinstance(message, Mapping) and message.get("type") == "http.response.start":
                    try:
                        # Runtime-vitals boundary: bump reliability counters from
                        # the final HTTP status + sample memory (throttled).
                        # Additive + display-only — never feeds the Speed score.
                        status = message.get("status")
                        response_shape["status"] = status
                        response_shape["headers"] = message.get("headers")
                        note_request(status if isinstance(status, int) else None)
                        if mcp_watch:
                            mcp_status["code"] = status if isinstance(status, int) else None
                    except Exception:
                        pass  # instrumentation must never disturb the response
                    try:
                        # cookieExposure boundary: observe the OUTGOING Set-Cookie
                        # headers at the same point we read status — the kit sees
                        # what actually goes out on the wire, after the framework
                        # rewrote headers. Counts only; the parser never keeps a
                        # cookie name, value, header text, or route.
                        extra_meters.note_cookie_headers(message.get("headers"))
                    except Exception:
                        pass  # instrumentation must never disturb the response
                    try:
                        # accessPressure boundary: count this response's OUTCOME
                        # class (401/403/429/404/5xx) at the same point we read
                        # status. The client host (scope["client"][0], NEVER the
                        # port) flips one anonymous reach-sketch bit inside the
                        # collector and is retained nowhere. Counts only.
                        client = scope.get("client") if isinstance(scope, Mapping) else None
                        client_addr = (
                            client[0]
                            if isinstance(client, (list, tuple)) and client
                            else None
                        )
                        extra_meters.note_access_outcome(
                            status if isinstance(status, int) else None,
                            client_addr,
                            True if scope.get("route") is not None else None,
                        )
                    except Exception:
                        pass  # instrumentation must never disturb the response
                    try:
                        # refusalHonesty: presence-only header checks at this
                        # SAME response-start boundary; no value is retained.
                        request_headers = scope.get("headers") or []
                        response_headers = message.get("headers") or []
                        authorization_present = any(
                            isinstance(pair, (list, tuple))
                            and len(pair) >= 2
                            and bytes(pair[0]).lower() == b"authorization"
                            and bool(pair[1])
                            for pair in request_headers
                        )
                        challenge_present = any(
                            isinstance(pair, (list, tuple))
                            and len(pair) >= 2
                            and bytes(pair[0]).lower() == b"www-authenticate"
                            and bool(pair[1])
                            for pair in response_headers
                        )
                        extra_meters.note_refusal_honesty(
                            status if isinstance(status, int) else None,
                            authorization_present,
                            challenge_present,
                        )
                    except Exception:
                        pass
                    try:
                        headers = list(message.get("headers") or [])
                        headers.append(
                            (TRACE_HEADER.encode("latin-1"), trace_id.encode("latin-1"))
                        )
                        # message may be an immutable Mapping; build a shallow copy.
                        message = {**message, "headers": headers}
                    except Exception:
                        pass  # never break the response over a header echo
                    try:
                        for raw_name, raw_value in message.get("headers") or []:
                            if bytes(raw_name).lower() == b"content-type":
                                realtime["contentType"] = bytes(raw_value).decode(
                                    "latin-1", "ignore"
                                ).lower()
                                break
                    except Exception:
                        pass
                if (
                    isinstance(message, Mapping)
                    and message.get("type") == "http.response.body"
                    and "text/event-stream" in realtime["contentType"]
                ):
                    try:
                        from boosthis.live_connections import begin_live_connection
                        connection = realtime["connection"]
                        if connection is None and message.get("more_body"):
                            # Folded immediately inside the collector and never
                            # uploaded. No body bytes are passed to it.
                            connection = begin_live_connection(
                                scope.get("path", ""), flow_measurable=True
                            )
                            realtime["connection"] = connection
                        if connection is not None and message.get("body"):
                            connection.activity()
                        if connection is not None and not message.get("more_body", False):
                            connection.close(clean=True)
                    except Exception:
                        pass
                await send(message)

            # Did the app ITSELF fail? An ASGI app that raises past this
            # middleware never sends a status, so the status alone cannot tell
            # a 200 from a crash — the failure would read as "nothing said".
            # Re-raised untouched, so the host's own error handling is
            # unchanged and the traceback is the app's own.
            app_threw = False
            try:
                await self.app(scope, receive_for_app, send_with_trace)
            except BaseException:
                app_threw = True
                raise
            finally:
                try:
                    connection = realtime["connection"]
                    if connection is not None:
                        connection.close(clean=not app_threw)
                except Exception:
                    pass
                duration_ms = (time.monotonic() - req_start) * 1000.0
                held_open = False
                try:
                    from boosthis.held_open import (
                        is_held_open_response,
                        note_held_open_excluded,
                    )

                    held_open = is_held_open_response(
                        status=response_shape.get("status"),
                        headers=response_shape.get("headers"),
                    )
                    if held_open:
                        note_held_open_excluded(duration_ms)
                except Exception:
                    held_open = False
                try:
                    from boosthis.request_error_timer_meters import note_response
                    note_response(
                        self._sample_label(scope) or ASGI_REQUEST_LABEL,
                        response_shape.get("status"),
                        dependency_handle,
                    )
                except Exception:
                    pass
                # MCP auto-HTTP relabel: record ONE extra mcp sample beside the
                # normal route sample when the teed body parses as JSON-RPC.
                # Duration is this process's own time-in-request; error when
                # status >= 500. Unavailable/unparseable body → record nothing
                # extra. Fully guarded.
                #
                # A held-open response is excluded here too: this sample carries
                # the RESPONSE's lifetime into the same store the summary reads,
                # so an SSE reply to POST /mcp would be excluded on its route
                # label and still poison the tail through its mcp.* twin. A tool
                # timed by hand times the tool's own work and is unaffected.
                if mcp_watch and mcp_body is not None and not held_open:
                    try:
                        _record_mcp_auto(
                            bytes(mcp_body),
                            duration_ms,
                            mcp_status.get("code"),
                        )
                    except Exception:
                        pass  # instrumentation must never take down the host.
                # THE timing sample. Every other boundary here files a meter;
                # this is the one reading the dashboard counts under "Samples",
                # and recording it is also what triggers the throttled snapshot
                # upload. Without it a mounted FastAPI/Starlette app registers,
                # checks in, collects meters in memory — and reports NOTHING,
                # which on the dashboard is indistinguishable from a kit that
                # was never installed at all.
                #
                # The label is code-defined (the matched route TEMPLATE, or the
                # endpoint's function name), never the raw URL: a path carries
                # ids. Recorded at most once per request even if the middleware
                # is installed twice, and never for the kit's own pages.
                # Imported at call time — tracker imports THIS module.
                try:
                    if not held_open and not scope.get(_MEASURED_KEY):
                        label = self._sample_label(scope)
                        if label:
                            try:
                                scope[_MEASURED_KEY] = True  # type: ignore[index]
                            except Exception:
                                pass  # an immutable scope only risks a repeat
                            from boosthis import tracker
                            from boosthis.span_work import outcome_for_status

                            # This boundary knows both facts about the work it
                            # just measured: it IS the request handler, and it
                            # saw the status the app answered with. A 4xx is
                            # the app answering; only a 5xx or a throw past
                            # this middleware is the work failing. When
                            # neither is available — no status sent and no
                            # exception seen — the outcome stays unsaid rather
                            # than defaulting to a clean result.
                            _status = response_shape.get("status")
                            _outcome = (
                                "error"
                                if app_threw
                                else (
                                    outcome_for_status(_status)
                                    if isinstance(_status, int)
                                    else None
                                )
                            )
                            tracker._emit(
                                label,
                                int(round(duration_ms)),
                                kind="handler",
                                outcome=_outcome,
                            )
                except Exception:
                    pass  # instrumentation must never disturb the host app.
                # loadDeflection: file this request's duration into the bucket for
                # the in-flight count captured at its start. Fully guarded — it
                # can never disturb the response teardown OR the paired decrement
                # in the outer finally.
                if not held_open:
                    try:
                        extra_meters.note_deflection_sample(
                            defl_in_flight, duration_ms
                        )
                    except Exception:
                        pass
                if token is not None:
                    current_trace_id.reset(token)
                if base_token is not None:
                    current_trace_elapsed_base.reset(base_token)
                if start_token is not None:
                    current_trace_request_start.reset(start_token)
                if span_token is not None:
                    current_request_span_id.reset(span_token)
                if parent_span_token is not None:
                    current_request_parent_span_id.reset(parent_span_token)
        finally:
            # Repeated work: close this request's scope and fold its
            # identical-call counts into the session totals. Same
            # guaranteed-exit reasoning as note_request_end() below; a declined
            # open makes it a no-op, so the pair can never half-run.
            try:
                repeated_work.end_request_work(
                    work_handle, (time.monotonic() - req_start) * 1000.0
                )
            except Exception:
                pass  # instrumentation must never take down the host.
            # This must close before AI: it borrows AI wait from the same live
            # request tally, which the AI close resets.
            try:
                dependency_work.end_dependency_work(
                    dependency_handle, (time.monotonic() - req_start) * 1000.0
                )
            except Exception:
                pass
            try:
                ai_calls.end_ai_work(
                    ai_handle, (time.monotonic() - req_start) * 1000.0
                )
            except Exception:
                pass
            # Database: STRICTLY after the dependency close above, which borrows
            # this request's database ms off the still-open tally so every
            # outside-service share divides by one denominator.
            try:
                db_work.end_db_work(
                    db_handle, (time.monotonic() - req_start) * 1000.0
                )
            except Exception:
                pass
            # Idle-burn boundary: request finished; snapshot the CPU baseline when
            # nothing else is in flight so the next idle window is measured. This
            # is the UNCONDITIONAL partner of the note_request_start() above — it
            # runs on every exit path so the in-flight counter always balances.
            note_request_end()


#: Scope key marking "this request has already been filed as a sample", so a
#: middleware installed twice cannot count one request twice.
_MEASURED_KEY = "boosthis.measured"
_QUEUE_MEASURED_KEY = "boosthis.queue-measured"

#: The name an ASGI request files under when the scope offers no code-defined
#: route to name it by (an unmatched 404, a bare ASGI app). NEVER the raw URL
#: path: an id in the path would both leak and blow the label space open, so
#: everything unnamed lands in one bounded bucket.
ASGI_REQUEST_LABEL = "asgi.request"


def _route_label_from_scope(scope: Mapping[str, Any]) -> str:
    """Name a request after the ROUTE it matched, never after its URL.

    FastAPI publishes the matched route on the scope (``path_format`` is the
    template, ``/orders/{oid}``); plain Starlette publishes the endpoint
    function instead. Both are code-defined and bounded. Anything else falls
    back to :data:`ASGI_REQUEST_LABEL`.
    """
    try:
        # Call-time import: unit_of_work imports THIS module.
        from boosthis import unit_of_work
    except Exception:
        return ASGI_REQUEST_LABEL
    route = scope.get("route")
    for attr in ("path_format", "path"):
        value = getattr(route, attr, None)
        if isinstance(value, str) and value.strip():
            return unit_of_work.safe_label(value, ASGI_REQUEST_LABEL)
    name = getattr(scope.get("endpoint"), "__name__", None)
    if isinstance(name, str) and name.strip():
        return unit_of_work.safe_label(name, ASGI_REQUEST_LABEL)
    return ASGI_REQUEST_LABEL


def _trace_id_from_scope(scope: Mapping[str, Any]) -> str:
    """Extract + adopt-or-mint the trace id from an ASGI ``http`` scope.

    ASGI headers are a list of lowercase ``(bytes, bytes)`` tuples.
    """
    for raw_key, raw_val in scope.get("headers") or []:
        try:
            if bytes(raw_key).decode("latin-1").lower() == TRACE_HEADER:
                return sanitize_trace_id(bytes(raw_val).decode("latin-1"))
        except Exception:
            continue
    return new_trace_id()


def _client_host_from_scope(scope: Mapping[str, Any]) -> Optional[str]:
    """The peer address of an ASGI ``http`` scope, for the kit's loopback guard.

    ASGI hands it over as a ``(host, port)`` pair — or omits it entirely on a
    transport that has none, which the guard reads as "not loopback".
    """
    try:
        client = scope.get("client")
        if isinstance(client, (tuple, list)) and client:
            host = client[0]
            return host if isinstance(host, str) else None
    except Exception:
        pass
    return None


def _headers_map_from_scope(scope: Mapping[str, Any]) -> dict[str, str]:
    """ASGI's ``(bytes, bytes)`` header list as the plain lowercase name -> value
    mapping the kit's guards expect (the proxy-header check reads keys)."""
    out: dict[str, str] = {}
    for raw_key, raw_val in scope.get("headers") or []:
        try:
            out[bytes(raw_key).decode("latin-1").lower()] = bytes(raw_val).decode(
                "latin-1"
            )
        except Exception:
            continue
    return out


async def _send_kit_reply(send: Any, reply: Any) -> bool:
    """Write one framework-free kit answer straight onto the ASGI send channel.

    Returns True when the host must NOT also answer — which includes a failure
    AFTER the response head went out, because the reply is already on the wire
    and a second one corrupts it rather than improving it.
    """
    started = False
    try:
        headers = [
            (b"content-type", str(reply.content_type).encode("latin-1")),
            (b"cache-control", b"no-store"),
        ]
        for name, value in (reply.headers or {}).items():
            if str(name).lower() in ("content-type", "cache-control"):
                continue
            headers.append(
                (str(name).encode("latin-1"), str(value).encode("latin-1"))
            )
        await send(
            {
                "type": "http.response.start",
                "status": int(reply.status),
                "headers": headers,
            }
        )
        started = True
        await send({"type": "http.response.body", "body": reply.body})
        return True
    except Exception:
        return started


def _is_mcp_post(scope: Mapping[str, Any]) -> bool:
    """True when this ASGI ``http`` scope is a POST to a query-less path of
    ``/mcp`` or ending in ``/mcp`` (see ``mcp_measure.is_mcp_path``)."""
    method = scope.get("method")
    if not isinstance(method, str) or method.upper() != "POST":
        return False
    raw = scope.get("path")
    if not isinstance(raw, str):
        return False
    path = raw.split("?", 1)[0]
    return mcp_measure.is_mcp_path(path)


def _record_mcp_auto(
    body: bytes, duration_ms: float, status: Any
) -> None:
    """Parse the teed request body as JSON-RPC and record ONE bounded mcp
    sample. Records nothing when the body is empty or unparseable. ``.error``
    label when the response status is >= 500. Never raises."""
    if not body:
        return
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:
        return  # unparseable → record nothing extra
    label = mcp_measure.mcp_auto_label(parsed)
    if not label:
        return
    failed = isinstance(status, int) and status >= 500
    mcp_measure.record_mcp_sample(label, duration_ms, failed)


def _trace_elapsed_from_scope(scope: Mapping[str, Any]) -> int:
    """Extract + sanitize the elapsed-ms base from an ASGI ``http`` scope.

    Missing/malformed values degrade to 0 (offset only — never affects the
    trace id adoption).
    """
    for raw_key, raw_val in scope.get("headers") or []:
        try:
            if bytes(raw_key).decode("latin-1").lower() == ELAPSED_HEADER:
                return sanitize_trace_elapsed(bytes(raw_val).decode("latin-1"))
        except Exception:
            continue
    return 0

def _parent_span_id_from_scope(scope: Mapping[str, Any]) -> Optional[str]:
    """Extract the parent header from ASGI's byte-pair header list."""
    for raw_key, raw_val in scope.get("headers") or []:
        try:
            if bytes(raw_key).decode("latin-1").lower() == PARENT_HEADER:
                return adopt_parent_span_id(raw_val)
        except Exception:
            continue
    return None

def get_parent_span_id() -> Optional[str]:
    """Return the caller identity adopted by this request, if valid."""
    try:
        return sanitize_span_id(current_request_parent_span_id.get())
    except Exception:
        return None
