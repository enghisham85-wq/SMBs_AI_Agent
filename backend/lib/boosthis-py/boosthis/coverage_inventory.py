"""What this kit reached in this app, and what it found and cannot watch.

A developer looking at a page of green meters reasonably assumes we are
watching their whole app. We are not, and this kit already KNOWS where it is
blind: ``arm_job_systems`` meets Dramatiq and calls
:func:`~boosthis.job_work.note_job_system_unattached`, ``host_surface`` names
the readings a Streamlit rerun can never produce, and nothing in this package
has ever watched a database query. Every one of those facts used to die inside
the process that learned it.

This module adds them up into one small inventory that rides the existing
registration. It is a READER: it starts nothing, patches nothing, imports
nothing on the app's behalf and asks nothing to arm. Everything here is already
known for some other reason.

Three rules it exists to keep:

1. POSITIVE EVIDENCE BOTH WAYS. A surface is reported as WATCHED only once
   something has actually come through it, and as UNWATCHED only when the kit
   really found the thing present in this process. An app with no queue system
   has no queue gap. A surface with no evidence either way is left OUT of both
   lists, where the server renders it as "we have not looked", never as
   "clean".
2. NO CUSTOMER NAMES. What leaves this process is a term from
   :mod:`boosthis.watchable_surfaces` and a small count. Never a module, class,
   route or function name — not even the name of the library we cannot watch.
3. NO PERCENTAGES. We do not know the denominator, and inventing one would be
   the self-flattering meter we tell customers not to trust.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Tuple

#: Database libraries whose PRESENCE in this process is evidence that this app
#: does database work. Code-defined and closed: the name is used to look in
#: ``sys.modules`` and is then thrown away — it never travels.
#:
#: This kit has no database reader at all, so every one of these is a blind
#: spot rather than a "could not patch it". Checking for them is a lookup in a
#: dict Python already maintains; nothing is imported on the app's behalf.
_DB_MODULES: Tuple[str, ...] = (
    "aiosqlite",
    "asyncpg",
    "MySQLdb",
    "mysql.connector",
    "psycopg",
    "psycopg2",
    "pymongo",
    "pymysql",
    "sqlalchemy",
    "sqlite3",
)

#: Axis keys whose absence means we cannot see this app's own reply at all.
#: When the mounted host makes these unmeasurable, response caching is not a
#: reading we are failing to take — it is one this kind of app cannot produce.
_RESPONSE_AXES: Tuple[str, ...] = ("reliability", "cookieExposure")


def _count(fn: Any) -> int:
    """A reader's number, or 0 — one blind reader must not cost the inventory."""
    try:
        value = fn()
        n = int(value)
        return n if n > 0 else 0
    except Exception:  # noqa: BLE001
        return 0


def coverage_inventory() -> Dict[str, Any]:
    """``{"watched": [...], "unwatched": [{surface, reason, detected}, ...]}``.

    Cheap enough to call at every registration. Never raises: a failure here
    reports nothing, which the server reads as "this runtime has not answered",
    not as a clean bill of health.
    """
    try:
        from boosthis.runtime_flags import is_boosthis_disabled

        if is_boosthis_disabled():
            # A switched-off kit has neither reached anything nor found
            # anything. Saying otherwise would be a claim about an app we
            # stopped looking at.
            return {"watched": [], "unwatched": []}
    except Exception:  # noqa: BLE001
        pass

    watched: List[str] = []
    unwatched: List[Dict[str, Any]] = []

    def gap(surface: str, reason: str, detected: int) -> None:
        if detected > 0:
            unwatched.append(
                {"surface": surface, "reason": reason, "detected": detected}
            )

    # -- Request handling ----------------------------------------------------
    # One ``mount()`` covers six frameworks with no view decorators, so the
    # question is never "did they decorate it" — it is whether a unit of work
    # has actually finished through the boundary.
    try:
        from boosthis import runtime_vitals

        if _count(runtime_vitals.requests_observed) > 0:
            watched.append("request-handling")
    except Exception:  # noqa: BLE001
        pass

    # -- Outbound calls ------------------------------------------------------
    try:
        from boosthis import live_detectors

        if _count(live_detectors.outbound_attempts_observed) > 0:
            watched.append("outbound-calls")
    except Exception:  # noqa: BLE001
        pass
    try:
        from boosthis.outbound_clients import unwatched_client_count

        gap("outbound-calls", "no-adapter-yet", _count(unwatched_client_count))
    except Exception:  # noqa: BLE001
        pass

    # -- Database work -------------------------------------------------------
    # There is no adapter here at all, so this is reported as a gap the moment
    # a database library is genuinely loaded — and stays silent in an app that
    # never touched one.
    loaded_db = 0
    try:
        for name in _DB_MODULES:
            if name in sys.modules:
                loaded_db += 1
    except Exception:  # noqa: BLE001
        loaded_db = 0
    gap("database-work", "no-adapter-yet", loaded_db)

    # -- Background jobs and schedules --------------------------------------
    try:
        from boosthis import job_work

        if (
            _count(job_work.attached_job_system_count) > 0
            or _count(lambda: job_work.get_job_work_stats().get("runs", 0)) > 0
        ):
            watched.append("background-jobs")
        gap(
            "background-jobs",
            "no-adapter-yet",
            _count(job_work.unattached_job_system_count),
        )
    except Exception:  # noqa: BLE001
        pass

    # -- Response caching ----------------------------------------------------
    # Not a gap the app can close: a Streamlit rerun or a Gradio interaction
    # never produces an HTTP reply this kit could read headers off. Said out
    # loud, on the host that really has that limit, rather than left blank.
    try:
        from boosthis import host_surface

        limits = host_surface.UNMEASURABLE.get(
            getattr(host_surface.active(), "framework", "") or "", {}
        )
        if any(axis in limits for axis in _RESPONSE_AXES):
            gap("response-caching", "host-cannot-expose", 1)
    except Exception:  # noqa: BLE001
        pass

    watched.sort()
    unwatched.sort(key=lambda g: (g["surface"], g["reason"]))
    return {"watched": watched, "unwatched": unwatched}


def coverage_fingerprint(inventory: Dict[str, Any]) -> str:
    """A short, comparable string for one inventory.

    Registration happens at start-up, and most of this app's surfaces do not
    exist yet at start-up: a job library imported lazily is not in
    ``sys.modules`` until something needs it, and no unit of work has finished.
    An inventory sent once would therefore be systematically incomplete and
    would read as "nothing unwatched here" for the life of the process. This is
    what lets the kit re-register when — and only when — the answer changed.
    """
    try:
        watched = ",".join(inventory.get("watched") or [])
        gaps = ",".join(
            "%s:%s:%s" % (g.get("surface"), g.get("reason"), g.get("detected"))
            for g in (inventory.get("unwatched") or [])
        )
        return "%s|%s" % (watched, gaps)
    except Exception:  # noqa: BLE001
        return ""
