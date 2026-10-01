"""Community rule book client (read-only, fail-open).

Reads a privacy-safe AGGREGATE of fixes proven across ALL projects from the
public ``GET /rules/community`` endpoint and caches it on disk so the matcher
can recommend real-world-proven fixes. This is the read side of Boosthis's
global self-learning loop.

It is strictly read-only and fail-open: it only GETs the public aggregate
(rule ids + buckets + counts -- no code, no route names, no values, no install
id), and ANY error (offline, missing cache, bad JSON, rate limited) silently
yields an empty book so the matcher falls back to its static ranking. Nothing
here ever transmits OUT.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_TTL_S = 6 * 60 * 60
_lock = threading.Lock()
_refreshing = False


def _endpoint() -> str:
    # ``or`` (not a default-arg) so a blank env var never shadows the fallback.
    raw = (
        os.environ.get("BOOSTHIS_ENDPOINT")
        or os.environ.get("BOOSTEN_ENDPOINT")
        or "https://www.boosthis.com/api"
    )
    return raw.rstrip("/")


def _cache_file(runtime: str) -> Path:
    return Path.home() / ".boosthis" / f"community.{runtime}.json"


def _describe(sev: object, ct: object) -> str:
    s = f"{sev} severity" if sev else "any severity"
    c = f"{ct} occurrences" if ct else "any volume"
    return f"{s} \u00b7 {c}"


def read_community_cache(runtime: str = "py") -> dict[str, dict]:
    """Synchronously read the cached book, rolled up per ruleId.

    Returns an empty dict on any failure (the matcher then uses static ranking
    only).
    """
    out: dict[str, dict] = {}
    try:
        data = json.loads(_cache_file(runtime).read_text(encoding="utf-8"))
    except Exception:
        return out
    for r in data.get("rules", []) or []:
        rid = r.get("ruleId")
        if not isinstance(rid, str):
            continue
        cur = out.setdefault(
            rid, {"projects": 0, "evidence": 0, "circumstances": []}
        )
        # projects is a distinct-project count PER circumstance row, so the
        # strongest single circumstance is the honest per-rule confidence;
        # evidence accumulates across circumstances.
        cur["projects"] = max(cur["projects"], int(r.get("projects", 0) or 0))
        cur["evidence"] += int(r.get("evidence", 0) or 0)
        desc = _describe(r.get("severityBucket"), r.get("countBucket"))
        if desc not in cur["circumstances"]:
            cur["circumstances"].append(desc)
    return out


def refresh_community_cache_if_stale(runtime: str = "py") -> None:
    """Best-effort background refresh; never blocks, never raises, skips tests."""
    global _refreshing
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("BOOSTHIS_DISABLED"):
        return
    cache = _cache_file(runtime)
    try:
        if cache.exists() and (time.time() - cache.stat().st_mtime) < _TTL_S:
            return
    except OSError:
        pass
    with _lock:
        if _refreshing:
            return
        _refreshing = True
    threading.Thread(
        target=_fetch_and_write, args=(runtime,), daemon=True
    ).start()


def _invite_key() -> str | None:
    """The single project key this process may present to the fix endpoint.

    The developer sets it once (the same project key they registered telemetry
    with). ``or`` (not a default-arg) so a blank var falls through. The invite
    key -- not a per-install token -- is the uniform credential so the same
    flow works for RN, Node, and Python.
    """
    return (
        os.environ.get("BOOSTHIS_INVITE_KEY")
        or os.environ.get("BOOSTEN_INVITE_KEY")
        or None
    )


_NO_KEY_NOTE = (
    "Fix guidance is served per-rule from the Boosthis server and requires a "
    "registered (invited) app. Set BOOSTHIS_INVITE_KEY in this process's "
    "environment to receive the fix. Detection (when_to_apply) works fully "
    "offline without it."
)


def _sanitize_counterparts(raw: object) -> list:
    """Validate the server's cross-language counterparts list into the closed
    snake_case wire shape (``runtime`` / ``rule_id`` / ``concept``).

    These name the SAME idea's rule in OTHER languages — only rules that
    actually exist are ever served. Untrusted input: anything malformed is
    dropped, strings are capped, and at most 5 entries survive (one per other
    language)."""
    if not isinstance(raw, list):
        return []
    out: list = []
    for item in raw:
        if len(out) >= 5:
            break
        if not isinstance(item, dict):
            continue
        runtime = item.get("runtime")
        rule_id = item.get("ruleId")
        concept = item.get("concept")
        if (
            isinstance(runtime, str)
            and isinstance(rule_id, str)
            and isinstance(concept, str)
        ):
            out.append(
                {
                    "runtime": runtime[:10],
                    "rule_id": rule_id[:100],
                    "concept": concept[:200],
                }
            )
    return out


def fetch_rule_fix(rule_id: str, runtime: str = "py") -> dict:
    """Fetch ONE rule's prescriptive fix from ``GET /rules/fix`` (bearer auth).

    The fix text is deliberately NOT bundled in this package, so it is fetched
    per-rule and fails GRACEFULLY: detection (``when_to_apply``) always works
    offline; only the written fix needs an invited app + a connection. Never
    raises -- on any failure it returns ``{"fix_available": False, "fix_note":
    ...}``. The corpus is never fetched in bulk.
    """
    key = _invite_key()
    if not key:
        return {"fix_available": False, "fix_note": _NO_KEY_NOTE}
    try:
        q = urllib.parse.urlencode({"ruleId": rule_id, "runtime": runtime})
        url = f"{_endpoint()}/rules/fix?{q}"
        req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            url,
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                body = resp.read(100_000)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                return {
                    "fix_available": False,
                    "fix_note": (
                        "The Boosthis server rejected this project key (403). "
                        "Check BOOSTHIS_INVITE_KEY."
                    ),
                }
            if e.code == 404:
                return {
                    "fix_available": False,
                    "fix_note": f"The server has no fix for rule '{rule_id}'.",
                }
            if e.code == 429:
                return {
                    "fix_available": False,
                    "fix_note": "Fix endpoint is rate limited right now; retry shortly.",
                }
            return {
                "fix_available": False,
                "fix_note": f"The Boosthis server returned {e.code}; retry shortly.",
            }
        data = json.loads(body)
        tmpl = data.get("fixTemplate")
        if not isinstance(tmpl, str):
            return {"fix_available": False, "fix_note": "Malformed fix response."}
        out: dict = {"fix_available": True, "fix_template": tmpl}
        comm = data.get("community")
        if isinstance(comm, dict) and int(comm.get("projects", 0) or 0) > 0:
            out["community_proven"] = True
            out["community_projects"] = int(comm.get("projects", 0) or 0)
            out["community_evidence"] = int(comm.get("evidence", 0) or 0)
        counterparts = _sanitize_counterparts(data.get("counterparts"))
        if counterparts:
            out["counterparts"] = counterparts
        return out
    except Exception:
        return {
            "fix_available": False,
            "fix_note": (
                "Could not reach the Boosthis server (offline?). Detection "
                "still works offline; fetch the fix when back online."
            ),
        }


def _fetch_and_write(runtime: str) -> None:
    global _refreshing
    try:
        url = f"{_endpoint()}/rules/community?runtime={runtime}"
        # The community book is now invite-gated (registered apps only). Present
        # the same invite key used for fix fetches when one is configured;
        # without it the server returns 403 and we keep the last good disk
        # cache (fail-open).
        headers = {"accept": "application/json"}
        key = _invite_key()
        if key:
            headers["authorization"] = f"Bearer {key}"
        req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            url, headers=headers
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            body = resp.read(1_000_000)
        json.loads(body)  # validate before persisting
        cache = _cache_file(runtime)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(body)
    except Exception:
        # offline / bad JSON / rate limited -> keep last good cache
        pass
    finally:
        with _lock:
            _refreshing = False
