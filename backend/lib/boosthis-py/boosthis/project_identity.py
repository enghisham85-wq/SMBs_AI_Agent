"""Which Boosthis project this kit reports to, in the developer's own words.

Only the server can resolve a project key to its developer-authored name, so
consent records the last answer here. The name is untrusted display data: it is
sanitised on arrival and every renderer treats the resulting display as text.
"""

from __future__ import annotations

import json
import re
from typing import Any

PROJECT_LABEL = "Project"
INSTALL_ID_LABEL = "Install ID"
INSTALL_ID_UNKNOWN_TEXT = "not assigned yet"
PROJECT_UNNAMED_TEXT = "Unnamed project"
PROJECT_UNKNOWN_TEXT = "Not received from Boosthis yet"
SCORE_CAPTION = "Measured on this device. Not proof anything reached Boosthis."

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_WHITESPACE_RE = re.compile(r"\s+")
_CODE_RE = re.compile(r"[^A-Za-z0-9]")
_MAX_NAME_CHARS = 60
_MAX_CODE_CHARS = 16

_current: dict[str, str | None] = {"name": None, "code": None}


def sanitize_project_name(value: Any) -> str | None:
    """Return a display-safe project name, or ``None`` when none remains."""
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_RE.sub(" ", value)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    cleaned = cleaned[:_MAX_NAME_CHARS].strip()
    return cleaned or None


def sanitize_project_code(value: Any) -> str | None:
    """Keep only the known short-code alphabet and length."""
    if not isinstance(value, str):
        return None
    cleaned = _CODE_RE.sub("", value)[:_MAX_CODE_CHARS]
    return cleaned or None


def set_kit_project(name: Any, code: Any) -> None:
    """Record a consent reply without blanking a previously known answer."""
    next_name = sanitize_project_name(name)
    next_code = sanitize_project_code(code)
    if next_code:
        _current["name"] = next_name
        _current["code"] = next_code
    elif next_name:
        _current["name"] = next_name


def get_kit_project() -> dict[str, str | None]:
    """Return a copy of the project identity currently held by this process."""
    return {"name": _current["name"], "code": _current["code"]}


def project_display(project: dict[str, str | None] | None = None) -> str:
    """Render the one spelling shared by every developer-facing surface."""
    try:
        value = project if project is not None else _current
        code = value.get("code")
        if not code:
            return PROJECT_UNKNOWN_TEXT
        return f"{value.get('name') or PROJECT_UNNAMED_TEXT} ({code})"
    except Exception:  # noqa: BLE001
        return PROJECT_UNKNOWN_TEXT


def serialize_kit_project(project: dict[str, str | None] | None = None) -> str:
    """Return the JSON form persisted beside this install's credentials."""
    value = project if project is not None else get_kit_project()
    return json.dumps(
        {"name": value.get("name"), "code": value.get("code")},
        separators=(",", ":"),
    )


def parse_kit_project(raw: Any) -> dict[str, str | None] | None:
    """Read the persisted form; malformed or empty values mean no stored fact."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        name = sanitize_project_name(value.get("name"))
        code = sanitize_project_code(value.get("code"))
        if not name and not code:
            return None
        return {"name": name, "code": code}
    except Exception:  # noqa: BLE001
        return None


def _reset_kit_project() -> None:
    """Test/erasure seam: forget the last server-resolved project."""
    _current["name"] = None
    _current["code"] = None
    _promises.clear()
    _promise_total[0] = 0


# ── The developer's own standing promises ──────────────────────────────────
#
# A promise is the one thing in Boosthis that IS a standing instruction, and
# every surface carrying it was outside the app. The kit serves a page inside
# the developer's own app, so the promises ride the consent reply the project
# name already rides.
#
# The wording is untrusted display data, screened here and rendered as text.
# The standing is a CLOSED token, never a sentence: the words for "watched"
# and "remembered only" are literals in the page's own source, held byte-equal
# to the server's vocabulary by a test. No verdict travels — the reply is read
# at registration, and a stale "broken" would be a claim this page cannot
# stand behind.

# Deliberately the SAME number the server refuses a longer promise at. A
# smaller cap here would quietly rewrite the developer's own standing
# instruction — the one piece of text on this page that is theirs and not
# ours — with nothing to show it had happened. So nothing the server accepted
# is ever trimmed, and anything longer than it would store is dropped whole.
# Held to the server's own limit by a test in the scripts package.
_MAX_PROMISE_CHARS = 240
_MAX_PROMISES = 6
_STANDINGS = ("watched", "remembered-only")

_promises: list[dict[str, str]] = []
_promise_total = [0]


def sanitize_promise_text(value: Any) -> str | None:
    """Same screen as a project name: control characters out, whitespace
    collapsed, capped. ``None`` when nothing readable is left."""
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_RE.sub(" ", value)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    # Whole, or not at all. A promise longer than the server stores did not
    # come from the promise store, and half of someone's standing instruction
    # is not a shorter version of it.
    if not cleaned or len(cleaned) > _MAX_PROMISE_CHARS:
        return None
    return cleaned


def set_kit_promises(raw: Any) -> None:
    """Record the promises off a consent reply.

    An unrecognised standing is dropped rather than rendered as one of the two
    this kit knows.

    THE REPLY IS THE ANSWER, whatever it says. A list replaces what was held,
    an empty list clears it, and a reply carrying no promises at all clears it
    too: the server sends the empty list explicitly when a project holds none,
    so nothing arriving means Boosthis could not read them — and a promise we
    cannot confirm is still held is exactly the one this page must stop
    showing. Unlike the project name, which is an identity that does not stop
    being true, a promise is a live list the developer edits."""
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        _promises.clear()
        _promise_total[0] = 0
        return
    kept: list[dict[str, str]] = []
    for item in items[:_MAX_PROMISES]:
        if not isinstance(item, dict):
            continue
        text = sanitize_promise_text(item.get("text"))
        standing = item.get("standing")
        if not text or standing not in _STANDINGS:
            continue
        kept.append({"text": text, "standing": str(standing)})
    _promises[:] = kept
    total = raw.get("total")
    total = int(total) if isinstance(total, int) and total > 0 else 0
    # Never below what is being shown: a total that disagreed with the list
    # would have the page claim it is hiding a negative number of promises.
    _promise_total[0] = max(len(kept), total)


def get_kit_promises() -> dict[str, Any]:
    """What this process currently holds. Never raises."""
    return {
        "total": _promise_total[0],
        "items": [dict(p) for p in _promises],
    }


def serialize_kit_promises() -> str:
    """The JSON form persisted beside this install's credentials."""
    return json.dumps(get_kit_promises(), separators=(",", ":"))


def parse_kit_promises(raw: Any) -> Any:
    """Read the persisted form; anything unreadable means nothing stored."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None