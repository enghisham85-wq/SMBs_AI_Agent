"""The kit's own pages, expressed once, without a web framework.

``mount()`` registers these routes three times already — as a FastAPI router, a
Starlette route list and a Flask blueprint — because each of those frameworks
wants its own decorators. Django and Streamlit make four and five, and Streamlit
has no user-facing routing layer at all: its adapter has to answer raw ASGI.

So the route TABLE lives here, framework-free: given a path suffix, a method,
the caller's address and headers, hand back a status, a content type and a body.
An adapter's job shrinks to plumbing its own request objects in and its own
response objects out, and every surface keeps the same guards, the same bodies
and the same failure behaviour as the three original adapters.

Nothing here raises: a handler that blows up answers with the kit's own apology
in the shape the caller asked for, exactly like the ``_safe`` wrappers in
``mount.py``. A page of ours must never become the host's error page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html import escape
from importlib import import_module
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

_HTML = "text/html; charset=utf-8"
_JSON = "application/json"

#: The default mount prefix — every kit-owned read hangs off it. It lives here,
#: in the framework-free module, because the door that answers when NO mount
#: ran needs it too. ``mount.DEFAULT_PREFIX`` is the same string, and
#: ``tests/test_unmounted_kit_read.py`` holds the two together.
KIT_BASE_PATH = "/_boosthis"

# ---------------------------------------------------------------------------
# The apology a kit route serves instead of handing a throw to the host
# ---------------------------------------------------------------------------
#
# Every adapter wraps its own routes so an exception inside kit code can never
# reach the host application's error handling — a hiccup of ours must not
# render the CUSTOMER's error page to their visitor. The bodies live here, in
# the one framework-free module every adapter already imports, so all of them
# apologise in the same words. Both are pre-rendered constants: the apology has
# nothing to look up and no state to read, so it cannot fail the way the view
# it replaces just did.

KIT_ERROR_HTML = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Boosthis</title>'
    '</head><body style="background:#0b0c10;color:#e6e7eb;'
    'font-family:system-ui,sans-serif;padding:40px">'
    "<h1>Boosthis could not draw this page</h1>"
    "<p>Something inside the kit failed while building this view. Your app is "
    "unaffected — only this page is. Reload to try again.</p></body></html>"
)

KIT_ERROR_JSON = json.dumps({"error": "boosthis could not answer this read."})


@dataclass
class KitResponse:
    """One framework-free answer from a kit route."""

    status: int
    content_type: str
    body: bytes
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        try:
            return self.body.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001  # pragma: no cover - defensive
            return ""


# How each suffix is guarded.
OPEN = "open"  # ungated: static page / closed coarse read
LOCAL = "local"  # loopback, BOOSTHIS_MOUNT_ALLOW_REMOTE may open it
STRICT = "strict"  # loopback only; the env escape hatch never opens it

#: (suffix, method, guard). Order matters only for readability; lookup is exact
#: on the suffix, with ``/api/rules/<id>`` handled as a prefix match.
ROUTES: List[Tuple[str, str, str]] = [
    ("", "GET", OPEN),
    ("/", "GET", OPEN),
    ("/pulse", "GET", OPEN),
    ("/panel", "GET", OPEN),
    ("/dev", "GET", LOCAL),
    ("/api/context", "GET", LOCAL),
    ("/api/rules", "GET", LOCAL),
    ("/api/samples", "GET", LOCAL),
    ("/api/summary", "GET", LOCAL),
    ("/api/match", "POST", LOCAL),
    ("/api/connect", "GET", STRICT),
    ("/account", "GET", STRICT),
]

#: Prefix route: ``/api/rules/<rule_id>``.
RULE_PREFIX = "/api/rules/"


def suffixes(prefix: str) -> List[str]:
    """Every path this kit answers under ``prefix``, for adapters that must
    register concrete routes. The rule-detail route is returned as its parent
    ``/api/rules/`` so a catch-all can pick it up."""
    base = prefix or ""
    out = [f"{base}{suffix}" if suffix else (base or "/") for suffix, _, _ in ROUTES]
    out.append(f"{base}{RULE_PREFIX}")
    from .status_page import STATUS_SUFFIX  # local: keeps import cost off the hot path

    out.append(f"{base}{STATUS_SUFFIX}")
    return out


def _err(kind: str) -> KitResponse:
    from .mount import _KIT_ERROR_HTML, _KIT_ERROR_JSON

    if kind == "html":
        return KitResponse(
            500, _HTML, _KIT_ERROR_HTML.encode("utf-8"), {"Cache-Control": "no-store"}
        )
    return KitResponse(500, _JSON, _KIT_ERROR_JSON)


def _json(body: Any, status: int = 200, no_store: bool = False) -> KitResponse:
    payload = json.dumps(body).encode("utf-8")
    return KitResponse(
        status, _JSON, payload, {"Cache-Control": "no-store"} if no_store else {}
    )


def _forbidden_json() -> KitResponse:
    from .mount import _FORBIDDEN_BODY

    return KitResponse(403, _JSON, _FORBIDDEN_BODY)


def guard_for(suffix: str) -> Optional[str]:
    """Which guard a suffix carries, or ``None`` when it is not a kit route."""
    from .status_page import STATUS_SUFFIX

    if suffix == STATUS_SUFFIX:
        return STRICT
    for candidate, _, guard in ROUTES:
        if candidate == suffix:
            return guard
    if suffix.startswith(RULE_PREFIX):
        return LOCAL
    return None


# ---------------------------------------------------------------------------
# Which doors are already serving the kit's own paths
# ---------------------------------------------------------------------------

#: Bases under which some door in THIS process serves the kit's whole path set —
#: any successful ``boosthis.mount()``, whichever framework answered it.
_registered_kit_bases: Set[str] = set()


def normalise_base(base: str) -> str:
    """Fold ``/_boosthis/`` and ``/_boosthis`` into one entry, the way
    ``mount()`` normalises the prefix it is given."""
    b = base or ""
    return b[:-1] if b.endswith("/") else b


def note_kit_paths_registered(base: str = KIT_BASE_PATH) -> None:
    """Record that a mount now serves every kit-owned path under ``base``.

    The hand-installed ASGI middleware answers a few paths itself and leaves the
    rest to ``boosthis.mount(app)`` — a deliberate split. What it must NOT do is
    answer a path a mounted route is about to answer properly: middleware runs
    first, so an unconditional reply there would shadow the real page. This
    register is how that door knows to stay quiet.
    """
    try:
        _registered_kit_bases.add(normalise_base(base))
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        # A note about our own wiring must never break the wiring.
        pass


def are_kit_paths_registered(base: str = KIT_BASE_PATH) -> bool:
    """True when a mount already serves the kit's paths under this base."""
    return normalise_base(base) in _registered_kit_bases


def registered_kit_base_paths() -> List[str]:
    """Every base a mount registered, for a message that must not tell a
    developer to mount what they have already mounted somewhere else."""
    return list(_registered_kit_bases)


def _reset_kit_path_registrations_for_tests() -> None:
    _registered_kit_bases.clear()


def kit_read_suffix_for(path: str, base: str = KIT_BASE_PATH) -> Optional[str]:
    """Split a request path into the kit's mount prefix and the suffix it owns,
    or ``None`` when the path is the host's own. Query strings are stripped by
    the caller."""
    b = normalise_base(base)
    if b:
        if not path.startswith(b):
            return None
        raw = path[len(b) :]
    else:
        raw = path
    # A trailing slash is a habit, not a different address: ``mount()``
    # registers both spellings of the page for exactly that reason, and a door
    # that answered ``/_boosthis/status`` while 404ing ``/_boosthis/status/``
    # reads as "no kit here".
    suffix = raw[:-1] if len(raw) > 1 and raw.endswith("/") else raw
    return suffix if guard_for(suffix) is not None else None


def middleware_served(suffix: str) -> bool:
    """True for the suffixes a WATCHING door answers itself when no mount ran.

    Deliberately a subset of everything the kit can answer: the two closed,
    coarse badge reads and the loopback-only status page — the surfaces a
    back-end app with no UI has instead of the floating badge. Widening this
    set would change what an install serves without anyone asking for it; the
    rest of the prefix needs the mount, and says so.
    """
    from .status_page import STATUS_SUFFIX  # local: import cost off the hot path

    return suffix in ("/pulse", "/panel", STATUS_SUFFIX)


def _kit_page(title: str, body: str) -> str:
    """Wrap a body in the kit's own page chrome — the same chrome the Node kit
    uses. Static markup with nothing to look up, so a page built here cannot
    fail the way the page it stands in for did."""
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{title}</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '</head><body style="background:#0b0c10;color:#e6e7eb;'
        "font-family:system-ui,sans-serif;padding:40px;line-height:1.55;"
        'max-width:46rem">'
        f"{body}"
        "</body></html>"
    )


def unmounted_guidance(
    base: str, elsewhere: Optional[str] = None
) -> Tuple[str, str]:
    """How the honest "this surface is not registered here" answer is worded.
    Kept beside the answer itself so the two can never drift, and worded as the
    Node kit words it — only the call names are this language's."""
    if elsewhere:
        return (
            "Boosthis is installed and measuring, but its pages are served under "
            f"{elsewhere} in this app, not under {base}.",
            f"Use {elsewhere} instead — same page, same reads.",
        )
    return (
        "Boosthis is installed and measuring. This surface is registered by the "
        "optional second call, boosthis.mount(app), which this app has not made.",
        "Replace app.add_middleware(BoosthisTraceMiddleware) with "
        "boosthis.mount(app) and restart — that one call serves these pages and "
        "keeps the measuring you already have. It knows FastAPI, Starlette, "
        "Flask, Django, Streamlit and Gradio; on any other ASGI app the "
        "middleware is all there is, and these pages stay unserved.",
    )


def answer_unmounted_kit_read(
    suffix: str, *, base: str = KIT_BASE_PATH, elsewhere: Optional[str] = None
) -> Optional[KitResponse]:
    """Answer a kit-owned path that no mount in this process serves.

    ``boosthis.mount(app)`` is optional by design and we say so. What went
    unsaid is what happens to the rest of the prefix without it: every read the
    mount registers — INCLUDING the bare ``/_boosthis`` page printed in the
    setup block, the install guide and the kit's own comments — fell through to
    the host's own 404. A developer whose kit was installed, registered,
    measuring and uploading perfectly visited the address we gave them and got a
    reply byte-identical to having no kit at all.

    The watching middleware already knew everything needed to answer: it holds
    the prefix, it sees the raw path, and it can write a reply. So it says the
    true thing instead of nothing — the kit is here and measuring, this surface
    needs the mount call, and the surfaces that DO work are named.

    Still a 404: the address really does not serve a page in this app, and
    claiming 200 would trade one lie for another. What changes is that the body
    is ours and it names the cause. Only paths in the kit's own route table get
    this answer, so a host route that merely starts with the prefix is never
    claimed.
    """
    if guard_for(suffix) is None:
        return None
    from .status_page import STATUS_SUFFIX

    b = normalise_base(base)
    reason, fix = unmounted_guidance(b, elsewhere)
    status_path = f"{b}{STATUS_SUFFIX}"
    if suffix not in ("", "/", "/dev", STATUS_SUFFIX):
        return _json(
            {
                "error": reason,
                "fix": fix,
                "measuring": True,
                "statusPage": status_path,
            },
            404,
            no_store=True,
        )
    html = _kit_page(
        "Boosthis — one line short",
        "<h1>Boosthis is installed and measuring</h1>"
        f"<p>{escape(reason)}</p>"
        f"<p>{escape(fix)}</p>"
        + (
            ""
            if elsewhere
            else '<pre style="background:#15171d;padding:14px;border-radius:8px;'
            'overflow:auto"><code>boosthis.mount(app)</code></pre>'
        )
        + "<p>Nothing is broken and nothing is missing from the install — this "
        "app simply skipped a call we describe as optional.</p>"
        + "<p>Working right now, with no mount: the status page at "
        f'<a style="color:#7cc4ff" href="{escape(status_path)}">'
        f"{escape(status_path)}</a> (localhost only), and the badge reads at "
        f"<code>{escape(b)}/pulse</code> and <code>{escape(b)}/panel</code>.</p>",
    )
    return KitResponse(
        404, _HTML, html.encode("utf-8"), {"Cache-Control": "no-store"}
    )


def handle(
    suffix: str,
    *,
    method: str = "GET",
    remote_addr: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    query: Optional[Mapping[str, Any]] = None,
    body: Optional[bytes] = None,
) -> Optional[KitResponse]:
    """Answer one kit route, or return ``None`` when ``suffix`` is not ours.

    ``suffix`` is the path with the mount prefix already stripped ("" or "/" for
    the in-app page). ``headers`` must be a plain name -> value mapping (the
    proxy-header check lower-cases its KEYS, so a Flask-style header object that
    iterates as tuples would silently defeat it)."""
    try:
        return _handle(suffix, method, remote_addr, headers, query, body)
    except Exception:  # noqa: BLE001
        # The kit apologises in the caller's own shape rather than handing the
        # host's error machinery an exception of ours.
        return _err("json" if "/api/" in suffix else "html")


def _handle(
    suffix: str,
    method: str,
    remote_addr: Optional[str],
    headers: Optional[Mapping[str, str]],
    query: Optional[Mapping[str, Any]],
    body: Optional[bytes],
) -> Optional[KitResponse]:
    # NOT ``from . import mount``: the package re-exports the mount() FUNCTION
    # under that same name, so the plain form binds the function and every
    # attribute read below dies with an AttributeError the kit turns into a
    # blanket 500. import_module always answers with the module.
    _mount = import_module(f"{__package__}.mount")
    from .status_page import STATUS_SUFFIX, STATUS_FORBIDDEN_HTML, status_page_html

    guard = guard_for(suffix)
    if guard is None:
        return None

    if guard == LOCAL and not _mount._local_request_allowed(remote_addr, headers):
        return _forbidden_json()
    if guard == STRICT and not _mount._strict_loopback_allowed(remote_addr, headers):
        if suffix == STATUS_SUFFIX:
            return KitResponse(
                403,
                _HTML,
                STATUS_FORBIDDEN_HTML.encode("utf-8"),
                {"Cache-Control": "no-store"},
            )
        if suffix == "/account":
            return _json(
                {
                    "error": (
                        "boosthis /account is loopback-only — it emits an install token. "
                        "Open this page on the dev machine to sign in."
                    )
                },
                403,
            )
        return _json(
            {
                "error": (
                    "boosthis /connect is loopback-only — it emits a read token. "
                    "Copy the credentials from a local session."
                )
            },
            403,
        )

    q = query or {}

    def _int(name: str, default: int) -> int:
        try:
            return int(q.get(name, default))
        except Exception:  # noqa: BLE001
            return default

    if suffix in ("", "/"):
        return KitResponse(200, _HTML, _mount._read_panel_page_html().encode("utf-8"))
    if suffix == "/dev":
        return KitResponse(200, _HTML, _mount._read_dashboard_html().encode("utf-8"))
    if suffix == "/pulse":
        from .bubble import pulse_body

        return _json(pulse_body(), no_store=True)
    if suffix == "/panel":
        from .bubble import panel_body

        return _json(panel_body(), no_store=True)
    if suffix == STATUS_SUFFIX:
        return KitResponse(
            200, _HTML, status_page_html().encode("utf-8"), {"Cache-Control": "no-store"}
        )
    if suffix == "/api/context":
        return _json(_mount._h_context(limit=_int("limit", 25)))
    if suffix == "/api/rules":
        return _json(_mount._h_rules())
    if suffix.startswith(RULE_PREFIX):
        status, payload = _mount._h_rule(suffix[len(RULE_PREFIX) :])
        return _json(payload, status)
    if suffix == "/api/samples":
        name = q.get("name") or None
        return _json(_mount._h_samples(limit=_int("limit", 100), name=name))
    if suffix == "/api/summary":
        return _json(_mount._h_summary())
    if suffix == "/api/match":
        if (method or "GET").upper() != "POST":
            return _json({"error": "method not allowed"}, 405)
        try:
            parsed = json.loads((body or b"").decode("utf-8"))
        except Exception:  # noqa: BLE001
            return _json({"error": "invalid JSON"}, 400)
        status, payload = _mount._h_match(parsed)
        return _json(payload, status)
    if suffix == "/api/connect":
        return _json(_mount._h_connect())
    if suffix == "/account":
        return _json(_mount._h_account())
    return None
