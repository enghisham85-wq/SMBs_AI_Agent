"""Mount the Boosthis pages into a user's existing web app.

Friendliest possible Python integration: instead of asking the developer
to start a second server (``boosthis serve``), they add **one line** next
to the app they already have — on any of the frameworks listed in
:data:`SUPPORTED_FRAMEWORKS` — and the page shows up at ``/_boosthis`` on
the same port their app already uses.

Two pages, not one:

    ``/_boosthis``      The in-app page every Boosthis kit serves — the same
                        markup in every language, so a developer moving
                        between services always meets the same screen. It is
                        generated from ``lib/kit-page/panel_page.html``; never
                        hand-edit this package's copy. Served UNGATED, exactly
                        like the ``/panel`` read it draws its numbers from.

    ``/_boosthis/dev``  This kit's own richer view — per-route table, learned
                        budgets, recent samples, the rule checklist and the
                        "Connect your AI" card. Loopback-only, like every
                        other read below it.

Usage:
    import boosthis
    app = FastAPI()  # or Flask(__name__), or Starlette(...)
    boosthis.mount(app)            # → http://your-app/_boosthis
    boosthis.mount(app, "/perf")   # → http://your-app/perf

Auto-detects the framework from a CLOSED list — the one in
:data:`SUPPORTED_FRAMEWORKS`, and nothing else. A plain WSGI or ASGI
callable is not detected and cannot be mounted. No extra dependencies:
the detection reads the app object's own module path and uses the
framework the app is already using.

Anything else raises a :class:`TypeError` that names what it was handed,
names the frameworks that do work, and points at ``track_perf`` / ``perf``
— which measure any Python program and need no adapter. It does NOT offer
``boosthis serve`` as a way to attach: that command starts a separate
Boosthis-only server for reading what has already been recorded, and it
never measures the host app.

Privacy / security:
    - Everything served is the SAME data the in-process tracker already
      holds. Nothing new is recorded; nothing is uploaded.
    - Read-only by design: GET endpoints expose rules + recent samples
      + summary; the only POST is ``/match`` which is a pure function
      over a code snippet the developer themselves sent in. The snippet
      is matched against keyword hints and discarded — it isn't stored
      and it isn't transmitted. No write paths into the sample buffer
      (the tracker is the only writer).
    - We do NOT add CORS headers. The dashboard is meant to be opened
      in the same browser tab as the host app, so it lives on the same
      origin and doesn't need them.
    - Localhost-only by default: all routes return 403 for any request
      whose remote address is not a loopback address. Set the
      environment variable ``BOOSTHIS_MOUNT_ALLOW_REMOTE=1`` to disable
      this guard (e.g. when running inside a container where the app
      itself is already behind an authenticated reverse proxy).
"""

from __future__ import annotations

import functools
import json
import os
import re
from pathlib import Path
from typing import Any

from boosthis import samples
from boosthis.bubble import (
    MAX_INJECT_BYTES,
    bubble_snippet,
    inject_into_html,
    panel_body,
    pulse_body,
    resolve_bubble_visibility,
)
from boosthis.checklist import BOOSTHIS_CHECKLIST, get_checklist_entry
from boosthis.community import fetch_rule_fix
from boosthis import host_surface
from boosthis import kit_pages
from boosthis.context import build_context, match_rules_for_code
from boosthis.runtime_vitals import note_request
from boosthis.project_key import resolve_project_key
from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.start_announce import (
    announce_unreadable_sources,
    announce_kit_start,
    begin_start_announcement,
    flush_held_refusals,
)
from boosthis.status_page import (
    STATUS_FORBIDDEN_HTML,
    STATUS_SUFFIX,
    status_page_html,
)

DEFAULT_PREFIX = "/_boosthis"

# ---------------------------------------------------------------------------
# WHAT mount() ACTUALLY ATTACHES TO — stated ONCE, here
# ---------------------------------------------------------------------------
#
# Every sentence that describes the supported set is built from this tuple:
# this module's docstring, ``mount()``'s own docstring, the TypeError raised
# when detection fails, `boosthis init`'s printed hint — and, via a guard in
# the hosted server, the served Python install guide.
#
# They used to be four hand-written copies, and they disagreed. The served
# guide offered "Flask (or any WSGI)" while ``_detect()`` below has only ever
# matched the six names here, so a developer with a plain WSGI app was
# promised support in writing and refused at runtime. One list, or it happens
# again.
#
# Each entry is (framework name, what to hand mount()). The second half is
# empty where the app object is the obvious one.
SUPPORTED_FRAMEWORKS: tuple[tuple[str, str], ...] = (
    ("FastAPI", ""),
    ("Starlette", ""),
    ("Flask", ""),
    ("Django", "pass the WSGI/ASGI handler"),
    ("Streamlit", "pass the streamlit module"),
    ("Gradio", "pass the Blocks/Interface object"),
)

#: Just the names, in the same order.
SUPPORTED_FRAMEWORK_NAMES: tuple[str, ...] = tuple(
    name for name, _hint in SUPPORTED_FRAMEWORKS
)


def supported_frameworks_sentence(with_hints: bool = True) -> str:
    """The supported set as one prose list, e.g. ``"FastAPI, Starlette, Flask,
    Django (pass the WSGI/ASGI handler), … and Gradio (…)"``.

    Anything that tells a developer which frameworks work calls this rather
    than typing the list again.
    """
    parts = [
        f"{name} ({hint})" if with_hints and hint else name
        for name, hint in SUPPORTED_FRAMEWORKS
    ]
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


# This kit's own richer, loopback-only view (per-route table, learned budgets,
# recent samples, the rule checklist and the "Connect your AI" card).
_DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"
# The canonical in-app page — byte-identical in every Boosthis kit, written
# here by scripts/build-kit-page.mjs. DO NOT EDIT panel_page.html in this
# package: edit lib/kit-page/panel_page.html and re-run that script, or the
# parity guard fails.
_PANEL_PAGE_HTML = Path(__file__).parent / "panel_page.html"

# PEP 563 is on for this module (``from __future__ import annotations``), so
# FastAPI resolves each route's annotations as STRINGS against these MODULE
# globals. A ``Request`` imported inside ``_mount_fastapi()`` is invisible to
# that lookup, and FastAPI then treats ``request`` as an ordinary query
# parameter — every mounted route answers 422 instead of serving. The real
# class is published here by ``_mount_fastapi()`` before it declares a route;
# Starlette is deliberately NOT imported eagerly, so Flask-only hosts never pay
# for a dependency they don't use.
Request: Any = Any

_LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {"127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1", "::ffff:127.0.0.1"}
)

# Headers injected by reverse proxies.  Their presence on an otherwise-loopback
# connection means the real caller is not local — the proxy is.  We deny the
# request in that case regardless of peer address.
_PROXY_HEADERS: frozenset[str] = frozenset(
    {
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
        "forwarded",
    }
)


def _remote_access_allowed() -> bool:
    """Return True when the operator has explicitly opted in to remote access."""
    v = os.environ.get("BOOSTHIS_MOUNT_ALLOW_REMOTE") or os.environ.get(
        "BOOSTEN_MOUNT_ALLOW_REMOTE", ""
    )
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _is_local_host(host: str | None) -> bool:
    """Return True if *host* is a loopback address."""
    if host is None:
        return False
    return host.strip().lower() in _LOOPBACK_HOSTS


def _has_proxy_headers(headers: Any) -> bool:
    """Return True if any well-known reverse-proxy forwarding header is present.

    When a proxy sits between the internet and the app it typically forwards
    traffic over a loopback connection *and* injects one of these headers so
    the app knows the real client address.  If we see any of them we treat the
    connection as proxied and deny it — the peer address alone is not a safe
    locality signal in that topology.

    ``headers`` may be a dict, a Starlette/FastAPI ``Headers`` object, or any
    other mapping whose keys are header names (case-insensitive lookup is
    performed by lower-casing the key set).
    """
    try:
        header_keys = {k.lower() for k in headers}
    except Exception:  # noqa: BLE001
        return False
    return bool(header_keys & _PROXY_HEADERS)


def _local_request_allowed(host: str | None, headers: Any = None) -> bool:
    """Return True when the request should be allowed through the guard.

    Grants access only when ALL of the following are true (unless the operator
    has set ``BOOSTHIS_MOUNT_ALLOW_REMOTE=1``):

    1. The immediate TCP peer is a loopback address.
    2. No reverse-proxy forwarding headers are present.

    Condition 2 guards against the common topology where nginx / Caddy /
    Traefik connects to the app over ``127.0.0.1`` on behalf of a public
    caller.  In that case the peer address looks local, but the proxy headers
    reveal that the real origin is external.
    """
    if _remote_access_allowed():
        return True
    if not _is_local_host(host):
        return False
    if headers is not None and _has_proxy_headers(headers):
        return False
    return True


def _strict_loopback_allowed(host: str | None, headers: Any = None) -> bool:
    """Loopback-only guard that IGNORES ``BOOSTHIS_MOUNT_ALLOW_REMOTE``.

    Used by ``/connect`` only, which emits a read-only CREDENTIAL. Unlike the
    perf-data endpoints (which the operator may open with the env escape hatch
    when running behind an authenticated proxy), the connect card must never be
    reachable off-box — mirroring the Node runtime's always-loopback
    ``/connect`` route. Requires a loopback peer AND no reverse-proxy headers.
    """
    if not _is_local_host(host):
        return False
    if headers is not None and _has_proxy_headers(headers):
        return False
    return True


def build_connect_payload(cfg: Any) -> dict[str, Any]:
    """Build the paste-ready hosted-MCP "Connect your AI" payload.

    Returns ``{"connected": False}`` until a read token has been issued (fresh
    consent or backfill). The install id + read token are surfaced so the
    developer can hand them to their OWN AI as per-call tool arguments; the
    config JSON deliberately does NOT bake them in (the hosted server holds no
    per-install creds in module state — the cross-tenant guarantee). The invite
    key is a ``<YOUR_PROJECT_KEY>`` placeholder: the runtime never stores it in a
    readable form. Mirrors Node's ``buildConnectPayload`` + the RN card.
    """
    if cfg is None or not getattr(cfg, "enabled", False) or not getattr(cfg, "read_token", None):
        return {"connected": False}
    endpoint = getattr(cfg, "endpoint", "") or ""
    # Hosted MCP lives next to the API (…/api → …/mcp), mirroring the RN card.
    mcp_url = re.sub(r"/api/?$", "", endpoint) + "/mcp"
    mcp_config = "\n".join(
        [
            "{",
            '  "mcpServers": {',
            '    "boosthis": {',
            f'      "url": "{mcp_url}",',
            '      "headers": { "Authorization": "Bearer <YOUR_PROJECT_KEY>" }',
            "    }",
            "  }",
            "}",
        ]
    )
    return {
        "connected": True,
        "install_id": cfg.install_id,
        "read_token": cfg.read_token,
        "mcp_url": mcp_url,
        "mcp_config": mcp_config,
    }


def _h_connect() -> Any:
    """Return the connect payload from the on-disk telemetry config.

    Lazy-imports ``telemetry`` so ``import boosthis`` stays side-effect-free
    and cheap for host apps that never mount the dashboard.
    """
    from boosthis import telemetry as tm

    return build_connect_payload(tm.get_config())


def _h_account() -> Any:
    """Return the account auth-context payload for the floating bubble's
    account card. Emits the install DELETE TOKEN (used to claim/link the
    install), so callers MUST gate this behind the strict loopback guard.

    Returns ``{endpoint, installId, deleteToken, fullTelemetry}`` when telemetry
    is enabled, else all keys ``None``/``False``. The camelCase keys mirror
    Node's ``/account`` shape the bubble IIFE reads; ``fullTelemetry`` surfaces
    the install's current server-side "Full telemetry" override so the account
    card can seed its toggle. Lazy-imports ``telemetry`` to keep
    ``import boosthis`` side-effect-free.
    """
    from boosthis import telemetry as tm

    cfg = tm.get_config()
    if cfg is None or not getattr(cfg, "enabled", False):
        return {
            "endpoint": None,
            "installId": None,
            "deleteToken": None,
            "fullTelemetry": False,
        }
    return {
        "endpoint": getattr(cfg, "endpoint", None) or None,
        "installId": getattr(cfg, "install_id", None) or None,
        "deleteToken": getattr(cfg, "delete_token", None) or None,
        # Current dashboard "Full telemetry" override for this install.
        "fullTelemetry": bool(getattr(cfg, "server_full", False)),
    }


def _read_dashboard_html() -> str:
    """Read this kit's detailed local view on every request rather than at
    import time. Avoids stale-on-disk caching during local development and
    keeps the import side-effect-free — important since ``boosthis`` is
    imported extremely early in many host apps."""
    try:
        return _DASHBOARD_HTML.read_text(encoding="utf-8")
    except OSError:
        return (
            "<!doctype html><h1>Boosthis</h1>"
            "<p>dashboard.html missing — reinstall the boosthis package.</p>"
        )


def _read_panel_page_html() -> str:
    """Read the canonical in-app page — the ONE page every Boosthis kit serves.
    Read per request for the same reasons as the view above. The fallback still
    tells the developer the truth: the kit is running, the packed page file is
    not."""
    try:
        return _PANEL_PAGE_HTML.read_text(encoding="utf-8")
    except OSError:
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            "<title>Boosthis</title></head>"
            '<body style="background:#0b0c10;color:#e6e7eb;'
            'font-family:system-ui,sans-serif;padding:40px">'
            "<h1>Boosthis is running</h1>"
            "<p>This install is measuring, but the page file is missing from "
            "the package. Reinstall the kit to restore it.</p></body></html>"
        )


# ---------------------------------------------------------------------------
# Shared handlers — return plain Python data, NOT framework-specific objects.
# The framework adapters below wrap these into framework-native responses.
# ---------------------------------------------------------------------------

def _h_context(limit: int = 25) -> Any:
    return build_context(sample_limit=limit)


def _h_rules() -> Any:
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "category": r["category"],
            "languages": r["languages"],
        }
        for r in BOOSTHIS_CHECKLIST
    ]


def _h_rule(rule_id: str) -> tuple[int, Any]:
    rule = get_checklist_entry(rule_id)
    if rule is None:
        return 404, {"error": "rule not found", "id": rule_id}
    # Detection metadata is local; the prescriptive fix is fetched per-rule
    # from the server (invite-key gated). On any failure the merged result
    # carries fix_available:false + a note, so detection still works offline.
    fix = fetch_rule_fix(rule_id, "py")
    return 200, {**rule, **fix}


def _h_samples(limit: int = 100, name: str | None = None) -> Any:
    return [s.to_dict() for s in samples.recent(limit=limit, name=name)]


def _h_summary() -> Any:
    from boosthis.health_axes import compute_dashboard_axes

    s = samples.summary()
    return {**s, "axes": compute_dashboard_axes(s)}


def _h_match(body: Any) -> tuple[int, Any]:
    if not isinstance(body, dict):
        return 400, {"error": "expected JSON object"}
    snippet = str(body.get("code") or "")
    lang = str(body.get("language") or "python")
    if not snippet:
        return 400, {"error": "missing 'code' field"}
    # No PII guard here on purpose: the guard inspects field NAMES, and
    # the only field we'd hand it is `code`, which isn't in the denylist.
    # The endpoint is read-only — it returns rule IDs, it doesn't store
    # or transmit the snippet anywhere. So adding an `assert_no_pii`
    # call would be misleading theatre. The snippet stays in-process.
    return 200, match_rules_for_code(snippet, language=lang)


_FORBIDDEN_BODY = json.dumps(
    {
        "error": "forbidden",
        "detail": (
            "The Boosthis dashboard is only accessible from localhost. "
            "Set BOOSTHIS_MOUNT_ALLOW_REMOTE=1 if you are running inside "
            "a container behind an authenticated reverse proxy."
        ),
    }
).encode()

# The /connect card emits a read token, so its 403 message is credential-
# specific and does NOT mention the remote escape hatch (which never opens it).
_CONNECT_FORBIDDEN_BODY = json.dumps(
    {
        "error": (
            "boosthis /connect is loopback-only — it emits a read token. "
            "Copy the credentials from a local session."
        ),
    }
).encode()

# The /account card emits an install DELETE TOKEN, so — like /connect — it is
# ALWAYS loopback-only and its 403 message is credential-specific (the remote
# escape hatch never opens it). The delete token is NEVER surfaced through the
# ungated /panel or /pulse reads — only here, behind the strict loopback gate.
_ACCOUNT_FORBIDDEN_BODY = json.dumps(
    {
        "error": (
            "boosthis /account is loopback-only — it emits an install token. "
            "Open this page on the dev machine to sign in."
        ),
    }
).encode()


# ---------------------------------------------------------------------------
# Guest safety: a throw inside kit code must never reach the host's handler
# ---------------------------------------------------------------------------
#
# The kit is a guest in someone else's application. It may watch; it may never
# intervene. An unhandled exception inside one of OUR route handlers is the
# host framework's cue to run the HOST's error handling — so a hiccup in kit
# code shows the customer's own visitor the customer's error page, or our
# traceback on their site while they are developing.
#
# Same shape the Go kit uses (``defer recover()`` at the top of every serve
# function), with one addition: we still answer, in the shape this route's
# caller expects — the kit's own page for an HTML route, a JSON body for a
# JSON read — so the browser or the badge gets a well-formed reply instead of
# the host's 500.
#
# Both bodies below are pre-rendered constants. The apology has nothing to look
# up and no state to read, so it cannot fail the way the view it replaces just
# did.

# Defined once in the framework-free page module, so every adapter — these
# three and the Django/Streamlit/Gradio ones — apologises in the same words.
_KIT_ERROR_HTML = kit_pages.KIT_ERROR_HTML
_KIT_ERROR_JSON = kit_pages.KIT_ERROR_JSON


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def mount(app: Any, prefix: str = DEFAULT_PREFIX, bubble: bool | None = None) -> Any:
    """Mount the Boosthis dashboard onto ``app`` at ``prefix``.

    Returns ``app`` for chaining. Raises :class:`TypeError` if ``app`` is not
    one of the frameworks this kit attaches to. Supported today — the list in
    :data:`SUPPORTED_FRAMEWORKS`, and only this list: FastAPI, Starlette,
    Flask, Django (pass the WSGI/ASGI handler), Streamlit (pass the
    ``streamlit`` module) and Gradio (pass the ``Blocks``/``Interface``).
    A plain WSGI or ASGI callable is NOT one of them; measure such an app
    with :func:`boosthis.track_perf` / :func:`boosthis.perf`, which need no
    adapter. The TypeError says all of this at the point of failure.

    Every one of them installs BOTH halves — the kit's own pages and badge, and
    the measuring of the host's own unit of work — in this one call. They can
    never come apart: if either half cannot go in, neither is left installed and
    the kit says so once, on stderr, naming the missing half and the cause.

    All mounted routes are localhost-only by default. Requests from
    non-loopback addresses receive a 403. Set the environment variable
    ``BOOSTHIS_MOUNT_ALLOW_REMOTE=1`` to disable this guard when running
    inside a container that is already protected by an authenticated
    reverse proxy.

    The floating **bubble** — a small movable badge showing the app's
    live page pulse (one score, one rating, one request count) — is injected
    into outgoing HTML pages by default, on every deployment (live/published
    sites included). The badge polls a closed, coarse ``<prefix>/pulse``
    endpoint (always mounted, ungated) and never transmits anything off the
    app's own origin. Visibility precedence (first match wins):
    ``BOOSTHIS_DISABLED`` (always hidden) → ``BOOSTHIS_BUBBLE`` env
    (tri-state) → truthy ``BOOSTHIS_NO_BUBBLE`` → truthy
    ``BOOSTHIS_FORCE_BUBBLE`` → the ``bubble`` argument → default VISIBLE.
    """
    begin_start_announcement()
    try:
        from boosthis.request_error_timer_meters import install_hooks
        install_hooks()
    except Exception:  # noqa: BLE001
        pass
    try:
        try:
            disabled = is_boosthis_disabled()
        except Exception:  # noqa: BLE001
            disabled = True
        try:
            badge_state = "switched-off" if disabled else (
                "visible" if resolve_bubble_visibility(bubble) else "hidden-by-setting"
            )
        except Exception:  # noqa: BLE001
            badge_state = "visible"
        announce_kit_start(resolve_project_key().key, badge_state)
        announce_unreadable_sources()
    except BaseException:  # evidence must never block the host
        pass
    finally:
        flush_held_refusals()
    if not prefix.startswith("/"):
        raise ValueError("prefix must start with '/'")
    # Normalize: store without trailing slash, add it where needed.
    prefix = prefix.rstrip("/") or "/"
    effective_prefix = "" if prefix == "/" else prefix

    framework = _detect(app)
    # Hand the app object to the devPosture debugFlag probe (read-only): a
    # Flask/Starlette/FastAPI app's ``.debug`` attribute is the one dev-mode
    # signal the kit can read from the mounted object. Best-effort; a failure
    # here must never break mount().
    try:
        from boosthis import extra_meters

        extra_meters.set_mounted_app(app)
    except Exception:  # noqa: BLE001
        pass
    # Hand the same app object to the route inventory, so a snapshot can ask
    # the framework for its WHOLE route table and the map can draw the routes
    # nobody has opened yet. A reference assignment only — nothing is walked
    # here, so mount() costs no more than it did.
    try:
        from boosthis import route_inventory

        route_inventory.register_app(app, effective_prefix)
    except Exception:  # noqa: BLE001
        pass
    if framework == "fastapi":
        return _mount_fastapi(app, effective_prefix, bubble)
    if framework == "starlette":
        return _mount_starlette(app, effective_prefix, bubble)
    if framework == "flask":
        return _mount_flask(app, effective_prefix, bubble)
    if framework == "django":
        from boosthis.adapter_django import mount_django

        return mount_django(app, effective_prefix, bubble)
    if framework == "gradio":
        from boosthis.adapter_gradio import mount_gradio

        return mount_gradio(app, effective_prefix, bubble)
    if framework == "streamlit":
        from boosthis.adapter_streamlit import mount_streamlit

        return mount_streamlit(app, effective_prefix, bubble)
    raise TypeError(_unsupported_app_message(app))


def _unsupported_app_message(app: Any) -> str:
    """The error a developer reads when ``mount()`` cannot attach.

    It has to answer three questions, because the wording before it answered
    only the second and cost an outside developer several attempts: what did
    you hand me, what would I accept, and what do I do now.

    The last one is where the old text failed. It offered ``boosthis serve``
    as a "fallback" without saying what it was a fallback FOR — and that
    command runs a SEPARATE Boosthis-only server that measures nothing of the
    host app, so a reader who took it as the way forward spent a cycle finding
    out it was not. The way forward for an unsupported framework is
    ``track_perf`` / ``perf``, which need no adapter, so that is what this
    text names first and what it names as the remedy.
    """
    try:
        cls = type(app)
        given = f"{cls.__module__}.{cls.__qualname__}"
    except Exception:  # noqa: BLE001
        given = "an object whose type could not be read"
    return (
        f"boosthis.mount() cannot attach to {given}. mount() attaches to "
        f"{supported_frameworks_sentence()} — those, by name, and nothing "
        "else: a plain WSGI or ASGI callable, or any other framework, is not "
        "detected and cannot be mounted.\n"
        "\n"
        "To measure THIS app, use the decorator or the context manager. They "
        "need no framework support and record the same timings mount() would, "
        "for whichever work you name:\n"
        "    from boosthis import track_perf, perf\n"
        "\n"
        "    @track_perf('checkout')\n"
        "    def checkout(...): ...\n"
        "\n"
        "    with perf('db.query'):\n"
        "        run_query()\n"
        "\n"
        "What you lose without a mount is the kit's own in-app page and the "
        "automatic per-request timing; what you keep is every reading those "
        "named blocks produce, uploaded and scored exactly the same way.\n"
        "\n"
        "`boosthis serve` is NOT a way to attach to this app. It starts a "
        "separate Boosthis-only server on its own port that displays what has "
        "already been recorded on this machine: it does not host your app, "
        "sees none of its requests, and measures nothing by itself."
    )


def _detect(app: Any) -> str | None:
    """Identify the framework without importing it. We never import the
    framework directly — if the host hasn't installed it, importing here
    would fail; the class's module path is enough.

    Strict prefix match per MRO entry — substring matching on a
    flattened string is too loose (e.g. ``notfastapi.adapters`` would
    have matched the literal ``"fastapi."``).

    ORDER MATTERS. The web-framework loop runs FIRST because a Gradio demo's
    served app is a FastAPI subclass: handing that object to the Gradio adapter
    instead of the FastAPI one would wire the wrong half. Only the Blocks object
    a developer writes their demo in resolves as ``gradio``.
    """
    for cls in type(app).__mro__:
        module = (cls.__module__ or "").lower()
        # Exact module name OR a real top-level prefix (`fastapi.X`),
        # never a substring like `notfastapi.X`.
        if module == "fastapi" or module.startswith("fastapi."):
            return "fastapi"
        if module == "starlette" or module.startswith("starlette."):
            return "starlette"
        if module == "flask" or module.startswith("flask."):
            return "flask"
    from boosthis import adapter_django, adapter_gradio, adapter_streamlit

    if adapter_django.is_django_handler(app):
        return "django"
    if adapter_gradio.is_gradio_blocks(app):
        return "gradio"
    # Streamlit hands us no app object at all — `boosthis.mount(st)` passes the
    # module, because the script IS the app.
    if adapter_streamlit.is_streamlit_module(app):
        return "streamlit"
    return None


# ---------------------------------------------------------------------------
# Bubble injection helpers
# ---------------------------------------------------------------------------
#
# The floating bubble is a small <script> inserted just before the last
# </body> of outgoing HTML pages. Every step is fail-open by construction:
# any doubt (non-HTML, compressed, over the size cap, no </body>, a bad decode,
# or any raised error) serves the ORIGINAL bytes untouched. The visibility is
# re-checked per request inside the injector so ``BOOSTHIS_DISABLED`` set after
# mount() still hides the badge (the kill-switch always wins).


def _install_asgi_bubble(app: Any, snippet: str, bubble: bool | None) -> None:
    """Install a fail-open HTML-injection middleware on a FastAPI/Starlette app.

    Buffers eligible HTML responses (2xx, ``text/html``, no
    ``Content-Encoding``, under the size cap) and inserts the bubble snippet
    before the closing ``</body>``. Multi-valued headers (e.g. ``set-cookie``)
    are preserved verbatim; only ``content-length``/``content-type`` are
    reset on the rebuilt response. Import/registration failures are swallowed
    so a missing Starlette or an already-started app never breaks mount().
    """
    try:
        from starlette.middleware.base import BaseHTTPMiddleware  # type: ignore[import-not-found]
        from starlette.responses import Response  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return

    async def _dispatch(request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        # Cheap eligibility checks — safe to bail to the untouched response
        # because we have NOT consumed the body iterator yet.
        try:
            if not resolve_bubble_visibility(bubble):
                return response
            status = getattr(response, "status_code", 200) or 200
            if not (200 <= status < 300):
                return response
            headers = response.headers
            ct = headers.get("content-type", "") or ""
            if "text/html" not in ct.lower():
                return response
            if headers.get("content-encoding"):
                return response
            clen = headers.get("content-length")
            if clen is not None and str(clen).isdigit() and int(clen) > MAX_INJECT_BYTES:
                return response
            if not hasattr(response, "body_iterator"):
                return response
        except Exception:  # noqa: BLE001
            return response

        # From here we MUST consume the iterator; the original response body is
        # gone once drained, so we always return a freshly built Response.
        body = b""
        try:
            over = False
            async for chunk in response.body_iterator:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                elif not isinstance(chunk, (bytes, bytearray)):
                    chunk = bytes(chunk)
                body += bytes(chunk)
                if len(body) > MAX_INJECT_BYTES:
                    over = True  # keep draining so the full body is returned
            out = body
            if not over:
                try:
                    injected = inject_into_html(body.decode("utf-8"), snippet)
                    if injected is not None:
                        out = injected.encode("utf-8")
                except Exception:  # noqa: BLE001
                    out = body
            new_response = Response(content=out, status_code=status, media_type=ct)
            # Preserve all original headers (including duplicates like
            # set-cookie) except the two the rebuilt Response now owns.
            preserved = [
                (k, v)
                for (k, v) in response.headers.raw
                if k.lower() not in (b"content-length", b"content-type")
            ]
            new_response.raw_headers = preserved + new_response.raw_headers
            return new_response
        except Exception:  # noqa: BLE001
            # Last resort: return whatever we buffered rather than a drained
            # (empty) original response.
            try:
                return Response(content=body, status_code=status)
            except Exception:  # noqa: BLE001
                return response

    try:
        app.add_middleware(BaseHTTPMiddleware, dispatch=_dispatch)
    except Exception:  # noqa: BLE001
        # App already started, or an unusual app object — skip silently.
        return


def _install_flask_bubble(app: Any, snippet: str, bubble: bool | None) -> None:
    """Install a fail-open ``after_request`` HTML injector on a Flask app.

    Registered on the app (not the blueprint) so the badge reaches every HTML
    page the host serves, not just the dashboard routes. Streamed responses
    (``direct_passthrough``) and non-HTML/compressed/oversized bodies are left
    untouched; any raised error serves the original response.
    """

    def _boosthis_inject(response: Any) -> Any:
        try:
            if not resolve_bubble_visibility(bubble):
                return response
            if getattr(response, "direct_passthrough", False):
                return response
            status = getattr(response, "status_code", 200) or 200
            if not (200 <= status < 300):
                return response
            ct = response.headers.get("Content-Type", "") or ""
            if "text/html" not in ct.lower():
                return response
            if response.headers.get("Content-Encoding"):
                return response
            data = response.get_data()
            if len(data) > MAX_INJECT_BYTES:
                return response
            injected = inject_into_html(data.decode("utf-8"), snippet)
            if injected is None:
                return response
            response.set_data(injected.encode("utf-8"))
            return response
        except Exception:  # noqa: BLE001
            return response

    try:
        app.after_request(_boosthis_inject)
    except Exception:  # noqa: BLE001
        return


# ---------------------------------------------------------------------------
# FastAPI adapter
# ---------------------------------------------------------------------------

def _mount_fastapi(app: Any, prefix: str, bubble: bool | None = None) -> Any:
    global Request
    from fastapi import APIRouter  # type: ignore[import-not-found]
    from fastapi import Request as _FastAPIRequest  # type: ignore[import-not-found]

    # Publish the real class as a module global BEFORE any route is declared —
    # see the note next to the placeholder above. Without this every mounted
    # route (dashboard, /pulse, /panel, …) answers HTTP 422.
    Request = _FastAPIRequest
    from fastapi.responses import HTMLResponse, JSONResponse, Response  # type: ignore[import-not-found]

    router = APIRouter()

    def _safe(kind: str) -> Any:
        """Wrap ONE kit-owned route so a throw inside it never reaches the host.

        Without this, an exception in any handler below is handed to FastAPI's
        (and therefore the host app's) exception handling, and the customer's
        visitor sees the customer's error page because OUR code hiccupped.

        The wrapper keeps the wrapped function's signature intact —
        ``functools.wraps`` sets ``__wrapped__``, which is what FastAPI's
        signature reader follows — so dependency injection and the PEP 563
        ``Request`` resolution above are unaffected. Only routes the KIT
        registers are wrapped; the host's own handlers are never touched, so
        their exceptions still reach the host exactly as before.
        """

        def _decorate(fn: Any) -> Any:
            @functools.wraps(fn)
            async def _wrapped(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await fn(*args, **kwargs)
                except Exception:  # noqa: BLE001
                    if kind == "html":
                        return HTMLResponse(
                            _KIT_ERROR_HTML,
                            status_code=500,
                            headers={"cache-control": "no-store"},
                        )
                    return Response(
                        content=_KIT_ERROR_JSON,
                        status_code=500,
                        media_type="application/json",
                    )

            return _wrapped

        return _decorate

    def _guard(request: Request) -> Response | None:
        """Return a 403 Response when the caller is not localhost."""
        host = request.client.host if request.client else None
        if not _local_request_allowed(host, request.headers):
            return Response(content=_FORBIDDEN_BODY, status_code=403, media_type="application/json")
        return None

    # The in-app page, at the prefix itself. Both spellings answer it directly
    # — no redirect — so neither habit 404s, exactly like every other kit.
    # UNGATED, matching the /panel read below that it fetches its numbers from:
    # the markup is static and carries no measurements, no route labels and no
    # credential, so gating it while /panel stays open would protect nothing.
    if prefix:  # a root mount has no bare form to register
        @router.get("", response_class=HTMLResponse, include_in_schema=False)
        @_safe("html")
        async def _page_bare(request: Request) -> Any:
            return HTMLResponse(_read_panel_page_html())

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    @_safe("html")
    async def _page(request: Request) -> Any:
        return HTMLResponse(_read_panel_page_html())

    # This kit's detailed local view — per-route table, learned budgets, recent
    # samples, the rule checklist and the "Connect your AI" card. It renders
    # route labels and a credential, so unlike the page above it keeps the
    # loopback guard, like every JSON read it draws from. Registered WITHOUT a
    # trailing slash so its relative `api/...` fetches resolve under the prefix.
    @router.get("/dev", response_class=HTMLResponse, include_in_schema=False)
    @_safe("html")
    async def _dashboard(request: Request) -> Any:
        if (err := _guard(request)) is not None:
            return err
        return HTMLResponse(_read_dashboard_html())

    @router.get("/api/context", include_in_schema=False)
    @_safe("json")
    async def _context(request: Request, limit: int = 25) -> Any:
        if (err := _guard(request)) is not None:
            return err
        return _h_context(limit=limit)

    @router.get("/api/rules", include_in_schema=False)
    @_safe("json")
    async def _rules(request: Request) -> Any:
        if (err := _guard(request)) is not None:
            return err
        return _h_rules()

    @router.get("/api/rules/{rule_id}", include_in_schema=False)
    @_safe("json")
    async def _rule(request: Request, rule_id: str) -> Any:
        if (err := _guard(request)) is not None:
            return err
        status, body = _h_rule(rule_id)
        return JSONResponse(body, status_code=status)

    @router.get("/api/samples", include_in_schema=False)
    @_safe("json")
    async def _samples(request: Request, limit: int = 100, name: str | None = None) -> Any:
        if (err := _guard(request)) is not None:
            return err
        return _h_samples(limit=limit, name=name)

    @router.get("/api/summary", include_in_schema=False)
    @_safe("json")
    async def _summary(request: Request) -> Any:
        if (err := _guard(request)) is not None:
            return err
        return _h_summary()

    @router.post("/api/match", include_in_schema=False)
    @_safe("json")
    async def _match(request: Request) -> Any:
        if (err := _guard(request)) is not None:
            return err
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        status, payload = _h_match(body)
        return JSONResponse(payload, status_code=status)

    @router.get("/api/connect", include_in_schema=False)
    @_safe("json")
    async def _connect(request: Request) -> Any:
        # /connect emits a read token, so it is ALWAYS loopback-only —
        # BOOSTHIS_MOUNT_ALLOW_REMOTE never opens it (mirrors Node's /connect).
        host = request.client.host if request.client else None
        if not _strict_loopback_allowed(host, request.headers):
            return Response(
                content=_CONNECT_FORBIDDEN_BODY, status_code=403, media_type="application/json"
            )
        return _h_connect()

    @router.get("/account", include_in_schema=False)
    @_safe("json")
    async def _account(request: Request) -> Any:
        # /account emits an install delete token, so it is ALWAYS loopback-only —
        # BOOSTHIS_MOUNT_ALLOW_REMOTE never opens it (mirrors Node's /account).
        # A remote request (even with the escape hatch set) gets 403 with NO
        # token in the body.
        host = request.client.host if request.client else None
        if not _strict_loopback_allowed(host, request.headers):
            return Response(
                content=_ACCOUNT_FORBIDDEN_BODY, status_code=403, media_type="application/json"
            )
        return _h_account()

    @router.get(STATUS_SUFFIX, include_in_schema=False)
    @_safe("html")
    async def _status(request: Request) -> Any:
        # The standalone "is Boosthis working?" page for an API with no UI.
        # STRICT loopback only — the SAME guard /account uses, and
        # BOOSTHIS_MOUNT_ALLOW_REMOTE never opens it (mirrors Node's /status).
        # A remote request gets 403 with NO install detail in the body.
        host = request.client.host if request.client else None
        if not _strict_loopback_allowed(host, request.headers):
            return HTMLResponse(
                STATUS_FORBIDDEN_HTML,
                status_code=403,
                headers={"cache-control": "no-store"},
            )
        return HTMLResponse(status_page_html(), headers={"cache-control": "no-store"})

    # Closed pulse read for the floating bubble — deliberately UNGATED (no
    # loopback guard): the injected badge polls it from any page the app
    # serves (dev previews may be remote), and the shape is closed + coarse
    # ({score, rating, sampleCount} — never route labels, durations, or
    # per-route data).
    @router.get("/pulse", include_in_schema=False)
    @_safe("json")
    async def _pulse(request: Request) -> Any:
        return JSONResponse(pulse_body(), headers={"cache-control": "no-store"})

    @router.get("/panel", include_in_schema=False)
    @_safe("json")
    async def _panel(request: Request) -> Any:
        # Closed panel read for the bubble's dashboard panel — same UNGATED
        # visibility policy as /pulse; the shape carries only scores, ratings,
        # labels, and numeric captions (no routes, no URLs, no raw samples).
        return JSONResponse(panel_body(), headers={"cache-control": "no-store"})

    wiring = host_surface.begin("fastapi", unit="request")
    app.include_router(router, prefix=prefix or "")
    host_surface.note_panel(wiring, True)
    # These routes now serve the whole prefix. The trace middleware installed
    # below runs BEFORE them, and it answers kit-owned paths itself when nobody
    # else does — without this note its honest "that surface needs
    # boosthis.mount(app)" reply would shadow the real page it is about to serve.
    kit_pages.note_kit_paths_registered(prefix)
    # Install the per-request trace middleware so host-request meters are
    # collected for FastAPI apps exactly as they are for Flask/Starlette:
    #   • accessPressure  (10-min bucket gate + 50-response gate)
    #   • idle            (idle-window detection between requests)
    #   • loadDeflection  (concurrent in-flight peak tracking)
    #   • runtime vitals  (reliability, memoryStability, threadHealth,
    #                      memoryPerRequest, crashFree)
    # The middleware is self-guarding: it passes through immediately when the
    # kill switch is active or telemetry is disabled, so there is zero overhead
    # in those states. Must be called before the app starts serving (at module
    # load / startup time), which is exactly when boosthis.mount() is called.
    #
    # This used to be a bare ``except: pass``, which is precisely how an adapter
    # ships a panel with nothing behind it: the routes go in, the measuring
    # silently does not, and the app looks healthy for months. The failure is
    # now REPORTED — see boosthis.host_surface and the parity guard in
    # tests/test_adapter_wiring_parity.py.
    try:
        from .trace import BoosthisTraceMiddleware
        app.add_middleware(BoosthisTraceMiddleware, kit_prefix=prefix)
        host_surface.note_measurement(wiring, True)
    except Exception as exc:  # noqa: BLE001
        # Never break the host app over an instrumentation failure — but never
        # stay quiet about it either.
        host_surface.note_measurement(wiring, False, "%s: %s" % (type(exc).__name__, exc))
    host_surface.finish(wiring)
    # Inject the dev bubble into outgoing HTML only when it should be visible
    # for this process — production (no dev signal) never installs the
    # buffering middleware, so there is zero prod overhead.
    if resolve_bubble_visibility(bubble):
        _install_asgi_bubble(app, bubble_snippet(f"{prefix}/pulse"), bubble)
    return app


# ---------------------------------------------------------------------------
# Starlette adapter
# ---------------------------------------------------------------------------

def _mount_starlette(app: Any, prefix: str, bubble: bool | None = None) -> Any:
    from starlette.responses import (  # type: ignore[import-not-found]
        HTMLResponse,
        JSONResponse,
        Response,
    )
    from starlette.routing import Route  # type: ignore[import-not-found]

    def _safe(kind: str, fn: Any) -> Any:
        """Wrap ONE kit-owned route so a throw inside it never reaches the host.

        Starlette hands an unhandled exception to the host app's error
        middleware, which is how a hiccup in kit code ends up rendering the
        customer's own error page to their visitor. Same shape as the FastAPI
        adapter above and the Go kit's ``defer recover()``: catch it here, and
        still answer in the shape this route's caller expects.
        """

        @functools.wraps(fn)
        async def _wrapped(request: Any) -> Any:
            try:
                return await fn(request)
            except Exception:  # noqa: BLE001
                if kind == "html":
                    return HTMLResponse(
                        _KIT_ERROR_HTML,
                        status_code=500,
                        headers={"cache-control": "no-store"},
                    )
                return Response(
                    content=_KIT_ERROR_JSON,
                    status_code=500,
                    media_type="application/json",
                )

        return _wrapped

    def _guard(request: Any) -> Any | None:
        """Return a 403 JSONResponse when the caller is not localhost."""
        client = getattr(request, "client", None)
        host = client.host if client else None
        headers = getattr(request, "headers", None)
        if not _local_request_allowed(host, headers):
            return JSONResponse(
                {"error": "forbidden", "detail": (
                    "The Boosthis dashboard is only accessible from localhost. "
                    "Set BOOSTHIS_MOUNT_ALLOW_REMOTE=1 if you are running inside "
                    "a container behind an authenticated reverse proxy."
                )},
                status_code=403,
            )
        return None

    async def page(request: Any) -> Any:
        # The in-app page — the ONE page every kit serves. UNGATED, matching the
        # /panel read it fetches its numbers from: static markup, no
        # measurements, no route labels, no credential.
        return HTMLResponse(_read_panel_page_html())

    async def dashboard(request: Any) -> Any:
        # This kit's detailed local view. Renders route labels and a credential,
        # so it keeps the loopback guard every JSON read here has.
        if (err := _guard(request)) is not None:
            return err
        return HTMLResponse(_read_dashboard_html())

    async def ctx(request: Any) -> Any:
        if (err := _guard(request)) is not None:
            return err
        limit = int(request.query_params.get("limit", "25"))
        return JSONResponse(_h_context(limit=limit))

    async def rules(request: Any) -> Any:
        if (err := _guard(request)) is not None:
            return err
        return JSONResponse(_h_rules())

    async def rule(request: Any) -> Any:
        if (err := _guard(request)) is not None:
            return err
        status, body = _h_rule(request.path_params["rule_id"])
        return JSONResponse(body, status_code=status)

    async def samples_route(request: Any) -> Any:
        if (err := _guard(request)) is not None:
            return err
        limit = int(request.query_params.get("limit", "100"))
        name = request.query_params.get("name") or None
        return JSONResponse(_h_samples(limit=limit, name=name))

    async def summary_route(request: Any) -> Any:
        if (err := _guard(request)) is not None:
            return err
        return JSONResponse(_h_summary())

    async def match(request: Any) -> Any:
        if (err := _guard(request)) is not None:
            return err
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        status, payload = _h_match(body)
        return JSONResponse(payload, status_code=status)

    async def connect_route(request: Any) -> Any:
        # /connect emits a read token, so it is ALWAYS loopback-only —
        # BOOSTHIS_MOUNT_ALLOW_REMOTE never opens it (mirrors Node's /connect).
        client = getattr(request, "client", None)
        host = client.host if client else None
        headers = getattr(request, "headers", None)
        if not _strict_loopback_allowed(host, headers):
            return JSONResponse(
                {
                    "error": (
                        "boosthis /connect is loopback-only — it emits a read token. "
                        "Copy the credentials from a local session."
                    )
                },
                status_code=403,
            )
        return JSONResponse(_h_connect())

    async def account_route(request: Any) -> Any:
        # /account emits an install delete token, so it is ALWAYS loopback-only —
        # BOOSTHIS_MOUNT_ALLOW_REMOTE never opens it (mirrors Node's /account).
        client = getattr(request, "client", None)
        host = client.host if client else None
        headers = getattr(request, "headers", None)
        if not _strict_loopback_allowed(host, headers):
            return JSONResponse(
                {
                    "error": (
                        "boosthis /account is loopback-only — it emits an install token. "
                        "Open this page on the dev machine to sign in."
                    )
                },
                status_code=403,
            )
        return JSONResponse(_h_account())

    async def status_route(request: Any) -> Any:
        # The standalone "is Boosthis working?" page for an API with no UI.
        # STRICT loopback only — the SAME guard /account uses, and
        # BOOSTHIS_MOUNT_ALLOW_REMOTE never opens it (mirrors Node's /status).
        # A remote request gets 403 with NO install detail in the body.
        client = getattr(request, "client", None)
        host = client.host if client else None
        headers = getattr(request, "headers", None)
        if not _strict_loopback_allowed(host, headers):
            return HTMLResponse(
                STATUS_FORBIDDEN_HTML,
                status_code=403,
                headers={"cache-control": "no-store"},
            )
        return HTMLResponse(status_page_html(), headers={"cache-control": "no-store"})

    async def panel_route(request: Any) -> Any:
        # Closed panel read for the bubble's dashboard panel — same UNGATED
        # policy as /pulse (scores/ratings/captions only, never routes/URLs).
        return JSONResponse(panel_body(), headers={"cache-control": "no-store"})

    async def pulse_route(request: Any) -> Any:
        # Closed pulse read for the floating bubble — deliberately UNGATED (no
        # loopback guard): the injected badge polls it from any page the app
        # serves, and the shape is closed + coarse ({score, rating,
        # sampleCount} — never route labels, durations, or per-route data).
        return JSONResponse(pulse_body(), headers={"cache-control": "no-store"})

    p = prefix or ""
    routes = []
    # Every handler goes on the wire through _safe(): a throw inside kit code
    # answers the kit's own page (or JSON), never the host's error page.
    safe_page = _safe("html", page)
    # Both spellings of the prefix answer the in-app page directly — no
    # redirect — so neither habit 404s, exactly like every other kit.
    if p:
        routes.append(Route(p, safe_page, methods=["GET"]))
    routes += [
        Route(f"{p}/", safe_page, methods=["GET"]),
        Route(f"{p}/dev", _safe("html", dashboard), methods=["GET"]),
        Route(f"{p}/api/context", _safe("json", ctx), methods=["GET"]),
        Route(f"{p}/api/rules", _safe("json", rules), methods=["GET"]),
        Route(f"{p}/api/rules/{{rule_id}}", _safe("json", rule), methods=["GET"]),
        Route(f"{p}/api/samples", _safe("json", samples_route), methods=["GET"]),
        Route(f"{p}/api/summary", _safe("json", summary_route), methods=["GET"]),
        Route(f"{p}/api/match", _safe("json", match), methods=["POST"]),
        Route(f"{p}/api/connect", _safe("json", connect_route), methods=["GET"]),
        Route(f"{p}/account", _safe("json", account_route), methods=["GET"]),
        Route(f"{p}{STATUS_SUFFIX}", _safe("html", status_route), methods=["GET"]),
        Route(f"{p}/pulse", _safe("json", pulse_route), methods=["GET"]),
        Route(f"{p}/panel", _safe("json", panel_route), methods=["GET"]),
    ]
    # Starlette: prepend our routes so they take priority over a
    # catch-all the host app may have registered earlier.
    wiring = host_surface.begin("starlette", unit="request")
    app.routes[:0] = routes
    host_surface.note_panel(wiring, True)
    # Same note as the FastAPI adapter: the trace middleware below must not
    # answer for a prefix these routes already serve.
    kit_pages.note_kit_paths_registered(prefix)
    # Install the per-request trace middleware — same reason as the FastAPI
    # adapter above: without it every request-boundary meter (accessPressure,
    # idle, loadDeflection, the runtime vitals, allocChurn, importChurn) stays
    # on "warming up" forever. Self-guarding and zero-overhead when telemetry
    # is off or the kill switch is active. Reported, never swallowed — see the
    # note in the FastAPI adapter above.
    try:
        from .trace import BoosthisTraceMiddleware
        app.add_middleware(BoosthisTraceMiddleware, kit_prefix=prefix)
        host_surface.note_measurement(wiring, True)
    except Exception as exc:  # noqa: BLE001
        host_surface.note_measurement(wiring, False, "%s: %s" % (type(exc).__name__, exc))
    host_surface.finish(wiring)
    # Inject the dev bubble into outgoing HTML only when it should be visible
    # for this process — production (no dev signal) never installs the
    # buffering middleware, so there is zero prod overhead.
    if resolve_bubble_visibility(bubble):
        _install_asgi_bubble(app, bubble_snippet(f"{prefix}/pulse"), bubble)
    return app


# ---------------------------------------------------------------------------
# Flask adapter
# ---------------------------------------------------------------------------

def _mount_flask(app: Any, prefix: str, bubble: bool | None = None) -> Any:
    from flask import Blueprint, jsonify, request  # type: ignore[import-not-found]

    bp = Blueprint("boosthis_dashboard", __name__)

    # Pre-built replies for a route that failed. Plain tuples rather than
    # jsonify() so the apology needs no application context and has nothing
    # left that could fail.
    _ERR_JSON = (_KIT_ERROR_JSON, 500, {"Content-Type": "application/json"})
    _ERR_HTML = (
        _KIT_ERROR_HTML,
        500,
        {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
    )
    _ERR_FORBIDDEN = (
        _FORBIDDEN_BODY.decode(),
        403,
        {"Content-Type": "application/json"},
    )

    def _safe(kind: str) -> Any:
        """Wrap ONE kit-owned route so a throw inside it never reaches the host.

        Flask hands an unhandled exception to the HOST's error handler, which
        is how a hiccup in kit code ends up rendering the customer's own error
        page — or Werkzeug's debugger — to their visitor. Same shape as the
        FastAPI and Starlette adapters above, and the Go kit's ``defer
        recover()``: catch it here, and still answer in the shape this route's
        caller expects. ``functools.wraps`` keeps the function name Flask uses
        as the endpoint name.
        """

        def _decorate(fn: Any) -> Any:
            @functools.wraps(fn)
            def _wrapped(*args: Any, **kwargs: Any) -> Any:
                try:
                    return fn(*args, **kwargs)
                except Exception:  # noqa: BLE001
                    return _ERR_HTML if kind == "html" else _ERR_JSON

            return _wrapped

        return _decorate

    def _guard() -> Any:
        """Return a 403 JSON response when the caller is not localhost, else None."""
        host = request.remote_addr
        if not _local_request_allowed(host, request.headers):
            return (
                jsonify({
                    "error": "forbidden",
                    "detail": (
                        "The Boosthis dashboard is only accessible from localhost. "
                        "Set BOOSTHIS_MOUNT_ALLOW_REMOTE=1 if you are running inside "
                        "a container behind an authenticated reverse proxy."
                    ),
                }),
                403,
            )
        return None

    @bp.before_request
    def _check_local() -> Any:
        # The status page owns its own STRICT loopback guard AND its own
        # fixed HTML 403 body, so let it through the looser blueprint guard
        # here and enforce (+ render the local-only page) inside _status.
        try:
            if (request.path or "").endswith(STATUS_SUFFIX):
                return None
            return _guard()
        except Exception:  # noqa: BLE001
            # A guard that fails must DENY, not crash the host's request and
            # not accidentally open a local-only surface.
            return _ERR_FORBIDDEN

    # This kit's detailed local view — per-route table, learned budgets, recent
    # samples, the rule checklist and the "Connect your AI" card. It renders
    # route labels and a credential, so it stays on the blueprint, behind the
    # loopback guard above, like every read it draws from. The in-app page
    # itself is registered on the APP further down, deliberately ungated.
    @bp.route("/dev", methods=["GET"])
    @_safe("html")
    def _dashboard() -> Any:
        return _read_dashboard_html(), 200, {"Content-Type": "text/html; charset=utf-8"}

    @bp.route("/api/context", methods=["GET"])
    @_safe("json")
    def _context() -> Any:
        limit = int(request.args.get("limit", "25"))
        return jsonify(_h_context(limit=limit))

    @bp.route("/api/rules", methods=["GET"])
    @_safe("json")
    def _rules() -> Any:
        return jsonify(_h_rules())

    @bp.route("/api/rules/<rule_id>", methods=["GET"])
    @_safe("json")
    def _rule(rule_id: str) -> Any:
        status, body = _h_rule(rule_id)
        return jsonify(body), status

    @bp.route("/api/samples", methods=["GET"])
    @_safe("json")
    def _samples() -> Any:
        limit = int(request.args.get("limit", "100"))
        name = request.args.get("name") or None
        return jsonify(_h_samples(limit=limit, name=name))

    @bp.route("/api/summary", methods=["GET"])
    @_safe("json")
    def _summary() -> Any:
        return jsonify(_h_summary())

    @bp.route("/api/match", methods=["POST"])
    @_safe("json")
    def _match() -> Any:
        try:
            body = request.get_json(force=True, silent=False)
        except Exception:  # noqa: BLE001
            return jsonify({"error": "invalid JSON"}), 400
        status, payload = _h_match(body)
        return jsonify(payload), status

    @bp.route("/api/connect", methods=["GET"])
    @_safe("json")
    def _connect() -> Any:
        # The blueprint's before_request guard honours the remote escape hatch;
        # /connect emits a read token, so re-check STRICT loopback here (env
        # override never opens it) — mirrors Node's always-loopback /connect.
        if not _strict_loopback_allowed(request.remote_addr, request.headers):
            return (
                jsonify(
                    {
                        "error": (
                            "boosthis /connect is loopback-only — it emits a read token. "
                            "Copy the credentials from a local session."
                        )
                    }
                ),
                403,
            )
        return jsonify(_h_connect())

    @bp.route("/account", methods=["GET"])
    @_safe("json")
    def _account() -> Any:
        # The blueprint's before_request guard honours the remote escape hatch;
        # /account emits an install delete token, so re-check STRICT loopback
        # here (env override never opens it) — mirrors Node's /account.
        if not _strict_loopback_allowed(request.remote_addr, request.headers):
            return (
                jsonify(
                    {
                        "error": (
                            "boosthis /account is loopback-only — it emits an install token. "
                            "Open this page on the dev machine to sign in."
                        )
                    }
                ),
                403,
            )
        return jsonify(_h_account())

    @bp.route(STATUS_SUFFIX, methods=["GET"])
    @_safe("html")
    def _status() -> Any:
        # Standalone status page — STRICT loopback only, the SAME guard
        # /account uses; BOOSTHIS_MOUNT_ALLOW_REMOTE never opens it (mirrors
        # Node's /status). The blueprint's before_request skips this path so
        # this route owns both the guard and the fixed HTML 403 body — a remote
        # request gets 403 disclosing nothing.
        #
        # Flask's ``request.headers`` iterates as (name, value) tuples, which
        # the shared ``_has_proxy_headers`` (it lower-cases each iterated KEY)
        # cannot read — so pass a plain {name: value} dict, the mapping shape
        # the guard documents. This is NOT a weakening: it makes proxy-header
        # detection actually fire for the Flask status route.
        if not _strict_loopback_allowed(request.remote_addr, dict(request.headers)):
            return (
                STATUS_FORBIDDEN_HTML,
                403,
                {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
            )
        return (
            status_page_html(),
            200,
            {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
        )

    wiring = host_surface.begin("flask", unit="request")
    app.register_blueprint(bp, url_prefix=prefix or "/")
    host_surface.note_panel(wiring, True)
    # A door now serves the kit's paths under this prefix — see the note in the
    # FastAPI adapter. Recorded for every framework, not just the ASGI ones: an
    # app can hand-install the ASGI middleware in front of any of them.
    kit_pages.note_kit_paths_registered(prefix)

    @app.before_request
    def _boosthis_queue_time() -> None:
        try:
            from boosthis import extra_meters

            extra_meters.note_queue_start(request.headers)
        except Exception:  # noqa: BLE001
            pass

    # Runtime-vitals boundary (reliability + memory): count total responses and
    # 5xx errors, and sample memory (throttled), on every finished request.
    # Registered on the APP so it observes the host's real traffic, not just the
    # dashboard routes. Additive + display-only — never feeds the Speed score.
    # Fail-open: any error serves the original response untouched.
    def _boosthis_vitals(response: Any) -> Any:
        try:
            status = getattr(response, "status_code", 200) or 200
            note_request(int(status))
            # Remember the status for the span's OUTCOME. Flask tears a
            # request down after the response object is gone, so this is the
            # last point at which the kit can see what the app answered. A
            # request that never reaches here (an unhandled throw) leaves the
            # mark unset, and the span then says nothing about its outcome
            # rather than reporting a clean one.
            _g._boosthis_status = int(status)
        except Exception:  # noqa: BLE001
            pass
        return response

    try:
        app.after_request(_boosthis_vitals)
    except Exception:  # noqa: BLE001
        pass

    # loadDeflection boundary (sync/WSGI): the ASGI trace middleware has this
    # boundary built in, but a mounted Flask/WSGI app has no vitals boundary of
    # its own — so wire the SAME in-flight counter here. ``before_request`` marks
    # the request in flight and stamps a monotonic start; ``teardown_request``
    # (which ALWAYS runs, even on an error) files the duration into the bucket
    # for the concurrency seen at start, then clears the in-flight mark.
    # Registered on the APP so it observes the host's real traffic.
    #
    # LEAK-SAFETY: the increment (_note_request_start) and the decrement
    # (_note_request_end) are strictly paired. before_request records a
    # "counted" flag on ``g`` the instant it increments, and teardown does the
    # decrement UNCONDITIONALLY in a finally that sits OUTSIDE the recording
    # try — so an exception inside our own sample recording (or anywhere else)
    # can never leave the shared in-flight count elevated. If the increment
    # itself never happened, no flag is set and teardown decrements nothing.
    import time as _time
    from flask import g as _g  # type: ignore[import-not-found]
    from boosthis.live_detectors import (
        current_in_flight as _current_in_flight,
        note_request_end as _note_request_end,
        note_request_start as _note_request_start,
    )
    from boosthis import extra_meters as _extra_meters
    from boosthis.held_open import (
        is_held_open_response as _is_held_open_response,
        note_held_open_excluded as _note_held_open_excluded,
    )

    def _boosthis_defl_start() -> None:
        try:
            _note_request_start()
            # Mark "we counted a start" BEFORE anything that could throw, so the
            # teardown decrement is guaranteed to pair with this increment.
            _g._boosthis_defl_counted = True
            _g._boosthis_defl_t0 = _time.monotonic()
            _g._boosthis_defl_in_flight = _current_in_flight()
        except Exception:  # noqa: BLE001
            pass

    def _kit_rule(rule: str) -> bool:
        """Is this route rule one of the KIT's own pages?

        Under a real prefix the prefix answers it. Mounted at the ROOT there is
        no prefix to test against — the kit's pages sit at their bare suffixes
        beside the app's own routes — so the kit's own route table answers
        instead. Testing ``startswith("/")`` there would fold the entire app
        away, which is exactly the "registers, then reports nothing" failure
        this boundary exists to prevent.
        """
        if prefix and prefix != "/":
            return rule == prefix or rule.startswith(prefix + "/")
        from boosthis import kit_pages as _kit_pages

        if rule.startswith(_kit_pages.RULE_PREFIX):
            return True
        return any(rule == (suffix or "/") for suffix, _method, _guard in _kit_pages.ROUTES)

    def _flask_sample_label() -> str | None:
        """The label this request files under, or None to record nothing.

        Named after the matched ROUTE RULE (``/widgets/<wid>``), never the URL:
        a path carries ids. The kit's own pages return None — a kit's own
        traffic is not the app's reading.
        """
        from flask import request as _request  # type: ignore[import-not-found]
        from boosthis import unit_of_work as _unit_of_work

        rule = getattr(getattr(_request, "url_rule", None), "rule", None)
        if not isinstance(rule, str) or not rule.strip():
            # Unmatched (404) or no routing context: still the app's work, but
            # nothing code-defined names it, so it files under one bounded name.
            return "flask.request"
        if _kit_rule(rule):
            return None
        return _unit_of_work.safe_label(rule, "flask.request")

    def _boosthis_defl_record(
        t0: float | None,
        in_flight: int,
        label: str | None,
        held_open: bool = False,
        status: int | None = None,
    ) -> None:
        """File the readings for one finished request.

        Never releases the in-flight mark: teardown owns that, unconditionally,
        so no response shape and no host WSGI driver can leave the shared count
        elevated.
        """
        try:
            if t0 is not None:
                duration_ms = (_time.monotonic() - t0) * 1000.0
                if held_open:
                    _note_held_open_excluded(duration_ms)
                    return
                # THE timing sample — the reading the dashboard counts under
                # "Samples", and what triggers the throttled snapshot upload.
                # Without it a mounted Flask app registers, checks in and
                # reports nothing, which looks exactly like no kit at all.
                try:
                    from boosthis import tracker as _tracker
                    from boosthis.span_work import outcome_for_status

                    if label:
                        # A Flask request IS the host's handler, so the span
                        # says so. The outcome comes from the status its
                        # caller read while the request context was still
                        # alive — this function also runs at the END OF A
                        # STREAMED BODY, outside that context, where touching
                        # ``g`` raises. Absent when nothing saw a status,
                        # which reads as "not reported", never as a success.
                        _tracker._emit(
                            label,
                            int(round(duration_ms)),
                            kind="handler",
                            outcome=(
                                outcome_for_status(status)
                                if isinstance(status, int)
                                else None
                            ),
                        )
                except Exception:  # noqa: BLE001
                    pass  # instrumentation must never disturb the host app.
                # The request-path family's route-failure reading: how many of
                # the app's own route rules answered, and how many of those
                # answers were 5xx. The ASGI middleware files this from
                # trace.py and the Django adapter from
                # unit_of_work.note_http_response; a mounted Flask app went
                # through NEITHER, so the reading simply never attached there
                # while both its siblings reported it — a partial view of one
                # kit reading as a complete one, which is the exact failure
                # docs/request-error-and-timer-reading-contract.md §4 forbids.
                # Filed here because this is the one place a Flask request is
                # finished with both its matched rule and the status its
                # caller read. No dependency handle: nothing in the WSGI path
                # carries a request-local outbound tally, so failureContainment
                # stays honestly unattached rather than being fed a blank one.
                try:
                    from boosthis.request_error_timer_meters import (
                        note_response as _note_response,
                    )

                    if label:
                        _note_response(label, status, None)
                except Exception:  # noqa: BLE001
                    pass  # instrumentation must never disturb the host app.
                _extra_meters.note_deflection_sample(in_flight, duration_ms)
        except Exception:  # noqa: BLE001
            pass

    def _boosthis_defl_handoff(response: Any) -> Any:
        """For a STREAMING response, move the close to the end of the body.

        Flask tears a request down when the view RETURNS. For an ordinary
        response the body is already built by then, so teardown is the honest
        instant and nothing is deferred. For a streamed one it is before a
        single byte has been written: timing there measures how long the
        generator took to BUILD, not the request the client waited on.

        The deferral rides the body ITSELF — the wrapper's ``finally`` runs
        whether the stream is exhausted or abandoned — with ``call_on_close``
        as the backstop for a body we must not wrap (``direct_passthrough``
        hands the host's file straight to the server). A close callback alone
        would be wrong: a WSGI caller is only obliged to close what it iterates,
        and Werkzeug's own test client never closes at all — so a kit that
        recorded only there would be invisible to every Flask test suite,
        including this kit's. Everything the close needs is read HERE, while
        the request context is still alive.

        Only the READING is deferred. The in-flight mark is released at
        teardown as it always was, so a host driver that neither drains nor
        closes a body can cost us a sample but never a stuck count.
        """
        try:
            if not getattr(_g, "_boosthis_defl_counted", False) or getattr(
                _g, "_boosthis_defl_handed", False
            ):
                return response
            if not getattr(response, "is_streamed", False):
                _g._boosthis_defl_held_open = _is_held_open_response(response)
                return response  # teardown files it, as it always has

            t0 = getattr(_g, "_boosthis_defl_t0", None)
            in_flight = getattr(_g, "_boosthis_defl_in_flight", 1)
            label = _flask_sample_label()
            held_open = _is_held_open_response(response)
            # Read HERE with the rest, for the span's outcome: the close below
            # runs after the body is finished, when the response object may be
            # all that is left of the request.
            status = getattr(response, "status_code", None)
            fired: list[bool] = [False]

            def _close() -> None:
                if fired[0]:
                    return
                fired[0] = True
                _boosthis_defl_record(t0, in_flight, label, held_open, status)

            if not getattr(response, "direct_passthrough", False):
                inner = response.response

                def _timed_body() -> Any:
                    try:
                        for chunk in inner:
                            yield chunk
                    finally:
                        # The host's own iterable is what the framework would
                        # have closed; standing in front of it makes closing it
                        # OUR job, and its cleanup comes before our reading.
                        try:
                            _inner_close = getattr(inner, "close", None)
                            if callable(_inner_close):
                                _inner_close()
                        finally:
                            _close()

                response.response = _timed_body()
            response.call_on_close(_close)
            _g._boosthis_defl_handed = True
        except Exception:  # noqa: BLE001
            pass
        return response

    def _boosthis_defl_end(_exc: Any = None) -> None:
        """Teardown: release the in-flight mark, and file the reading unless a
        streamed body took that job.

        An exception that propagates (debug or testing mode) skips
        ``after_request`` entirely, so this is also the only close a failed
        request gets. The release is unconditional — the increment's partner —
        and happens here for EVERY request, deferred body or not.
        """
        counted = getattr(_g, "_boosthis_defl_counted", False)
        try:
            if counted and not getattr(_g, "_boosthis_defl_handed", False):
                t0 = getattr(_g, "_boosthis_defl_t0", None)
                in_flight = getattr(_g, "_boosthis_defl_in_flight", 1)
                try:
                    label = _flask_sample_label()
                except Exception:  # noqa: BLE001
                    label = None
                held_open = getattr(_g, "_boosthis_defl_held_open", False)
                # Teardown still runs inside the request context, so the
                # status the after_request hook noted is readable here. A
                # request whose exception skipped that hook leaves it unset,
                # and the span then says nothing about its outcome.
                status = getattr(_g, "_boosthis_status", None)
                _boosthis_defl_record(
                    t0,
                    in_flight,
                    label,
                    held_open,
                    status if isinstance(status, int) else None,
                )
        finally:
            if counted:
                try:
                    _note_request_end()
                except Exception:  # noqa: BLE001
                    pass

    # These three ARE the Flask measuring half — without them every
    # request-boundary meter stays on "warming up" forever. Reported, never
    # swallowed: the panel above must not be able to arrive on its own.
    try:
        app.before_request(_boosthis_defl_start)
        app.after_request(_boosthis_defl_handoff)
        app.teardown_request(_boosthis_defl_end)
        host_surface.note_measurement(wiring, True)
    except Exception as exc:  # noqa: BLE001
        host_surface.note_measurement(wiring, False, "%s: %s" % (type(exc).__name__, exc))
    host_surface.finish(wiring)

    # cookieExposure boundary (Flask/WSGI): wrap app.wsgi_app so we can observe
    # the final outgoing Set-Cookie headers from the WSGI start_response call.
    # This runs AFTER Flask has executed all after_request handlers AND saved
    # its session cookie (Flask's SecureCookieSessionInterface.save_session is
    # called inside finalize_request, which writes Set-Cookie then hands off to
    # the WSGI layer), so every cookie the app emits is visible here —
    # including the session cookie and cookies added by host after_request
    # handlers registered before mount(). The ASGI trace middleware already
    # covers ASGI apps via http.response.start; this is the WSGI equivalent.
    #
    # app.wsgi_app is the standard Flask/Werkzeug middleware seam (Flask's own
    # ProxyFix, DispatcherMiddleware, and others use the same slot). Wrapping
    # it rather than the app object itself preserves Flask's dispatch stack.
    #
    # Security: header names are checked for "set-cookie" only; header values
    # are re-encoded to bytes solely to pass to note_cookie_headers, which
    # parses only attribute tokens from the value bytes and discards them after
    # counting. No cookie name, value, or route ever leaves this function.
    # Fail-open: any error calls the real start_response and returns its result.
    try:
        _original_wsgi = getattr(app, "wsgi_app", None)
        if _original_wsgi is not None:
            def _boosthis_cookie_wsgi(environ: Any, start_response: Any) -> Any:
                def _intercepted(
                    status: Any, headers: Any, exc_info: Any = None
                ) -> Any:
                    try:
                        sc = [
                            (
                                b"set-cookie",
                                v.encode("latin-1", "replace")
                                if isinstance(v, str)
                                else bytes(v),
                            )
                            for n, v in (headers or [])
                            if isinstance(n, str) and n.lower() == "set-cookie"
                        ]
                        if sc:
                            _extra_meters.note_cookie_headers(sc)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        status_code = int(str(status).split(" ", 1)[0])
                        authorization_present = bool(
                            environ.get("HTTP_AUTHORIZATION")
                        )
                        challenge_present = any(
                            isinstance(n, str)
                            and n.lower() == "www-authenticate"
                            and bool(v)
                            for n, v in (headers or [])
                        )
                        _extra_meters.note_refusal_honesty(
                            status_code,
                            authorization_present,
                            challenge_present,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    if exc_info is not None:
                        return start_response(status, headers, exc_info)
                    return start_response(status, headers)

                return _original_wsgi(environ, _intercepted)

            app.wsgi_app = _boosthis_cookie_wsgi
    except Exception:  # noqa: BLE001
        pass

    # The in-app page — the ONE page every Boosthis kit serves. Registered on
    # the APP itself, NOT the blueprint, so the blueprint's before_request
    # loopback guard does not gate it: it rides the same visibility as the
    # /panel read below that it fetches its numbers from. The markup is static
    # and carries no measurements, no route labels and no credential, so gating
    # it while /panel stays open would protect nothing. Both spellings of the
    # address are registered, so neither habit 404s.
    @_safe("html")
    def _page_route() -> Any:
        return _read_panel_page_html(), 200, {"Content-Type": "text/html; charset=utf-8"}

    for _i, _rule in enumerate((prefix, f"{prefix}/") if prefix else ("/",)):
        try:
            app.add_url_rule(_rule, f"boosthis_page_{_i}", _page_route, methods=["GET"])
        except Exception:  # noqa: BLE001
            # A duplicate endpoint (e.g. mount() called twice) must never break
            # mount(); the blueprint routes are already registered.
            pass

    # Closed pulse read for the floating bubble — registered on the APP itself,
    # NOT the blueprint, so the blueprint's before_request loopback guard does
    # not gate it. Deliberately UNGATED: the injected badge polls it from any
    # page the app serves, and the shape is closed + coarse ({score, rating,
    # sampleCount} — never route labels, durations, or per-route data).
    @_safe("json")
    def _pulse_route() -> Any:
        resp = jsonify(pulse_body())
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @_safe("json")
    def _panel_route() -> Any:
        # Closed panel read for the bubble's dashboard panel — same UNGATED
        # policy as /pulse (scores/ratings/captions only, never routes/URLs).
        resp = jsonify(panel_body())
        resp.headers["Cache-Control"] = "no-store"
        return resp

    try:
        app.add_url_rule(
            f"{prefix}/pulse" if prefix else "/pulse",
            "boosthis_pulse",
            _pulse_route,
            methods=["GET"],
        )
    except Exception:  # noqa: BLE001
        # A duplicate endpoint (e.g. mount() called twice) must never break
        # mount(); the dashboard + blueprint routes are already registered.
        pass

    try:
        app.add_url_rule(
            f"{prefix}/panel" if prefix else "/panel",
            "boosthis_panel",
            _panel_route,
            methods=["GET"],
        )
    except Exception:  # noqa: BLE001
        # Same duplicate-registration tolerance as the pulse route above.
        pass

    # Inject the dev bubble into outgoing HTML only when it should be visible
    # for this process — production (no dev signal) never installs the
    # after_request injector, so there is zero prod overhead.
    if resolve_bubble_visibility(bubble):
        _install_flask_bubble(app, bubble_snippet(f"{prefix}/pulse"), bubble)
    return app


__all__ = ["mount", "DEFAULT_PREFIX"]
