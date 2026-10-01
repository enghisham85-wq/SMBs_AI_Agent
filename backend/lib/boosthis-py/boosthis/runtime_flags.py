"""Global runtime kill-switch — Python sibling of `runtimeFlags.ts`.

If the environment variable ``BOOSTHIS_DISABLED`` is set to a truthy value
("1", "true", "yes", "on") then every Boosthis hot path becomes a no-op:

  - ``@track_perf`` / ``perf()`` skip recording.
  - ``safe_transmit()`` refuses to send and returns 204 without touching
    the network.

The check is re-evaluated on every call so the flag can be toggled at
runtime (useful for tests and for AI agents that flip the switch on
sensitive workloads).
"""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_boosthis_disabled() -> bool:
    """Return True if Boosthis should be entirely silent."""
    v = os.environ.get("BOOSTHIS_DISABLED") or os.environ.get("BOOSTEN_DISABLED")
    if v is None:
        return False
    return v.strip().lower() in _TRUTHY


def is_collection_gated() -> bool:
    """Return True when NOTHING may be collected right now.

    Wider than :func:`is_boosthis_disabled`: it also covers the server-authority
    entitlement gate (revoked / unpaid / paused / tampered / offline-grace
    lapsed / never activated). Every RECORDING path consults this at record
    time — not only at start time — so flipping the kill switch, or an account
    going unpaid mid-run, means the meters stop collecting immediately rather
    than merely stop displaying.

    The kill-switch client is imported lazily (it imports this module, so a
    module-level import would be a cycle) and any failure degrades to the env
    flag alone. Never raises.
    """
    if is_boosthis_disabled():
        return True
    try:
        from boosthis.kill_switch import is_runtime_inert
    except Exception:  # noqa: BLE001
        return False
    try:
        return bool(is_runtime_inert())
    except Exception:  # noqa: BLE001
        return False
