"""Streamlit, in one call — the kit's pages AND per-rerun measuring.

    import streamlit as st, boosthis

    boosthis.mount(st)

WHAT A UNIT OF WORK IS HERE
------------------------------------------------------------------
Streamlit has no request a developer thinks in. It has the SCRIPT RERUN: every
widget touched re-executes the script top to bottom, and the wait the user feels
is that re-execution. So a rerun is the unit, measured from Streamlit's own
"script started" to its "script stopped", and labelled ``streamlit.rerun``. A
partial rerun of a single ``@st.fragment`` is a different, much cheaper thing
and is labelled ``streamlit.fragment`` so the two never average together.

WHAT THIS HOST CANNOT TELL US
------------------------------------------------------------------
A rerun has no HTTP status, and Streamlit — not the app author — writes every
response and every cookie. So the response-shaped readings (cookie exposure,
refusal honesty, access pressure) and the event-loop reading (the script runs on
its own worker thread) are reported as NOT MEASURABLE here, with the reason
attached, instead of sitting blank forever. See ``host_surface.UNMEASURABLE``.

HOW IT ATTACHES
------------------------------------------------------------------
``boosthis.mount(st)`` runs inside the script, which is to say after Streamlit's
web server is already up. Both halves therefore attach to the LIVE process:

  * the pages are added to the running Starlette app Streamlit serves from, at
    the front of its route list, so they answer on the app's own port with no
    second server and no extra address to remember;
  * the measuring is connected to Streamlit's own script-runner event signal —
    to every runner alive now, and, via one patched constructor, to every runner
    Streamlit creates afterwards.

Both go in together or neither does: if the pages cannot be added, the measuring
is disconnected again and the mount says so once, in one line, on stderr.
"""

from __future__ import annotations

import gc
from typing import Any, Dict, List, Optional

from . import host_surface
from . import kit_pages
from . import unit_of_work

SURFACE = "streamlit"

#: Streamlit script-runner events that open / close one unit of work.
_START_EVENT = "SCRIPT_STARTED"
_STOP_EVENTS = (
    "SCRIPT_STOPPED_WITH_SUCCESS",
    "SCRIPT_STOPPED_WITH_COMPILE_ERROR",
    "SCRIPT_STOPPED_FOR_RERUN",
    "FRAGMENT_STOPPED_WITH_SUCCESS",
    "SHUTDOWN",
)

#: Open units, keyed by the id of the script runner that opened them. At most
#: one per live session; capped so a runner that dies mid-run cannot grow it.
_open_units: Dict[int, unit_of_work.Unit] = {}
_MAX_OPEN_UNITS = 256

#: What we patched, so it can be undone (rollback, and ``forget()``).
_installed: Dict[str, Any] = {}


def is_streamlit_module(app: Any) -> bool:
    """True for the ``streamlit`` module itself — what ``boosthis.mount(st)``
    passes. Streamlit has no app object to hand us, so the module IS the
    handle."""
    try:
        import types

        return isinstance(app, types.ModuleType) and (
            getattr(app, "__name__", "") == "streamlit"
        )
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Half 1: measuring a rerun
# ---------------------------------------------------------------------------


def _on_script_event(sender: Any, **kwargs: Any) -> None:
    """Open a unit on "script started", close it on any "script stopped".

    This runs on Streamlit's own script thread for EVERY runner event, including
    the one it fires per rendered element, so it does the cheapest possible
    check first and returns."""
    try:
        name = getattr(kwargs.get("event"), "name", "")
        if name == _START_EVENT:
            key = id(sender)
            stale = _open_units.pop(key, None)
            if stale is not None:
                unit_of_work.end(stale)
            if len(_open_units) >= _MAX_OPEN_UNITS:
                return
            label = (
                "streamlit.fragment"
                if kwargs.get("fragment_ids_this_run")
                else "streamlit.rerun"
            )
            _open_units[key] = unit_of_work.begin(label)
        elif name in _STOP_EVENTS:
            unit = _open_units.pop(id(sender), None)
            if unit is not None:
                unit_of_work.end(unit)
    except Exception:  # noqa: BLE001
        pass  # instrumentation must never disturb the host app.


def _connect(runner: Any) -> None:
    """Attach the receiver to one script runner's event signal, once."""
    if getattr(runner, "_boosthis_connected", False):
        return
    runner.on_event.connect(_on_script_event, weak=False)
    setattr(runner, "_boosthis_connected", True)


def _install_measuring(live_runners: List[Any]) -> None:
    """Measure every rerun from now on — including in the session that is
    running this very script.

    Two steps, because a Streamlit script only ever executes inside an
    already-created runner: connect to the runners alive right now, and patch
    the constructor so every runner created later is connected as well. Raises
    on failure; the caller reports it and installs no pages."""
    from streamlit.runtime.scriptrunner import script_runner as _sr

    original_init = _sr.ScriptRunner.__init__

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        try:
            _connect(self)
        except Exception:  # noqa: BLE001
            pass

    _sr.ScriptRunner.__init__ = patched_init  # type: ignore[method-assign]
    _installed["script_runner_class"] = _sr.ScriptRunner
    _installed["script_runner_init"] = original_init
    connected: List[Any] = []
    for runner in live_runners:
        try:
            _connect(runner)
            connected.append(runner)
        except Exception:  # noqa: BLE001
            continue
    _installed["connected_runners"] = connected


def _uninstall_measuring() -> None:
    """Undo :func:`_install_measuring` completely."""
    cls = _installed.pop("script_runner_class", None)
    original_init = _installed.pop("script_runner_init", None)
    if cls is not None and original_init is not None:
        try:
            cls.__init__ = original_init
        except Exception:  # noqa: BLE001
            pass
    for runner in _installed.pop("connected_runners", []) or []:
        try:
            runner.on_event.disconnect(_on_script_event)
            setattr(runner, "_boosthis_connected", False)
        except Exception:  # noqa: BLE001
            continue
    _open_units.clear()


# ---------------------------------------------------------------------------
# Half 2: the kit's pages, on Streamlit's own server
# ---------------------------------------------------------------------------


def _install_pages(app: Any, prefix: str) -> None:
    """Add the kit's routes to the front of the running Starlette app.

    One catch-all under the prefix rather than a route per page: the route table
    already lives in :mod:`boosthis.kit_pages`, and a single entry keeps the
    footprint on someone else's app to exactly one object. Raises on failure."""
    from starlette.responses import Response  # type: ignore[import-not-found]
    from starlette.routing import Route  # type: ignore[import-not-found]

    base = prefix or ""

    def _safe(kind: str, fn: Any) -> Any:
        """Wrap ONE kit-owned route so a throw inside it never reaches the host.

        Same shape as the FastAPI/Starlette adapters in :mod:`boosthis.mount`.
        Streamlit hands an unhandled exception to its own error middleware, so
        a hiccup in kit code would render Streamlit's error page to somebody
        using the developer's app — their screen, our bug. Catch it here, and
        still answer in the shape the caller expects.
        """

        async def _wrapped(request: Any) -> Any:
            try:
                return await fn(request)
            except Exception:  # noqa: BLE001
                if kind == "html":
                    return Response(
                        content=kit_pages.KIT_ERROR_HTML,
                        status_code=500,
                        media_type="text/html; charset=utf-8",
                        headers={"cache-control": "no-store"},
                    )
                return Response(
                    content=kit_pages.KIT_ERROR_JSON,
                    status_code=500,
                    media_type="application/json",
                    headers={"cache-control": "no-store"},
                )

        return _wrapped

    async def _endpoint(request: Any) -> Any:
        try:
            path = str(request.url.path or "")
            suffix = path[len(base) :] if base and path.startswith(base) else path
            body = None
            if str(request.method or "GET").upper() == "POST":
                body = await request.body()
            reply = kit_pages.handle(
                suffix,
                method=str(request.method or "GET"),
                remote_addr=request.client.host if request.client else None,
                headers=dict(request.headers),
                query=dict(request.query_params),
                body=body,
            )
        except Exception:  # noqa: BLE001
            reply = None
        if reply is None:
            return Response(status_code=404)
        return Response(
            content=reply.body,
            status_code=reply.status,
            media_type=reply.content_type,
            headers=reply.headers,
        )

    safe_endpoint = _safe("html", _endpoint)
    routes = [
        Route(base or "/", safe_endpoint, methods=["GET", "POST"]),
        Route((base or "") + "/{rest:path}", safe_endpoint, methods=["GET", "POST"]),
    ]
    app.router.routes[:0] = routes
    _installed["app"] = app
    _installed["routes"] = routes


def _uninstall_pages() -> None:
    app = _installed.pop("app", None)
    routes = _installed.pop("routes", []) or []
    if app is None:
        return
    for route in routes:
        try:
            app.router.routes.remove(route)
        except Exception:  # noqa: BLE001
            continue


# ---------------------------------------------------------------------------
# Mount
# ---------------------------------------------------------------------------


def _find_live_objects() -> Dict[str, Any]:
    """One pass over live objects to find Streamlit's server app and its script
    runners.

    Streamlit keeps neither in a module global — the server is a local variable
    inside its bootstrap, and runners belong to sessions — so there is nothing
    to import. This runs exactly once, at mount."""
    found: Dict[str, Any] = {"app": None, "runners": []}
    try:
        from starlette.applications import Starlette  # type: ignore[import-not-found]
        from streamlit.runtime.scriptrunner import script_runner as _sr
    except Exception:  # noqa: BLE001
        return found
    for obj in gc.get_objects():
        try:
            if found["app"] is None and isinstance(obj, Starlette):
                found["app"] = obj
            elif isinstance(obj, _sr.ScriptRunner):
                found["runners"].append(obj)
        except Exception:  # noqa: BLE001
            continue
    return found


def mount_streamlit(module: Any, prefix: str, bubble: Optional[bool] = None) -> Any:
    """Install BOTH halves into a running Streamlit app and return the module.

    ``bubble`` is accepted for signature parity and deliberately unused: the
    Streamlit UI is rendered by Streamlit's own front end, not by HTML the kit
    can inject into, so the floating badge cannot exist here. The kit's page is
    served instead, at the mount prefix."""
    wiring = host_surface.begin(SURFACE, unit="rerun")

    if _installed.get("mounted"):
        host_surface.note_panel(wiring, True)
        host_surface.note_measurement(wiring, True)
        host_surface.finish(wiring)
        return module

    live = _find_live_objects()
    measuring = False
    try:
        _install_measuring(live["runners"])
        measuring = True
        app = live["app"]
        if app is None:
            raise RuntimeError(
                "the running Streamlit server could not be found, so the "
                "Boosthis page has nowhere to be served from — call "
                "boosthis.mount(st) from the Streamlit script itself, under "
                "`streamlit run`"
            )
        _install_pages(app, prefix)
        _installed["mounted"] = True
        host_surface.note_panel(wiring, True)
        host_surface.note_measurement(wiring, True)
        # The kit's paths are served under this prefix now. The ASGI trace
        # middleware answers them itself when no mount did, and Streamlit's
        # server is an ASGI app somebody could have wrapped by hand — record the
        # base so that door does not shadow the route just installed.
        kit_pages.note_kit_paths_registered(prefix)
    except Exception as exc:  # noqa: BLE001
        reason = "%s: %s" % (type(exc).__name__, exc)
        # Never leave the app half-wired: if the pages could not go in, take the
        # measuring back out too.
        if measuring:
            _uninstall_measuring()
        _uninstall_pages()
        host_surface.note_panel(wiring, False, reason)
        host_surface.note_measurement(wiring, False, reason)

    host_surface.finish(wiring)
    return module


def uninstall() -> None:
    """Detach everything this adapter installed (used by ``forget()`` and by
    the tests that mount more than one framework in a process)."""
    _uninstall_pages()
    _uninstall_measuring()
    _installed.pop("mounted", None)
