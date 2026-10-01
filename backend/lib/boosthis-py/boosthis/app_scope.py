"""Boosthis: a stable per-project scope key for the little state the meters
keep on disk (Python).

Today exactly one meter needs a file that outlives the process: the
worker-recycling history (``extra_meters``), which is how a gunicorn/uWSGI
worker can tell "I am being recycled every four minutes" from "I booted four
minutes ago". That file MUST be scoped to the project/install that wrote it —
several Boosthis-instrumented Python services routinely share one machine
account, and a single machine-wide file would blend their boot histories and
report restarts that never happened.

Scope inputs, most explicit first:

1. ``BOOSTHIS_APP_SCOPE`` — an explicit label the host can set. The escape hatch
   for the one case the automatic inputs cannot separate: two deployments of the
   SAME service, from the same directory, under the same user.
2. the install id — ``BOOSTHIS_INSTALL_ID`` or the persisted
   ``~/.boosthis/config.json``. Read straight off disk (never through
   ``telemetry``) so this module stays free of import cycles: everything that
   writes meter state is imported *by* telemetry.
3. the project's own identity — the entry script's directory, else the CWD.

The three are hashed together, so what lands on disk is an opaque short digest:
no path, username, or install id is ever written in readable form. Every worker
of one deployment computes the SAME key (they share argv/cwd/install id), which
is exactly what the worker-recycling meter needs.

Never raises: a scope always resolves, worst case to a digest of empty inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Optional

# Where every piece of Boosthis local state lives (config, entitlement cache,
# candidates, and the meters' own files).
STATE_DIR = Path.home() / ".boosthis"

# Length of the hex digest used in file names. 16 hex chars (64 bits) is far
# more than enough to keep two projects on one machine apart and keeps the file
# name readable.
_SCOPE_HEX_LEN = 16

_lock = threading.Lock()
_cached_scope: Optional[str] = None


def _read_install_id() -> str:
    """The current install id — the SAME answer the registering half reaches.

    Empty string when unknown (a kit that has never registered). Never raises,
    and never mints: a scope key is not a reason to become a new install.

    This asks ``telemetry.known_install_id`` rather than reading a file, because
    two resolvers that disagree is the defect this exists to avoid: the store is
    a ladder (a configured directory, the home directory, a scratch directory),
    so a fixed ``~/.boosthis/config.json`` read finds nothing on exactly the
    hosts where the ladder had to fall back — and then scopes the meter state to
    a different key than the one the kit registered under.

    The import is deferred and defensive on purpose: everything that writes
    meter state is imported *by* ``telemetry``, so a module-level import here
    would be a cycle. The direct read stays as the fallback for the window
    during which ``telemetry`` is still initialising.
    """
    try:
        from boosthis.telemetry import known_install_id

        known = known_install_id()
        if known:
            return known
    except Exception:  # noqa: BLE001 — mid-import, or a kit built without it.
        pass
    # Only reached when the shared resolver has NO answer. Local reading may
    # fill an unanswered gap; it may never contradict an answer that exists.
    try:
        env = (os.environ.get("BOOSTHIS_INSTALL_ID") or "").strip()
        if env:
            return env
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = json.loads((STATE_DIR / "config.json").read_text())
        if isinstance(raw, dict):
            value = raw.get("install_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _project_path() -> str:
    """A stable path identifying THIS project: the entry script's directory,
    else the working directory. Hashed, never stored. Never raises."""
    try:
        argv0 = sys.argv[0] if sys.argv else ""
        if argv0:
            parent = str(Path(argv0).resolve().parent)
            if parent and parent != os.sep:
                return parent
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(Path.cwd())
    except Exception:  # noqa: BLE001
        return ""


def _compute_scope() -> str:
    try:
        explicit = (os.environ.get("BOOSTHIS_APP_SCOPE") or "").strip()
    except Exception:  # noqa: BLE001
        explicit = ""
    material = "\x1f".join([explicit, _read_install_id(), _project_path()])
    try:
        digest = hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()
    except Exception:  # noqa: BLE001
        return "default"
    return digest[:_SCOPE_HEX_LEN]


def scope_key() -> str:
    """The opaque per-project scope key. Computed once per process (every worker
    of one deployment resolves the same value) and cached."""
    global _cached_scope
    with _lock:
        if _cached_scope is None:
            _cached_scope = _compute_scope()
        return _cached_scope


def scoped_path(stem: str, suffix: str = ".json") -> Path:
    """Path for a per-project state file: ``~/.boosthis/<stem>.<scope><suffix>``."""
    return STATE_DIR / f"{stem}.{scope_key()}{suffix}"


def reset_app_scope() -> None:
    """Drop the cached key so the next read recomputes it. Called after erasure
    (``forget()`` deletes the config, so a later enable gets a fresh install id
    and therefore a fresh scope) and by tests."""
    global _cached_scope
    with _lock:
        _cached_scope = None


__all__ = ["STATE_DIR", "scope_key", "scoped_path", "reset_app_scope"]
