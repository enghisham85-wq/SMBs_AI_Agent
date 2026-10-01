"""Named perf snapshots — the data layer for "compare two runs".

Captures a moment-in-time copy of ``samples.summary()`` to disk so you can
compare what production looked like before / after a deploy, or before /
after applying a Boosthis fix. Pure local files — no network, no PII.

Storage: ``~/.boosthis/snapshots/<name>.json``. Each snapshot is a single
JSON object with the summary payload plus ``saved_at`` and ``name``.

CLI shape (wired up in ``cli.py``):

  boosthis snapshot save <name>        — capture current samples
  boosthis snapshot list               — list saved snapshots
  boosthis snapshot show <name>        — print a snapshot
  boosthis snapshot diff <a> <b>       — per-route max-latency deltas between two
  boosthis snapshot delete <name>      — remove one
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from boosthis import samples

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _dir() -> Path:
    p = Path(os.path.expanduser("~/.boosthis/snapshots"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _validate(name: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError(
            f"snapshot name must match {NAME_RE.pattern} (got {name!r})"
        )


def save(name: str) -> dict[str, Any]:
    """Capture the current summary to a named snapshot. Overwrites."""
    _validate(name)
    payload = {
        "name": name,
        "saved_at": int(time.time() * 1000),
        "summary": samples.summary(),
    }
    path = _dir() / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2))
    return payload


def load(name: str) -> dict[str, Any]:
    _validate(name)
    path = _dir() / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"no snapshot named {name!r}")
    return json.loads(path.read_text())


def list_snapshots() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(_dir().glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        out.append({
            "name": payload.get("name", path.stem),
            "saved_at": payload.get("saved_at"),
            "total": payload.get("summary", {}).get("total", 0),
        })
    return out


def delete(name: str) -> bool:
    _validate(name)
    path = _dir() / f"{name}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def diff(name_a: str, name_b: str) -> dict[str, Any]:
    """Compare two snapshots. Returns per-route deltas + a verdict.

    Verdicts:
      regressed — route's max_ms in B is ≥ 1.5× max_ms in A
                  (same 1.5× threshold budgets.py uses for p95 regression;
                  we use max_ms here because that's what summary() stores
                  per-route — top-level p50/p95/p99 deltas are reported
                  separately in the diff's *_delta_ms fields)
      improved  — route's max_ms in B is ≤ 0.66× max_ms in A
      stable    — neither
      new       — route exists only in B (likely a new route)
      gone      — route exists only in A
    """
    a = load(name_a)["summary"]
    b = load(name_b)["summary"]
    # Python samples.summary() uses "byRoute" (camelCase, parity with the
    # Node + RN summary shape).
    routes_a = a.get("byRoute", {}) or {}
    routes_b = b.get("byRoute", {}) or {}
    all_routes = sorted(set(routes_a) | set(routes_b))
    rows: list[dict[str, Any]] = []
    for route in all_routes:
        ra = routes_a.get(route)
        rb = routes_b.get(route)
        if ra is None:
            rows.append({"route": route, "verdict": "new", "max_ms_b": rb["max_ms"]})
            continue
        if rb is None:
            rows.append({"route": route, "verdict": "gone", "max_ms_a": ra["max_ms"]})
            continue
        # We don't store per-route p95 in summary; fall back to max_ms as
        # the comparable knob (samples.summary() exposes max per route).
        a_max = ra["max_ms"]
        b_max = rb["max_ms"]
        verdict = "stable"
        if a_max > 0 and b_max >= a_max * 1.5:
            verdict = "regressed"
        elif a_max > 0 and b_max <= a_max * 0.66:
            verdict = "improved"
        rows.append({
            "route": route,
            "verdict": verdict,
            "max_ms_a": a_max,
            "max_ms_b": b_max,
            "delta_ms": b_max - a_max,
        })
    return {
        "a": name_a,
        "b": name_b,
        "p50_delta_ms": _none_minus(b.get("p50_ms"), a.get("p50_ms")),
        "p95_delta_ms": _none_minus(b.get("p95_ms"), a.get("p95_ms")),
        "p99_delta_ms": _none_minus(b.get("p99_ms"), a.get("p99_ms")),
        "rows": rows,
    }


def _none_minus(x: Any, y: Any) -> Any:
    if x is None or y is None:
        return None
    return x - y
