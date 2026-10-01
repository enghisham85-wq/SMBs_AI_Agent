"""Measure ONE unit of the host app's work, whatever the host calls a unit.

``BoosthisTraceMiddleware`` does this for ASGI apps, where a unit of work is
obviously an HTTP request. Django, Streamlit and Gradio each need the same
boundary meters, but they reach them from somewhere that is not an ASGI scope:

  * Django  — a request/response pair, taken around the handler.
  * Streamlit — a script rerun (or a fragment rerun), taken around the run.
  * Gradio  — one event-handler invocation, taken around the call.

So the boundary work lives here once, and every adapter opens a :class:`Unit`,
optionally reports an HTTP response into it, and closes it. Closing is what
makes the measurement real: it files the timing sample the dashboard draws, the
load-deflection point, the repeated-work fold, and the in-flight decrement.

Every step is individually guarded. A unit that is opened is ALWAYS closable,
and closing runs every step it can even if an earlier one blew up — the kit may
never break, slow, or half-leave state in the host app.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional, Sequence, Tuple

from . import extra_meters
from . import repeated_work
from . import db_work
from . import dependency_work
from . import tracker
from .event_loop_lag import sample_event_loop_lag
from .live_detectors import (
    _auto_arm_outbound_http_hook,
    current_in_flight,
    note_request_end,
    note_request_start,
)
from .runtime_vitals import note_request
from .runtime_flags import is_boosthis_disabled
from .held_open import (
    is_held_open_response,
    note_held_open_excluded,
)
from .trace import (
    current_request_parent_span_id,
    current_request_span_id,
    current_trace_elapsed_base,
    current_trace_id,
    current_trace_request_start,
    new_trace_id,
    sanitize_trace_id,
)
from .span_scope import new_span_id, sanitize_span_id


class Unit:
    """One in-flight unit of the host's work.

    Created by :func:`begin`, closed exactly once by :func:`end`. ``inert`` is
    True when the kill-switch is on, in which case every method is a no-op and
    nothing was ever incremented."""

    __slots__ = (
        "label",
        "inert",
        "started_at",
        "in_flight_at_start",
        "work_handle",
        "dependency_handle",
        "db_handle",
        "_trace_token",
        "_base_token",
        "_start_token",
        "_span_token",
        "_parent_span_token",
        "_closed",
        "status",
        "held_open",
    )

    def __init__(self, label: str) -> None:
        self.label = label
        self.inert = True
        self.started_at = 0.0
        self.in_flight_at_start = 0
        self.work_handle: Any = None
        self.dependency_handle: Any = None
        self.db_handle: Any = None
        self._trace_token: Any = None
        self._base_token: Any = None
        self._start_token: Any = None
        self._span_token: Any = None
        self._parent_span_token: Any = None
        self._closed = False
        self.status: Optional[int] = None
        self.held_open = False

    @property
    def elapsed_ms(self) -> float:
        if not self.started_at:
            return 0.0
        return (time.monotonic() - self.started_at) * 1000.0


def begin(
    label: str,
    *,
    trace_id: Optional[str] = None,
    elapsed_base: int = 0,
    parent_span_id: Optional[str] = None,
    sample_loop_lag: bool = False,
) -> Unit:
    """Open the measurement boundary for one unit of work.

    Mirrors the ASGI middleware's opening moves: in-flight increment (paired
    unconditionally in :func:`end`), start mark, deflection reading, repeated-
    work scope, outbound-HTTP arming, and the trace contextvars.

    ``sample_loop_lag`` is opt-in because there is only an event loop to probe
    on hosts that actually have one; a WSGI Django request or a Streamlit script
    thread has none, and probing there would invent a reading."""
    unit = Unit(label)
    if is_boosthis_disabled():
        return unit
    unit.inert = False
    # Paired with note_request_end() in end(); once counted, the decrement runs
    # on every exit path so the shared counter can never leak upward.
    try:
        note_request_start()
    except Exception:  # noqa: BLE001
        pass
    unit.started_at = time.monotonic()
    try:
        unit.in_flight_at_start = current_in_flight()
    except Exception:  # noqa: BLE001
        unit.in_flight_at_start = 1
    try:
        unit.work_handle = repeated_work.begin_request_work()
    except Exception:  # noqa: BLE001
        unit.work_handle = None
    try:
        unit.dependency_handle = dependency_work.begin_dependency_work()
    except Exception:  # noqa: BLE001
        unit.dependency_handle = None
    try:
        unit.db_handle = db_work.begin_db_work()
    except Exception:  # noqa: BLE001
        unit.db_handle = None
    try:
        _auto_arm_outbound_http_hook()
    except Exception:  # noqa: BLE001
        pass
    # Armed HERE, at the boundary, for the same reason the outbound hook is:
    # importing the kit must cost nothing until the app actually serves
    # traffic, and a driver the app imports lazily on its first request is
    # still picked up. Throttled inside, so this is a cheap no-op afterwards.
    try:
        db_work.arm_db_clients()
    except Exception:  # noqa: BLE001
        pass
    if sample_loop_lag:
        try:
            sample_event_loop_lag()
        except Exception:  # noqa: BLE001
            pass
    try:
        tid = sanitize_trace_id(trace_id) if trace_id else new_trace_id()
    except Exception:  # noqa: BLE001
        tid = None
    try:
        if tid:
            unit._trace_token = current_trace_id.set(tid)
            unit._base_token = current_trace_elapsed_base.set(int(elapsed_base or 0))
            unit._start_token = current_trace_request_start.set(unit.started_at)
            unit._span_token = current_request_span_id.set(new_span_id())
            unit._parent_span_token = current_request_parent_span_id.set(
                sanitize_span_id(parent_span_id)
            )
    except Exception:  # noqa: BLE001
        pass
    return unit


def note_http_response(
    unit: Unit,
    *,
    status: Optional[int] = None,
    response_headers: Optional[Sequence[Tuple[bytes, bytes]]] = None,
    authorization_present: bool = False,
    client_addr: Optional[str] = None,
    response: Any = None,
) -> None:
    """Report the HTTP response this unit produced, for the hosts that have one.

    Same four boundary meters the ASGI middleware reads at
    ``http.response.start``: reliability, cookie exposure, access outcome and
    refusal honesty. Hosts without a response (a Streamlit rerun) simply never
    call this, and :mod:`boosthis.host_surface` names those readings as not
    measurable rather than leaving them blank."""
    if unit.inert:
        return
    code = status if isinstance(status, int) else None
    unit.status = code
    try:
        unit.held_open = is_held_open_response(
            response, status=code, headers=response_headers
        )
    except Exception:  # noqa: BLE001
        unit.held_open = False
    try:
        note_request(code)
    except Exception:  # noqa: BLE001
        pass
    try:
        from .request_error_timer_meters import note_response
        note_response(unit.label, code, unit.dependency_handle)
    except Exception:  # noqa: BLE001
        pass
    try:
        extra_meters.note_cookie_headers(response_headers)
    except Exception:  # noqa: BLE001
        pass
    try:
        extra_meters.note_access_outcome(code, client_addr)
    except Exception:  # noqa: BLE001
        pass
    try:
        challenge_present = any(
            isinstance(pair, (list, tuple))
            and len(pair) >= 2
            and bytes(pair[0]).lower() == b"www-authenticate"
            and bool(pair[1])
            for pair in (response_headers or [])
        )
        extra_meters.note_refusal_honesty(
            code, bool(authorization_present), challenge_present
        )
    except Exception:  # noqa: BLE001
        pass


def end(unit: Unit, *, record_sample: bool = True) -> float:
    """Close the boundary and file everything this unit measured.

    Returns the measured duration in ms (0.0 for an inert unit). Idempotent:
    a second call is a no-op, so an adapter may close in a ``finally`` without
    worrying about a double close."""
    if unit.inert or unit._closed:
        return 0.0
    unit._closed = True
    duration_ms = unit.elapsed_ms
    if unit.held_open:
        try:
            note_held_open_excluded(duration_ms)
        except Exception:  # noqa: BLE001
            pass
    # The timing sample: this is the reading a developer actually looks at, and
    # the reason a panel is not a decoration. Labels are code-defined by the
    # adapters and re-checked by tracker._emit's route-label PII guard.
    if record_sample and not unit.held_open:
        try:
            # A unit of work IS the host's handler for this request/rerun/
            # interaction — that is what the boundary exists to bracket — so
            # the span can say so. The outcome comes from the status the
            # adapter recorded on the unit: a 4xx is the app answering, a 5xx
            # is the work failing. An adapter that never set a status (a
            # rerun, an interaction, a framework whose response the adapter
            # cannot see) says nothing rather than "ok".
            from boosthis.span_work import outcome_for_status

            tracker._emit(
                unit.label,
                int(round(duration_ms)),
                kind="handler",
                outcome=(
                    outcome_for_status(unit.status)
                    if isinstance(unit.status, int)
                    else None
                ),
            )
        except Exception:  # noqa: BLE001
            pass
    if not unit.held_open:
        try:
            extra_meters.note_deflection_sample(unit.in_flight_at_start, duration_ms)
        except Exception:  # noqa: BLE001
            pass
    for token, var in (
        (unit._trace_token, current_trace_id),
        (unit._base_token, current_trace_elapsed_base),
        (unit._start_token, current_trace_request_start),
        (unit._span_token, current_request_span_id),
        (unit._parent_span_token, current_request_parent_span_id),
    ):
        try:
            if token is not None:
                var.reset(token)
        except Exception:  # noqa: BLE001
            pass
    try:
        repeated_work.end_request_work(unit.work_handle, duration_ms)
    except Exception:  # noqa: BLE001
        pass
    try:
        dependency_work.end_dependency_work(unit.dependency_handle, duration_ms)
    except Exception:  # noqa: BLE001
        pass
    # STRICTLY after the dependency close: that reading borrows this request's
    # database ms off the still-open tally so every outside-service share
    # divides by one denominator. Closing here first would leave it borrowing
    # a zero and silently withhold the share it exists to publish.
    try:
        db_work.end_db_work(unit.db_handle, duration_ms)
    except Exception:  # noqa: BLE001
        pass
    try:
        note_request_end()
    except Exception:  # noqa: BLE001
        pass
    return duration_ms


@contextmanager
def measure(
    label: str,
    *,
    trace_id: Optional[str] = None,
    elapsed_base: int = 0,
    sample_loop_lag: bool = False,
) -> Iterator[Unit]:
    """``with measure("streamlit.rerun"): ...`` — begin/end with a guaranteed
    close, including when the host's own code raises."""
    unit = begin(
        label,
        trace_id=trace_id,
        elapsed_base=elapsed_base,
        sample_loop_lag=sample_loop_lag,
    )
    try:
        yield unit
    finally:
        end(unit)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def safe_label(candidate: Any, fallback: str) -> str:
    """Turn host-supplied text into a label the sample buffer will accept.

    The inventory and timing paths deliberately use the same normaliser. The
    ``fallback`` argument remains for source compatibility, but an unnameable
    part is refused rather than filed under a name the kit invented."""
    try:
        from boosthis import route_inventory

        label = route_inventory.normalize_part_name(candidate)
    except Exception:  # noqa: BLE001
        return ""
    return label or ""
