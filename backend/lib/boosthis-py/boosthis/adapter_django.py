"""Django, in one call — the kit's pages AND the per-request measuring.

    # myproject/wsgi.py  (or asgi.py — same line either way)
    application = get_wsgi_application()

    import boosthis
    boosthis.mount(application)

Until now a Django developer had to hand-decorate their own views with
``@track_perf`` to see anything at all, which is not a one-step install and is
not what the customer's AI does when it wires the kit in unsupervised.

WHERE THIS ATTACHES, AND WHY THERE
------------------------------------------------------------------
Django's handler (``WSGIHandler`` / ``ASGIHandler``) builds the request, then
calls ``get_response()`` — the method that runs the whole middleware chain, URL
resolution and the view. Wrapping THAT method on the handler INSTANCE:

  * measures the request the way the visitor experiences it, outside every one
    of the host's own middlewares (so a slow middleware is visible, not hidden);
  * hands us the real ``HttpRequest``/``HttpResponse`` pair, which is where the
    status, the outgoing ``Set-Cookie`` headers and the resolved URL pattern
    live;
  * lets the kit's own pages be returned directly, before any host middleware
    runs, so a host's login wall or CSRF rules can never swallow them;
  * needs nothing from the developer's ``settings.MIDDLEWARE``, and cannot be
    reordered, disabled or forgotten there.

Rejected alternatives: returning a WSGI wrapper the developer must remember to
assign (a forgotten ``application =`` is a silent no-op — the exact failure this
task exists to end), and wrapping the private ``_middleware_chain`` (which is
rebuilt on ``load_middleware()`` and would measure from inside the chain).

The route label is Django's own URL pattern (``resolver_match.route``, e.g.
``orders/<int:pk>/``) — code-defined, bounded in number, and never a value from
the URL.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple

from . import host_surface
from . import kit_pages
from . import unit_of_work
from .bubble import (
    MAX_INJECT_BYTES,
    bubble_snippet,
    inject_into_html,
    resolve_bubble_visibility,
)
from .trace import ELAPSED_HEADER, TRACE_HEADER, current_trace_elapsed, sanitize_trace_elapsed
from .span_scope import PARENT_HEADER, adopt_parent_span_id

#: Marker set on a handler we have already wrapped, so a second ``mount()``
#: cannot install a second layer of measuring (double-counted requests).
_MARK = "_boosthis_django_wired"

#: WSGI has no asyncio event loop in its request path; ASGI does. Which one we
#: are on decides both whether to probe loop lag and which readings this host
#: can never produce (see ``host_surface.UNMEASURABLE``).
WSGI_SURFACE = "django-wsgi"
ASGI_SURFACE = "django-asgi"


def is_django_handler(app: Any) -> bool:
    """True for a Django WSGI/ASGI handler, without importing Django."""
    try:
        for cls in type(app).__mro__:
            mod = getattr(cls, "__module__", "") or ""
            if mod == "django.core.handlers.wsgi" or mod.startswith(
                "django.core.handlers.wsgi."
            ):
                return True
            if mod == "django.core.handlers.asgi" or mod.startswith(
                "django.core.handlers.asgi."
            ):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _is_asgi_handler(app: Any) -> bool:
    try:
        return any(
            (getattr(cls, "__module__", "") or "").startswith("django.core.handlers.asgi")
            for cls in type(app).__mro__
        )
    except Exception:  # noqa: BLE001
        return False


def _headers_of(request: Any) -> dict:
    """A plain ``{name: value}`` mapping for the loopback guard.

    ``request.headers`` iterates as KEYS in Django, which is the shape the proxy
    -header check wants, but a plain dict keeps the guard's contract explicit."""
    try:
        return {str(k): str(v) for k, v in request.headers.items()}
    except Exception:  # noqa: BLE001
        return {}


def _set_cookie_pairs(response: Any) -> List[Tuple[bytes, bytes]]:
    """The response's outgoing ``Set-Cookie`` headers in the ASGI shape the
    cookieExposure collector reads.

    Django keeps cookies OUT of ``response.items()`` until the handler
    serialises them, so read ``response.cookies`` — the same place both the WSGI
    and ASGI handlers read them — plus any Set-Cookie set as a plain header."""
    out: List[Tuple[bytes, bytes]] = []
    try:
        for morsel in getattr(response, "cookies", {}).values():
            try:
                out.append((b"set-cookie", morsel.output(header="").strip().encode("latin-1")))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    try:
        for name, value in response.items():
            if str(name).lower() == "set-cookie":
                out.append((b"set-cookie", str(value).encode("latin-1", "replace")))
    except Exception:  # noqa: BLE001
        pass
    return out


def _route_label(request: Any) -> str:
    """``/orders/<int:pk>/`` from Django's resolved URL pattern."""
    try:
        match = getattr(request, "resolver_match", None)
        route = getattr(match, "route", None) if match is not None else None
        # ``is not None``, never truthiness: Django spells its ROOT route as
        # the empty string, so a plain ``if route`` files every hit on a site's
        # busiest page under the nameless fallback.
        if route is not None:
            return unit_of_work.safe_label("/" + str(route).lstrip("/"), "django.request")
    except Exception:  # noqa: BLE001
        pass
    return "django.request"


def _kit_suffix(path: str, prefix: str) -> Optional[str]:
    """The kit-route suffix for ``path``, or ``None`` when it is the host's."""
    if not prefix:
        return path or "/"
    if path == prefix:
        return ""
    if path.startswith(prefix + "/"):
        return path[len(prefix) :]
    return None


def _django_response(reply: kit_pages.KitResponse) -> Any:
    from django.http import HttpResponse  # type: ignore[import-not-found]

    response = HttpResponse(
        reply.body, status=reply.status, content_type=reply.content_type
    )
    for name, value in reply.headers.items():
        response[name] = value
    return response


def _maybe_inject_bubble(response: Any, snippet: str) -> None:
    """Insert the dev bubble into an eligible HTML response, in place.

    Fail-open in every branch: a streaming response, a compressed one, an
    oversized one or any error leaves the host's response exactly as it was."""
    try:
        if getattr(response, "streaming", False):
            return
        status = getattr(response, "status_code", 200) or 200
        if not (200 <= int(status) < 300):
            return
        ct = str(response.headers.get("Content-Type", "") or "")
        if "text/html" not in ct.lower():
            return
        if response.headers.get("Content-Encoding"):
            return
        body = response.content
        if not body or len(body) > MAX_INJECT_BYTES:
            return
        injected = inject_into_html(body.decode("utf-8"), snippet)
        if injected is None:
            return
        response.content = injected.encode("utf-8")
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
    except Exception:  # noqa: BLE001
        return


def mount_django(app: Any, prefix: str, bubble: Optional[bool] = None) -> Any:
    """Install BOTH halves on a Django handler and return it.

    One wrapper carries both: the same ``get_response`` interception that
    answers the kit's pages is the one that measures the host's requests, so
    this adapter structurally cannot deliver a panel with nothing behind it."""
    asgi = _is_asgi_handler(app)
    wiring = host_surface.begin(ASGI_SURFACE if asgi else WSGI_SURFACE, unit="request")

    if getattr(app, _MARK, False):
        # Already wired by an earlier mount() on this very handler: report both
        # halves present (they are) and install nothing twice.
        host_surface.note_panel(wiring, True)
        host_surface.note_measurement(wiring, True)
        host_surface.finish(wiring)
        return app

    show_bubble = False
    snippet = ""
    try:
        show_bubble = bool(resolve_bubble_visibility(bubble))
        if show_bubble:
            snippet = bubble_snippet(f"{prefix}/pulse" if prefix else "/pulse")
    except Exception:  # noqa: BLE001
        show_bubble = False

    def _before(request: Any) -> Tuple[Optional[Any], Optional[unit_of_work.Unit], str]:
        """Answer a kit route outright, or open the measurement boundary."""
        path = str(getattr(request, "path", "") or "")
        try:
            from . import extra_meters

            extra_meters.note_queue_start(_headers_of(request))
        except Exception:  # noqa: BLE001
            pass
        suffix = _kit_suffix(path, prefix)
        if suffix is not None and kit_pages.guard_for(suffix) is not None:
            reply = kit_pages.handle(
                suffix,
                method=str(getattr(request, "method", "GET") or "GET"),
                remote_addr=request.META.get("REMOTE_ADDR"),
                headers=_headers_of(request),
                query=request.GET,
                body=getattr(request, "body", b"") if suffix == "/api/match" else None,
            )
            if reply is not None:
                return _django_response(reply), None, ""
        trace_id = request.META.get("HTTP_" + TRACE_HEADER.upper().replace("-", "_"))
        elapsed = sanitize_trace_elapsed(
            request.META.get("HTTP_" + ELAPSED_HEADER.upper().replace("-", "_"))
        )
        parent_span_id = adopt_parent_span_id(
            request.META.get("HTTP_" + PARENT_HEADER.upper().replace("-", "_"))
        )
        unit = unit_of_work.begin(
            "django.request",
            trace_id=trace_id if isinstance(trace_id, str) else None,
            elapsed_base=elapsed,
            parent_span_id=parent_span_id,
            # Only ASGI Django has a running loop under the request.
            sample_loop_lag=asgi,
        )
        return None, unit, ""

    def _after(request: Any, response: Any, unit: unit_of_work.Unit) -> Any:
        """Close the boundary: label, status, cookies, refusal, bubble, trace."""
        try:
            unit.label = _route_label(request)
        except Exception:  # noqa: BLE001
            pass
        try:
            unit_of_work.note_http_response(
                unit,
                status=getattr(response, "status_code", None),
                response_headers=_set_cookie_pairs(response)
                + (
                    [(b"www-authenticate", b"1")]
                    if _has_header(response, "WWW-Authenticate")
                    else []
                ),
                authorization_present=bool(request.META.get("HTTP_AUTHORIZATION")),
                client_addr=request.META.get("REMOTE_ADDR"),
                response=response,
            )
        except Exception:  # noqa: BLE001
            pass
        if show_bubble and snippet:
            _maybe_inject_bubble(response, snippet)
        try:
            from .trace import get_trace_id, is_valid_trace_id

            tid = get_trace_id()
            if is_valid_trace_id(tid):
                response[TRACE_HEADER] = tid
                response[ELAPSED_HEADER] = str(current_trace_elapsed())
        except Exception:  # noqa: BLE001
            pass
        return response

    original_sync: Optional[Callable[..., Any]] = getattr(app, "get_response", None)
    original_async: Optional[Callable[..., Any]] = getattr(app, "get_response_async", None)

    def measured_get_response(request: Any) -> Any:
        early, unit, _ = _before(request)
        if early is not None:
            return early
        try:
            response = original_sync(request)  # type: ignore[misc]
        except BaseException:
            unit_of_work.end(unit)  # type: ignore[arg-type]
            raise
        try:
            return _after(request, response, unit)  # type: ignore[arg-type]
        finally:
            unit_of_work.end(unit)  # type: ignore[arg-type]

    async def measured_get_response_async(request: Any) -> Any:
        early, unit, _ = _before(request)
        if early is not None:
            return early
        try:
            response = await original_async(request)  # type: ignore[misc]
        except BaseException:
            unit_of_work.end(unit)  # type: ignore[arg-type]
            raise
        try:
            return _after(request, response, unit)  # type: ignore[arg-type]
        finally:
            unit_of_work.end(unit)  # type: ignore[arg-type]

    # BOTH HALVES OR NEITHER. The wrapper below is the panel and the measuring
    # at once, so the two cannot come apart — but the install itself can still
    # fail (an exotic handler subclass, a read-only instance). If it does,
    # nothing is left installed and the mount reports both halves missing.
    try:
        if callable(original_sync):
            app.get_response = measured_get_response
        if callable(original_async):
            app.get_response_async = measured_get_response_async
        if not callable(original_sync) and not callable(original_async):
            raise RuntimeError(
                "this Django handler exposes neither get_response nor "
                "get_response_async, so there is no request boundary to attach to"
            )
        setattr(app, _MARK, True)
        host_surface.note_panel(wiring, True)
        host_surface.note_measurement(wiring, True)
        # This handler now answers every kit-owned path under the prefix. The
        # ASGI trace middleware answers those paths itself when nobody else
        # does, and it runs first — recorded here so it stays quiet in front of
        # a Django app somebody wrapped by hand as well.
        kit_pages.note_kit_paths_registered(prefix)
    except Exception as exc:  # noqa: BLE001
        reason = "%s: %s" % (type(exc).__name__, exc)
        for attr, original in (
            ("get_response", original_sync),
            ("get_response_async", original_async),
        ):
            try:
                if callable(original):
                    setattr(app, attr, original)
            except Exception:  # noqa: BLE001
                pass
        host_surface.note_panel(wiring, False, reason)
        host_surface.note_measurement(wiring, False, reason)

    host_surface.finish(wiring)
    return app


def _has_header(response: Any, name: str) -> bool:
    try:
        return bool(response.has_header(name))
    except Exception:  # noqa: BLE001
        return False
