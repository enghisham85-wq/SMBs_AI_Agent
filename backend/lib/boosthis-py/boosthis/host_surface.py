"""Which kind of Python app the kit is mounted inside — and what that host can
and cannot tell us.

Two jobs live here on purpose, because they are the same question asked twice.

1.  **Both halves, or neither.** Every framework adapter installs *two* things:
    the kit's own pages (dashboard, panel, status, the small JSON endpoints) and
    the per-request measuring that makes those pages say anything. An adapter
    that quietly installs the first without the second is the worst failure this
    kit has: the developer sees a live panel, the panel says "warming up"
    forever, and nothing ever complains. Two of our own adapters shipped in
    exactly that shape once. So every adapter opens a :class:`Wiring` record,
    reports each half into it, and calls :func:`finish`. A half-wired mount
    prints ONE honest line naming the missing half and its cause — it never
    raises, because the kit may never break the host app. ``tests/
    test_adapter_wiring_parity.py`` walks every adapter and fails the build if
    one of them can reach a mounted panel without measuring.

2.  **What cannot be measured here.** A Streamlit script rerun has no HTTP
    response headers; a Django app served over WSGI has no asyncio event loop in
    its request path. Those readings are not "warming up" and never will be, and
    a blank tile is a lie by omission. :func:`unmeasurable_axes` names them, with
    the reason, so the snapshot reports them as *not measurable* instead.

    The catalogue below covers EVERY framework this kit mounts into, and every
    axis it can emit, because the two questions it answers are asked of all of
    them: "can this host produce this reading?" and, when it cannot, "why not?".
    It is not a hand-written list of excuses — it is derived from three facts a
    host either has or has not (:data:`HOST_LIMITS`) crossed with what each
    reading is built from (:data:`AXIS_NEEDS`), so a new axis cannot be added
    without deciding, once, whether every framework can take it.
    ``tests/test_unmeasurable_naming.py`` fails the build if one is.

Which framework is mounted also goes on the wire, as the snapshot's
``platform`` word (:func:`snapshot_platform`) — otherwise the dashboard would
know only "Python" and would have to draw a blank where this module has a
reason to give.

Nothing in this module ever raises; it is read on the mount path and on the
snapshot path, and both must survive a broken host.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 1. Both halves, or neither
# ---------------------------------------------------------------------------

#: The kit's own pages: dashboard, panel, status page, the small JSON reads.
PANEL = "panel"
#: The per-unit-of-work measuring: request/rerun/interaction boundary meters.
MEASUREMENT = "measurement"


@dataclass
class Wiring:
    """What one ``mount()`` call actually managed to install."""

    framework: str
    #: Unit of work this host measures, in the developer's own vocabulary.
    unit: str = "request"
    panel: bool = False
    panel_reason: Optional[str] = None
    measurement: bool = False
    measurement_reason: Optional[str] = None
    #: True while both halves are still waiting on a host event that has not
    #: happened yet (Gradio installs both at ``launch()``). A deferred mount is
    #: not half-wired — it is not wired at all yet — so it says nothing.
    deferred: bool = False

    def complete(self) -> bool:
        """True when both halves went in."""
        return bool(self.panel and self.measurement)

    def half_wired(self) -> bool:
        """True when exactly one half went in — the shape this module exists to
        catch."""
        if self.deferred:
            return False
        return bool(self.panel) != bool(self.measurement)

    def missing(self) -> List[str]:
        out: List[str] = []
        if not self.panel:
            out.append(PANEL)
        if not self.measurement:
            out.append(MEASUREMENT)
        return out


_lock = threading.Lock()
_active: Optional[Wiring] = None
_mounts: List[Wiring] = []


def begin(framework: str, unit: str = "request") -> Wiring:
    """Open a wiring record for one mount. Both halves start as NOT installed —
    an adapter has to earn each of them."""
    return Wiring(framework=str(framework or "unknown"), unit=str(unit or "request"))


def note_panel(w: Wiring, ok: bool, reason: Optional[str] = None) -> None:
    """Record whether the kit's own pages went in."""
    try:
        w.panel = bool(ok)
        w.panel_reason = None if ok else (reason or "reason not reported")
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        pass


def note_measurement(w: Wiring, ok: bool, reason: Optional[str] = None) -> None:
    """Record whether the per-unit-of-work measuring went in."""
    try:
        w.measurement = bool(ok)
        w.measurement_reason = None if ok else (reason or "reason not reported")
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        pass


def half_wired_line(w: Wiring) -> Optional[str]:
    """The one sentence a half-wired mount is entitled to, or ``None`` when the
    mount is whole (or wholly failed — that path announces itself elsewhere)."""
    if not w.half_wired():
        return None
    if w.panel and not w.measurement:
        why = w.measurement_reason or "reason not reported"
        return (
            "[boosthis] mounted the Boosthis page on %s but could NOT install "
            "per-%s measuring, so no timings will appear: %s"
        ) % (w.framework, w.unit, why)
    why = w.panel_reason or "reason not reported"
    return (
        "[boosthis] installed per-%s measuring on %s but could NOT mount the "
        "Boosthis page, so there is nowhere to read it: %s"
    ) % (w.unit, w.framework, why)


def forget_foreign_reading(key: str) -> bool:
    """Forget a value recorded for ``key`` before this host took the process.

    A reading gathered under a DIFFERENT host — or before any host mounted at
    all — is not this host's reading. Left in place it outlives the mount, and a
    project shows a real-looking value for something its own host can never
    produce (the naming below only fills EMPTY tiles, on purpose: a reading that
    arrives under this host must always win over our claim it could not).

    Returns ``True`` when a drop path exists for the axis. Never raises: this
    runs on the mount path.

    The meter modules own their own state, so each is asked in turn; a named
    reading whose module has no reset would return ``False`` here, and the guard
    test fails the build rather than let the stale value win."""
    try:
        if str(key) == "eventLoopLag":
            from boosthis.event_loop_lag import clear_event_loop_lag

            clear_event_loop_lag()
            return True
        try:
            from boosthis.request_error_timer_meters import drop_axis_state
            if drop_axis_state(str(key)):
                return True
        except Exception:  # noqa: BLE001
            pass
        from boosthis import extra_meters, os_meters, runtime_vitals

        for module in (extra_meters, os_meters, runtime_vitals):
            try:
                if bool(module.drop_axis_state(str(key))):
                    return True
            except Exception:  # noqa: BLE001  # pragma: no cover - defensive
                continue
        return False
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return False


def _forget_what_another_host_left_behind(framework: str) -> None:
    """Drop stale values for every reading ``framework`` cannot produce."""
    for key in UNMEASURABLE.get(str(framework), {}):
        forget_foreign_reading(key)


def finish(w: Wiring) -> Wiring:
    """Register the mount as the active host surface and, if only one half went
    in, say so once on stderr.

    Ungated on purpose (same rule as the never-started notice): a developer
    whose panel is about to lie to them for months needs to read this before
    anything else, whether or not telemetry is on."""
    global _active
    took_over = False
    try:
        with _lock:
            # True when a DIFFERENT kind of host now owns the process (or the
            # first host does, after a host-less phase). Same host mounting
            # again is NOT a takeover — its own readings must survive.
            took_over = _active is None or _active.framework != w.framework
            _active = w
            # A deferred mount finishes twice (once when the adapter arms its
            # host hook, once when the hook fires); it is still ONE mount.
            if not any(existing is w for existing in _mounts):
                _mounts.append(w)
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        pass
    if took_over:
        # Before this host serves anything, forget what another host recorded
        # for the readings this one cannot produce, so the snapshot says the
        # same thing whatever ran in this process first.
        try:
            _forget_what_another_host_left_behind(w.framework)
        except Exception:  # noqa: BLE001  # pragma: no cover - defensive
            pass
    try:
        line = half_wired_line(w)
        if line:
            print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        pass
    return w


def active() -> Optional[Wiring]:
    """The most recent mount, or ``None`` when the kit was never mounted."""
    return _active


def all_mounts() -> List[Wiring]:
    """Every mount this process has performed, oldest first."""
    with _lock:
        return list(_mounts)


def clear() -> None:
    """Wipe the record (wired into ``forget()`` and used by tests)."""
    global _active
    with _lock:
        _active = None
        _mounts.clear()


# ---------------------------------------------------------------------------
# 2. What cannot be measured here
# ---------------------------------------------------------------------------

# -- The three facts a Python host either gives the kit, or does not ---------
#
# Deliberately short. Every reading this kit takes is either built on one of
# these host-supplied signals, or it reads the process/interpreter itself and
# therefore works under any framework. A capability nobody's axis needs is not
# a capability worth naming here.

#: The kit sees the HTTP response this unit of work produced — its status code
#: and its response headers (``unit_of_work.note_http_response`` and the ASGI /
#: WSGI response hooks that feed it).
HTTP_RESPONSE = "http-response"
#: The unit of work runs on an asyncio event loop the kit can probe.
EVENT_LOOP = "event-loop"
#: The app is served by something that boots and recycles worker processes
#: (gunicorn, uWSGI), so worker churn is a thing that can happen at all.
WORKER_SERVER = "worker-server"
CAPABILITIES = (HTTP_RESPONSE, EVENT_LOOP, WORKER_SERVER)

#: Host surface -> the capabilities it does NOT have, each with the clause that
#: opens every reason written for it.
#:
#: Every framework ``mount()`` dispatches to appears here, including the ones
#: that lack nothing — an empty entry is an answer ("this host can take every
#: reading"), a missing entry is a gap, and the guard test tells them apart.
#:
#: These are hard facts about the host, not "not yet".
HOST_LIMITS: Dict[str, Dict[str, str]] = {
    # ASGI, served by uvicorn/hypercorn: a real event loop under every request,
    # a real HTTP response, and a process manager that may recycle workers.
    "fastapi": {},
    "starlette": {},
    "django-asgi": {},
    # WSGI: a real HTTP response and real worker processes, no event loop.
    "flask": {
        EVENT_LOOP: "a Flask app is served over WSGI",
    },
    "django-wsgi": {
        EVENT_LOOP: "this Django app is served over WSGI",
    },
    # Streamlit owns the whole server: it writes the HTTP responses itself, runs
    # the script on a worker thread of its own, and is started as one process.
    "streamlit": {
        HTTP_RESPONSE: (
            "Streamlit writes its own HTTP responses and a script rerun never "
            "sees one"
        ),
        EVENT_LOOP: (
            "the Streamlit script runs on its own worker thread, not on the "
            "server's event loop"
        ),
        WORKER_SERVER: (
            "Streamlit serves the app from the single process it starts itself"
        ),
    },
    # Gradio runs on FastAPI, so it reads responses and the loop exactly as the
    # FastAPI adapter does — but launch() starts its own single-process server,
    # so there is no worker manager behind it.
    "gradio": {
        WORKER_SERVER: (
            "Gradio's launch() serves the demo from the single process it "
            "starts itself"
        ),
    },
}

#: Axis key -> the host capability its reading is built on, or ``None`` when it
#: reads the process, the interpreter or the OS and therefore works under every
#: framework.
#:
#: EXHAUSTIVE, on purpose: every axis this kit can emit is answered here, so
#: adding a meter forces the question "and where can this NOT be taken?" once,
#: in this table, instead of leaving a framework to draw a blank tile for it
#: months later. The guard test walks the panel's own axis catalogue and fails
#: on the first key that is missing.
AXIS_NEEDS: Dict[str, Optional[str]] = {
    # -- built on the HTTP response the host produced ------------------------
    "reliability": HTTP_RESPONSE,
    "cookieExposure": HTTP_RESPONSE,
    "accessPressure": HTTP_RESPONSE,
    "refusalHonesty": HTTP_RESPONSE,
    "failureContainment": HTTP_RESPONSE,
    # -- built on the asyncio event loop under the unit of work --------------
    "eventLoopLag": EVENT_LOOP,
    "taskBacklog": EVENT_LOOP,
    "blockingAsync": EVENT_LOOP,
    "asyncSlowCallbacks": EVENT_LOOP,
    # -- built on a server that recycles worker processes --------------------
    "workerRestarts": WORKER_SERVER,
    # Counted from the kit's OWN request samples rather than from the host's
    # response, which is why it is not gated on a host capability: a kit that
    # watches calls instead of answering them still has route outcomes of its
    # own to count (scripts/src/meter-coverage/contract.ts). A Python host
    # that finishes no unit of work with a status simply files no sample, and
    # an absent sample is the ordinary nothing-yet answer, not a refusal.
    "routeFailures": None,
    "cpuConsumption": None,
    "queueTime": None,
    "threadFootprint": None,
    "threadPoolSaturation": None,
    "workerImbalance": None,
    "worstFreeze": None,
    "backpressure": None,
    "connectionSetup": None,
    "exceptionChurn": None,
    "timeoutHeadroom": None,
    "timerHealth": None,
    "unhandledErrors": None,
    "upstreamCache": None,
    "uptimeStability": None,
    # -- the rest read the process, the interpreter or the OS ----------------
    # Timing and shape of the host's own unit of work: every adapter opens and
    # closes one (a request, a rerun, an interaction), so these are readable
    # wherever the kit is mounted at all.
    "responsiveness": None,
    "resilience": None,
    "budget": None,
    "confidence": None,
    "baseline": None,
    "latencyFloor": None,
    "idle": None,
    "network": None,
    "schedulerLatency": None,
    "loadDeflection": None,
    "memoryPerRequest": None,
    "repeatedWork": None,
    # Database work is read inside the app's own driver, not off the response
    # or the loop, so it is readable wherever the kit is mounted. An app with no
    # database simply reports nothing, which is not a host limit.
    "dbWork": None,
    "leakWatch": None,
    # Interpreter and process state: read straight from CPython or the OS.
    "memoryStability": None,
    "residentGrowth": None,
    "heapHeadroom": None,
    "gcPressure": None,
    "gcTax": None,
    "gcPauseTail": None,
    "gcGenerationBalance": None,
    "finalizerBacklog": None,
    "gcGenOccupancy": None,
    "containerPressure": None,
    "crashFree": None,
    "gilContention": None,
    "startupImport": None,
    "coldStart": None,
    "swallowedErrors": None,
    "devPosture": None,
    "threadPoolStarvation": None,
    "forkChurn": None,
    "allocChurn": None,
    "peakRss": None,
    "contextSwitching": None,
    "pageFaults": None,
    "ioPressure": None,
    "cpuEntitlement": None,
    "descriptorMix": None,
    "importChurn": None,
    "runtimeCapability": None,
    "patchLag": None,
}

#: Axis key -> what the missing capability means FOR THAT READING.
#:
#: Joined onto the host's clause above, so one framework fact is written once
#: and every reading built on it says the same true thing about it: "a Flask app
#: is served over WSGI, so there are no asyncio tasks to queue up".
AXIS_CLAUSE: Dict[str, str] = {
    "reliability": "so a finished unit of work carries no status code to count as an error",
    "cookieExposure": "so there is no Set-Cookie header to inspect",
    "accessPressure": "so sign-in failures cannot be counted from it",
    "refusalHonesty": "so there is no status code to be honest or dishonest about",
    "failureContainment": "so a failed dependency cannot be matched to its response outcome",
    "eventLoopLag": "so there is no loop whose lateness could be timed",
    "taskBacklog": "so there are no asyncio tasks to queue up",
    "blockingAsync": "so nothing here can block a loop it never runs on",
    "asyncSlowCallbacks": "so asyncio never schedules a callback here to run late",
    "workerRestarts": "so there are no worker processes to be recycled",
}


def _reason(host_clause: str, axis_clause: str) -> str:
    """One host fact, one consequence for one reading."""
    return "%s, %s" % (host_clause, axis_clause)


#: Per host surface: axis key -> the reason it can never be read there.
#:
#: DERIVED from the two tables above, never hand-written: an entry exists
#: exactly when the reading needs something this host does not have. An entry
#: here turns a blank tile into a stated *not measurable* with its reason
#: attached; leaving the axis out entirely would render as "warming up" forever.
UNMEASURABLE: Dict[str, Dict[str, str]] = {
    framework: {
        axis: _reason(limits[need], AXIS_CLAUSE[axis])
        for axis, need in AXIS_NEEDS.items()
        if need is not None and need in limits
    }
    for framework, limits in HOST_LIMITS.items()
}

#: Host surface -> the word the snapshot puts on the wire for it.
#:
#: A closed vocabulary, mirrored by the server's own arrival check: the
#: dashboard cannot name a Flask app's impossible readings while all it is told
#: is "python". Any surface not listed here — and a process with no mount at
#: all — travels as the plain runtime word, which claims nothing.
UNMOUNTED_PLATFORM = "python"
PLATFORM_TAGS: Dict[str, str] = {
    "fastapi": "python-fastapi",
    "starlette": "python-starlette",
    "flask": "python-flask",
    "django-wsgi": "python-django-wsgi",
    "django-asgi": "python-django-asgi",
    "streamlit": "python-streamlit",
    "gradio": "python-gradio",
}


def snapshot_platform() -> str:
    """The ``platform`` word for the CURRENT host surface.

    ``"python"`` when nothing is mounted (a plain worker or a CLI has no host to
    name) or when the mounted framework has no tag. Never raises: this runs on
    the snapshot path."""
    try:
        w = _active
        if w is None:
            return UNMOUNTED_PLATFORM
        return PLATFORM_TAGS.get(w.framework, UNMOUNTED_PLATFORM)
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return UNMOUNTED_PLATFORM


def not_measurable(reason: str) -> Dict[str, Any]:
    """The wire shape for a reading that cannot exist on this host."""
    return {
        "score": None,
        "rating": "pending",
        "measurable": 0,
        "reasonCode": 8,
        "caption": "not measurable — %s" % reason,
        "context": {"reason": reason},
    }


def unmeasurable_axes() -> Dict[str, Dict[str, Any]]:
    """Axis key -> not-measurable payload for the CURRENT host surface.

    Empty when the kit was never mounted (a plain worker or CLI makes no claim
    about what a web host could have told us) or when the host can read
    everything. Never raises."""
    try:
        w = _active
        if w is None:
            return {}
        table = UNMEASURABLE.get(w.framework)
        if not table:
            return {}
        return {key: not_measurable(reason) for key, reason in table.items()}
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return {}
