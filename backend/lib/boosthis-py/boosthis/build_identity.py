"""Boosthis: build identity + Patch Lag ("exposure window") — Python.

Cross-runtime "Patch Lag" meter. It reports how stale the running build is —
how long ago it was built — as an ADDITIVE, display-only axis. It NEVER implies
the app is safe or patched: the meter reports lag, nothing more.

Two products live here:

- **build identity** — a small ``{commit?, buildTimeMs?, buildAgeMs?}`` object
  captured ONCE at telemetry-enable and FROZEN. Fabrication is forbidden:
  unknown fields are omitted, and the whole object is omitted when nothing is
  known.
- **patchLag axis** — emitted ONLY when a build TIME is known (honest absence
  otherwise — never a fake/warming axis). ``ageDays = buildAgeMs/86400000``;
  ``good`` if ageDays < 7, ``needs-work`` if < 30, else ``poor``; the score is
  ``round(clamp(100 - (ageDays/60)*100, 0, 100))``. Additive: it must NEVER feed
  the composite Speed score.

Build-identity resolution precedence (no fabrication, first known wins):
  a. Env overrides: ``BOOSTHIS_BUILD_COMMIT`` (validated hex 7-40, lowercased)
     and ``BOOSTHIS_BUILD_TIME`` (epoch ms if > 1e12, epoch seconds if numeric
     otherwise, or an ISO-8601 date string).
  b. Platform source: none beyond env on Python.
  c. Fallback: ``buildTimeMs`` = mtime of the main entry file
     (``sys.modules['__main__'].__file__`` if set, else ``sys.argv[0]``),
     guarded.

Dependency inventory — OPT-IN, OFF BY DEFAULT. Gate: ``BOOSTHIS_DEP_INVENTORY=1``.
When on, the snapshot carries a top-level ``deps`` array of
``{name, version}`` from ``importlib.metadata.distributions()``, capped at 150
entries, sorted by name, with unparseable entries skipped. When off: no field.
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

from .health_axes import rating_for
from .runtime_flags import is_boosthis_disabled

# ── patchLag bands ──────────────────────────────────────────────────────────
# ageDays -> rating: good < 7d, needs-work < 30d, else poor. The 0-100 score is
# a linear decay over a 60-day window: a fresh build (0d) scores 100, a build 60
# days old (or older) scores 0.
PATCH_LAG_GOOD_DAYS = 7
PATCH_LAG_NEEDS_WORK_DAYS = 30
PATCH_LAG_SCORE_WINDOW_DAYS = 60
_MS_PER_DAY = 86_400_000

# Dependency inventory caps + validation (server allowlist parity).
DEP_INVENTORY_MAX = 150
_DEP_NAME_RE = re.compile(r"^[A-Za-z0-9@/._:-]{1,100}$")
_DEP_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z._+-]{0,49}$")

# Commit hash: lowercase hex, 7-40 chars.
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")

# build identity captured ONCE at telemetry-enable and FROZEN. None until
# capture_build_identity() runs; a second capture never overwrites (idempotent).
_build: Optional[Dict[str, Any]] = None

# Test override — a callable returning the entry-file mtime in ms (or raising to
# exercise the unreadable path). None reads the live filesystem.
_entry_mtime_reader_override: Optional[Any] = None
# Test override for the current wall-clock at capture (ms). None reads live.
_now_ms_override: Optional[int] = None


def _now_ms() -> int:
    if _now_ms_override is not None:
        return int(_now_ms_override)
    return int(time.time() * 1000)


def parse_commit(raw: Optional[str]) -> Optional[str]:
    """Validate + lowercase a commit hash. Returns the lowercased hex when it is
    7-40 hex chars, else None (reject anything else). Never raises."""
    try:
        if not isinstance(raw, str):
            return None
        v = raw.strip().lower()
        if _COMMIT_RE.match(v):
            return v
        return None
    except Exception:  # noqa: BLE001
        return None


def parse_build_time_ms(raw: Optional[str]) -> Optional[int]:
    """Parse a ``BOOSTHIS_BUILD_TIME`` value into epoch ms.

    Accepts (first match wins):
      - a numeric epoch value: > 1e12 is treated as epoch ms, otherwise as epoch
        seconds (× 1000);
      - an ISO-8601 date/datetime string.
    Returns None on anything unparseable or non-positive. Never raises."""
    try:
        if raw is None:
            return None
        s = str(raw).strip()
        if not s:
            return None
        # Numeric epoch (int or float): ms if > 1e12, else seconds.
        try:
            num = float(s)
            ms = num if num > 1e12 else num * 1000.0
            ms_int = int(ms)
            return ms_int if ms_int > 0 else None
        except (ValueError, TypeError):
            pass
        # ISO-8601 date string. Accept a trailing "Z" (UTC).
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        import datetime as _dt

        dt = _dt.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        ms_int = int(dt.timestamp() * 1000)
        return ms_int if ms_int > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _entry_file_mtime_ms() -> Optional[int]:
    """Fallback build time: mtime (ms) of the main entry file
    (``sys.modules['__main__'].__file__`` if set, else ``sys.argv[0]``). Guarded
    — returns None on anything unreadable. Never raises."""
    if _entry_mtime_reader_override is not None:
        try:
            return _entry_mtime_reader_override()
        except Exception:  # noqa: BLE001
            return None
    try:
        path: Optional[str] = None
        main = sys.modules.get("__main__")
        main_file = getattr(main, "__file__", None) if main is not None else None
        if isinstance(main_file, str) and main_file:
            path = main_file
        elif sys.argv and isinstance(sys.argv[0], str) and sys.argv[0]:
            path = sys.argv[0]
        if not path:
            return None
        return int(os.path.getmtime(path) * 1000)
    except Exception:  # noqa: BLE001
        return None


def resolve_build_identity(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Resolve the ``{commit?, buildTimeMs?, buildAgeMs?}`` object from the
    precedence chain (env override → platform (none) → entry-file fallback).

    Never fabricates: omits unknown fields; returns ``{}`` when nothing is known.
    ``buildAgeMs`` is ``now - buildTimeMs`` clamped ≥ 0 (present only when a build
    time is known). Pure — reads env + filesystem, never mutates state. Never
    raises."""
    out: Dict[str, Any] = {}
    try:
        e = env if env is not None else os.environ

        commit = parse_commit(e.get("BOOSTHIS_BUILD_COMMIT"))
        if commit is not None:
            out["commit"] = commit

        build_time_ms = parse_build_time_ms(e.get("BOOSTHIS_BUILD_TIME"))
        if build_time_ms is None:
            build_time_ms = _entry_file_mtime_ms()

        if build_time_ms is not None and build_time_ms > 0:
            out["buildTimeMs"] = int(build_time_ms)
            out["buildAgeMs"] = max(0, _now_ms() - int(build_time_ms))
    except Exception:  # noqa: BLE001
        return out
    return out


def capture_build_identity(env: Optional[Dict[str, str]] = None) -> None:
    """Resolve + FREEZE the build identity ONCE at telemetry-enable. IDEMPOTENT
    — a second call never overwrites a stored value. Guest-safe: never raises.
    Wired best-effort into ``enable_telemetry`` (a failure must never break
    enable)."""
    global _build
    try:
        if _build is not None:
            return  # already frozen — never overwrite
        _build = resolve_build_identity(env)
    except Exception:  # noqa: BLE001
        pass  # best-effort — never fatal to the host


def read_build() -> Optional[Dict[str, Any]]:
    """The frozen build-identity object for the snapshot top-level ``build``
    field, or None when nothing is known (omit the whole object). Pure read;
    never raises."""
    if is_boosthis_disabled():
        return None
    try:
        if not _build:
            return None
        return dict(_build)
    except Exception:  # noqa: BLE001
        return None


def read_patch_lag() -> Optional[Dict[str, Any]]:
    """The patchLag axis, or None when no build TIME is known (honest absence —
    never a fake/warming axis). ``depCount`` is included only when the dependency
    inventory opt-in is ON. Additive, display-only — never feeds the Speed score.
    Pure read; never raises."""
    if is_boosthis_disabled():
        return None
    try:
        build = _build
        if not build:
            return None
        build_time_ms = build.get("buildTimeMs")
        build_age_ms = build.get("buildAgeMs")
        if not isinstance(build_time_ms, int) or not isinstance(build_age_ms, int):
            return None
        age_days = build_age_ms / _MS_PER_DAY
        if age_days < PATCH_LAG_GOOD_DAYS:
            rating = "good"
        elif age_days < PATCH_LAG_NEEDS_WORK_DAYS:
            rating = "needs-work"
        else:
            rating = "poor"
        raw = 100 - (age_days / PATCH_LAG_SCORE_WINDOW_DAYS) * 100
        score = round(max(0.0, min(100.0, raw)))
        axis: Dict[str, Any] = {
            "score": score,
            "rating": rating,
            "buildAgeMs": build_age_ms,
            "buildTimeMs": build_time_ms,
        }
        if dep_inventory_enabled():
            deps = collect_deps()
            if deps is not None:
                axis["depCount"] = len(deps)
        return axis
    except Exception:  # noqa: BLE001
        return None


def dep_inventory_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """The dependency-inventory opt-in gate — OFF by default. Only the exact
    value ``BOOSTHIS_DEP_INVENTORY=1`` turns it on. Never raises."""
    try:
        e = env if env is not None else os.environ
        return e.get("BOOSTHIS_DEP_INVENTORY") == "1"
    except Exception:  # noqa: BLE001
        return False


def collect_deps() -> Optional[List[Dict[str, str]]]:
    """The dependency inventory: ``[{name, version}]`` from
    ``importlib.metadata.distributions()``, validated (name matches
    ``[A-Za-z0-9@/._:-]{1,100}``; version starts with a digit and matches
    ``[0-9A-Za-z._+-]{1,50}``), sorted by name, capped at 150. Skips entries that
    don't validate. Returns None on a total read failure so the caller omits the
    field. Never raises."""
    try:
        from importlib import metadata as _md

        seen: Dict[str, str] = {}
        for dist in _md.distributions():
            try:
                name = dist.metadata["Name"]
                version = dist.version
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(name, str) or not isinstance(version, str):
                continue
            name = name.strip()
            version = version.strip()
            if not _DEP_NAME_RE.match(name) or not _DEP_VERSION_RE.match(version):
                continue
            # First occurrence wins (dedupe repeated dists deterministically).
            if name not in seen:
                seen[name] = version
        out = [
            {"name": n, "version": seen[n]} for n in sorted(seen.keys())
        ]
        return out[:DEP_INVENTORY_MAX]
    except Exception:  # noqa: BLE001
        return None


def clear_build_identity() -> None:
    """Drop all build-identity state (wired into ``forget()``). Idempotent.
    Never raises."""
    global _build, _entry_mtime_reader_override, _now_ms_override
    _build = None
    _entry_mtime_reader_override = None
    _now_ms_override = None


# ── @internal test hooks ────────────────────────────────────────────────────
def _set_entry_mtime_reader_for_tests(reader: Optional[Any]) -> None:
    """Inject a fake entry-file mtime reader: a zero-arg callable returning the
    mtime in ms (or raising to exercise the unreadable path). None reads live."""
    global _entry_mtime_reader_override
    _entry_mtime_reader_override = reader


def _set_now_ms_for_tests(now_ms: Optional[int]) -> None:
    """Pin the wall-clock (ms) used at capture so buildAgeMs is deterministic.
    None reads live."""
    global _now_ms_override
    _now_ms_override = now_ms


def _set_build_for_tests(build: Optional[Dict[str, Any]]) -> None:
    """Directly seed (or clear, with None) the frozen build-identity object."""
    global _build
    _build = build


def _reset_build_identity_for_tests() -> None:
    """Clear module state between hermetic test runs."""
    global _build, _entry_mtime_reader_override, _now_ms_override
    _build = None
    _entry_mtime_reader_override = None
    _now_ms_override = None


__all__ = [
    "resolve_build_identity",
    "capture_build_identity",
    "read_build",
    "read_patch_lag",
    "parse_commit",
    "parse_build_time_ms",
    "dep_inventory_enabled",
    "collect_deps",
    "clear_build_identity",
]
