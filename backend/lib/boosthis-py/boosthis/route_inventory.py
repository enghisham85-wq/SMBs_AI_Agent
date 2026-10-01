"""Ask the running framework for the whole route list.

The map can only draw what it has seen. Every route a request has reached has
a row; a route nobody has opened has nothing at all, so "which pages were
never reached?" could only ever answer "none declared". The framework, though,
already holds the whole list — this module asks it for one.

Three rules hold this module together:

* **Ask, never read.** This calls a running framework's own accessors. It does
  not open a source file, crawl the app, or parse a build artifact. If the
  framework will not say, we do not guess.
* **Nothing rather than a guess.** A shape we do not recognise loses the WHOLE
  read, not the part of it we failed to parse. A half list drawn as a whole
  map is a lie the reader cannot see.
* **A quiet route is "not seen", never "dead".** This module only reports what
  exists. Nothing here judges a route for being unvisited.

The labels produced here are the SAME strings this kit files its rows under —
Flask's ``/widgets/<wid>``, Starlette's ``/items/{item_id}``, Django's
``/orders/<int:pk>/`` — because the map joins the two by label. They are not
the Node kit's ``METHOD /path`` form, and they should not be: a route list
written in a vocabulary the rows do not share would join with nothing.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from boosthis import pii

# BOOSTHIS_PART_NAME_V1 — one bound and one refusal rule for inventory and timings.
MAX_PART_NAME = 100

#: Framework names the server's closed vocabulary accepts. Adding one here
#: without adding it there means the whole block is refused on arrival.
ROUTE_SOURCE_WORDS = frozenset({"fastapi", "starlette", "flask", "django"})

#: How many entries may travel. A larger app still reports its ``total`` so a
#: reader can say "at least this many" rather than believing the cap.
MAX_ROUTE_LIST_ENTRIES = 200

_TRUTHY_OFF = frozenset({"0", "false", "no", "off"})

_declared: List[str] = []
_app: Any = None
_kit_prefix: str = ""
_warned_declared_refusals: set[str] = set()


def register_routes(routes: Iterable[Any]) -> None:
    """Declare routes by hand.

    Still supported, and still useful: a route behind a proxy, or one a
    framework will not hand over, can only arrive this way. A declaration
    never loses to a framework read — the two lists merge.
    """
    try:
        for raw in routes:
            label = normalize_part_name(raw, warn_declared=True)
            if label and label not in _declared:
                _declared.append(label)
    except Exception:  # noqa: BLE001 — a declaration must never reach the host
        pass


def get_declared_routes() -> List[str]:
    """The hand-declared list, in the order it was declared."""
    return list(_declared)


def register_app(app: Any, prefix: str = "") -> None:
    """Remember the app object ``mount()`` was handed.

    This is a reference assignment and nothing else. No route table is read
    here: the read happens when a snapshot asks for one, so nothing is walked
    at startup and nothing sits in front of the first request.
    """
    global _app, _kit_prefix
    try:
        _app = app
        _kit_prefix = prefix or ""
    except Exception:  # noqa: BLE001
        pass


def _reset_route_inventory_for_tests() -> None:
    global _app, _kit_prefix
    _declared.clear()
    _app = None
    _kit_prefix = ""
    _warned_declared_refusals.clear()


def route_list_enabled() -> bool:
    """The switch. On unless ``BOOSTHIS_ROUTE_LIST`` says otherwise.

    Switched off, the block still travels — saying ``off`` — so the server can
    tell a developer who turned it off from a kit too old to have it.
    """
    v = os.environ.get("BOOSTHIS_ROUTE_LIST")
    if v is None:
        return True
    return v.strip().lower() not in _TRUTHY_OFF


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def normalize_part_name(raw: Any, *, warn_declared: bool = False) -> Optional[str]:
    """One route template, made safe to travel — or ``None``.

    A template is code, not user data: its literal segments are the developer's
    own words and are kept. What is refused is anything that does not LOOK like
    a template any more — a concrete id in a path means the framework handed
    back a URL rather than a pattern, and that is dropped rather than redacted
    into something that would read as a route.
    """
    try:
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text:
            if warn_declared:
                _warn_declared_refusal("unnameable")
            return None
        if len(text) > MAX_PART_NAME:
            if warn_declared:
                _warn_declared_refusal("too-long")
            return None
        if "?" in text or "#" in text:
            if warn_declared:
                _warn_declared_refusal("unsafe")
            return None
        if pii.check_route_label(text) is not None:
            if warn_declared:
                _warn_declared_refusal("unsafe")
            return None
        return text
    except Exception:  # noqa: BLE001
        return None


def _warn_declared_refusal(reason: str) -> None:
    """Use the kit's existing startup-warning channel, once per reason."""
    if reason in _warned_declared_refusals:
        return
    _warned_declared_refusals.add(reason)
    try:
        from boosthis import start_announce

        if reason == "too-long":
            detail = (
                f"its length exceeds {MAX_PART_NAME} characters. Declare a "
                f"route name of {MAX_PART_NAME} characters or fewer."
            )
        else:
            detail = (
                "it is empty or carries a value. Declare a non-empty, "
                "code-defined route template such as /users/:id."
            )
        start_announce.say_after_startup_line(
            f"[boosthis] Boosthis refused a declared route name because {detail}"
        )
    except Exception:  # noqa: BLE001
        pass


# Kept as a private compatibility seam for existing integrations and tests.
def _safe_route_label(raw: Any) -> Optional[str]:
    return normalize_part_name(raw)


# ---------------------------------------------------------------------------
# Reading a framework
# ---------------------------------------------------------------------------


def _unreadable(source: str) -> Tuple[str, Optional[str], List[str]]:
    return ("unreadable", source, [])


def read_framework_routes(app: Any) -> Tuple[str, Optional[str], List[str]]:
    """Ask ``app`` for its whole route table.

    Returns ``(status, source, labels)`` where status is one of ``read``,
    ``unsupported`` (nothing here could be asked) or ``unreadable`` (the
    framework was recognised, its shape was not).
    """
    if app is None:
        return ("unsupported", None, [])
    try:
        framework = _framework_of(app)
        if framework is None:
            return ("unsupported", None, [])
        if framework in ("fastapi", "starlette"):
            return _read_starlette(app, framework)
        if framework == "flask":
            return _read_flask(app)
        if framework == "django":
            return _read_django()
        return ("unsupported", None, [])
    except Exception:  # noqa: BLE001 — never into the host
        return ("unreadable", None, [])


def _framework_of(app: Any) -> Optional[str]:
    """Name the framework from its module path, importing nothing.

    Deliberately a copy of ``mount._detect``'s discipline rather than a call
    into it: importing mount() from here would be a cycle, and only the four
    frameworks with a route table are named.
    """
    try:
        for cls in type(app).__mro__:
            module = (cls.__module__ or "").lower()
            if module == "fastapi" or module.startswith("fastapi."):
                return "fastapi"
            if module == "starlette" or module.startswith("starlette."):
                return "starlette"
            if module == "flask" or module.startswith("flask."):
                return "flask"
    except Exception:  # noqa: BLE001
        return None
    try:
        from boosthis import adapter_django

        if adapter_django.is_django_handler(app):
            return "django"
    except Exception:  # noqa: BLE001
        return None
    return None


def _read_starlette(app: Any, source: str) -> Tuple[str, Optional[str], List[str]]:
    """FastAPI and Starlette both expose ``app.routes`` — a public API.

    A ``Mount`` carries its own child routes, so the tree is walked and the
    prefixes joined. A route object without a readable path is an unfamiliar
    shape and loses the whole read.
    """
    routes = getattr(app, "routes", None)
    if not isinstance(routes, list):
        return _unreadable(source)
    labels: List[str] = []
    if not _walk_starlette(routes, "", labels, 0):
        return _unreadable(source)
    return ("read", source, labels)


def _walk_starlette(routes: Any, prefix: str, out: List[str], depth: int) -> bool:
    if depth > 10:
        return False
    if not isinstance(routes, list):
        return False
    for route in routes:
        children = getattr(route, "routes", None)
        if isinstance(children, list) and children:
            # A Mount / sub-application: its own path is the prefix for what
            # is under it, and is not itself a page. Its prefix comes from
            # ``path``, never ``path_format`` — a Mount's path_format carries
            # the catch-all it matches everything under it with
            # (``/api/v1/{path}``), which would land in the middle of every
            # child's label.
            mount_path = getattr(route, "path", None)
            if not isinstance(mount_path, str):
                return False
            if not _walk_starlette(children, prefix + mount_path, out, depth + 1):
                return False
            continue
        path = getattr(route, "path_format", None)
        if not isinstance(path, str):
            path = getattr(route, "path", None)
        if not isinstance(path, str):
            # Not a shape we know. Report nothing at all.
            return False
        whole = prefix + path
        if _is_kit_path(whole):
            continue
        label = _safe_route_label(whole)
        if label and label not in out:
            out.append(label)
    return True


def _read_flask(app: Any) -> Tuple[str, Optional[str], List[str]]:
    """Flask's URL map is public and iterable."""
    url_map = getattr(app, "url_map", None)
    iter_rules = getattr(url_map, "iter_rules", None)
    if not callable(iter_rules):
        return _unreadable("flask")
    labels: List[str] = []
    for rule in iter_rules():
        text = getattr(rule, "rule", None)
        if not isinstance(text, str):
            return _unreadable("flask")
        if _is_kit_path(text) or _is_flask_kit_rule(text):
            continue
        label = _safe_route_label(text)
        if label and label not in labels:
            labels.append(label)
    return ("read", "flask", labels)


def _read_django() -> Tuple[str, Optional[str], List[str]]:
    """Django's resolver can be walked to every registered pattern.

    ``get_resolver()`` is the same public entry point Django itself uses to
    route a request, so this asks the router rather than reading urls.py.
    """
    try:
        from django.urls import get_resolver  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return _unreadable("django")
    try:
        resolver = get_resolver()
        patterns = getattr(resolver, "url_patterns", None)
    except Exception:  # noqa: BLE001
        # A resolver that cannot be built (no settings, a bad URLconf) is a
        # read we could not take, not an app with no pages.
        return _unreadable("django")
    labels: List[str] = []
    if not _walk_django(patterns, "", labels, 0):
        return _unreadable("django")
    return ("read", "django", labels)


def _walk_django(patterns: Any, prefix: str, out: List[str], depth: int) -> bool:
    if depth > 10:
        return False
    if not isinstance(patterns, (list, tuple)):
        return False
    for entry in patterns:
        pattern = getattr(entry, "pattern", None)
        if pattern is None:
            return False
        try:
            fragment = str(pattern)
        except Exception:  # noqa: BLE001
            return False
        children = getattr(entry, "url_patterns", None)
        if children is not None:
            if not _walk_django(children, prefix + fragment, out, depth + 1):
                return False
            continue
        if getattr(entry, "callback", None) is None:
            return False
        whole = "/" + (prefix + fragment).lstrip("/")
        if _is_kit_path(whole):
            continue
        label = _safe_route_label(whole)
        if label and label not in out:
            out.append(label)
    return True


def _is_kit_path(path: str) -> bool:
    """The kit's own pages are not the app's pages."""
    if not _kit_prefix or _kit_prefix == "/":
        return False
    return path == _kit_prefix or path.startswith(_kit_prefix + "/")


def _is_flask_kit_rule(rule: str) -> bool:
    """Mounted at the root there is no prefix to test against, so the kit's
    own route table answers instead — the same boundary ``mount()`` uses."""
    if _kit_prefix and _kit_prefix != "/":
        return False
    try:
        from boosthis import kit_pages

        if rule.startswith(kit_pages.RULE_PREFIX):
            return True
        return any(rule == (suffix or "/") for suffix, _m, _g in kit_pages.ROUTES)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# The merged answer
# ---------------------------------------------------------------------------


def route_list_report() -> Dict[str, Any]:
    """The whole route list: what the framework said, merged with what the
    developer declared, each entry saying where it came from."""
    if not route_list_enabled():
        return {"status": "off", "entries": [], "total": 0}
    status, source, discovered = read_framework_routes(_app)
    from_by_label: Dict[str, str] = {}
    order: List[str] = []
    for label in discovered:
        if label not in from_by_label:
            from_by_label[label] = "framework"
            order.append(label)
    for label in _declared:
        if label in from_by_label:
            from_by_label[label] = "both"
        else:
            from_by_label[label] = "declared"
            order.append(label)
    order.sort()
    entries = [
        {"label": label, "from": from_by_label[label]}
        for label in order[:MAX_ROUTE_LIST_ENTRIES]
    ]
    report: Dict[str, Any] = {
        "status": status,
        "entries": entries,
        "total": len(order),
    }
    if source is not None and source in ROUTE_SOURCE_WORDS:
        report["source"] = source
    return report


def route_list_for_snapshot() -> Dict[str, Any]:
    """The block a snapshot carries. A kit that HAS this feature always answers.

    Every state is a sentence the map can print: ``read`` (here is the table),
    ``unsupported`` (nothing could be asked — either the framework offers no
    accessor or this kit was never handed an app), ``unreadable`` (we
    recognised the framework and not the shape it answered in) and ``off``
    (the developer switched the list off). An absent block means one thing
    only, and it must keep meaning only that: the kit running there is older
    than this feature.

    So "no app was handed over" is not silence. Saying nothing there would
    make a manually measured Flask app indistinguishable from a kit that
    cannot do this at all, and the page would have to hedge both ways at once.
    """
    try:
        return route_list_report()
    except Exception:  # noqa: BLE001
        # A read that failed is a read that failed — never silence, which the
        # map would have to word as "this kit said nothing".
        return {"status": "unreadable", "entries": [], "total": 0}
