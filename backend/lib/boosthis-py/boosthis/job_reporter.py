"""Scheduled jobs — what the app tells Boosthis about its own background work.

WHY THIS EXISTS. Everything else the kit measures happens while somebody is
waiting: a request, a call out to another service. The work that runs when
nobody is looking — a nightly billing run, a Celery beat entry, a queue drain,
a reindex — was invisible. An app had no way to say "that ran", so Boosthis had
no way to notice that it stopped. A scheduled job that quietly dies is one of
the commonest ways a working app stops doing its job.

WHAT IS REPORTED, AND NOTHING ELSE. Four facts per run: the job's own name, how
long ago it finished, how long it took, and whether it succeeded. There is no
field for arguments, payload, input, output or error detail — not here, not on
the wire, and nowhere on the server to put them. A job's contents are the
customer's business.

TIME IS RELATIVE ON PURPOSE. A run reports ``finishedAgoMs``, an age, never a
wall-clock time. A machine whose clock is a month out can therefore never file a
run under the wrong day or make a stale job look fresh. The age is computed at
flush from a monotonic clock, so a run that waits in the buffer does not drift.

THE KIT IS A GUEST. :func:`report_job_run` and :func:`track_job` never raise,
never wait on the network, and never change what the host's own code does.
``track_job`` re-raises the host's exception untouched after recording the
failure: the app's error handling is the app's, and a reporting failure must
never surface as an application failure.

UPLOAD MODEL: identical to :mod:`boosthis.span_emitter`. The Python runtime has
no always-running event loop and Boosthis's own Python rule book forbids
idle-spinning background pollers, so flushing is **work-gated** — reporting a
run buffers it and, throttled to at most once per :data:`JOB_FLUSH_MS`, spawns a
ONE-SHOT daemon thread to ship a batch.

ONE EXCEPTION, and it is the whole point of a declared rhythm: work-gating is
the wrong clock for a DECLARATION. The job most worth watching is the one that
has stopped, and the process that owns it reports no runs — so a declaration
that waited for a run would leave exactly that job unwatched, and an owner who
later changed the interval, lengthened the grace or switched the watch off from
the dashboard could never reach a process already running. So a declaration
starts a small upkeep clock (see :func:`start_expectation_upkeep`): a daemon
thread that exists only while some rhythm is declared, sleeps until the next
thing is actually due rather than waking on a fixed tick, and ends itself when
the last declaration is forgotten. An app that never declares a rhythm gets no
thread at all.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import threading
import time
from typing import Any, Callable, TypeVar

from boosthis.promise_declaration import (
    clear_promise_declarations,
    has_pending_promise_declarations,
    note_promise_declaration_outcomes,
    restore_promise_declarations,
    take_promise_declarations_for_send,
)
from boosthis.pii import check_job_name
from boosthis.runtime_flags import is_boosthis_disabled

#: Server accepts at most 50 runs per batch.
MAX_JOB_RUN_BATCH = 50
#: Hard cap on the in-memory buffer (drop-oldest on overflow) so an app firing
#: jobs faster than the flush throttle can never grow memory without bound.
MAX_BUFFERED_JOB_RUNS = 200
#: ``job`` name server cap.
MAX_JOB_NAME = 80
#: Work-gated flush throttle window. Slower than spans: a background job is not
#: urgent, and the point is to notice one MISSING over hours, not to be live.
JOB_FLUSH_MS = 30_000
#: Duration + age hard clamps (match the server's 0..604800000 bounds).
MAX_JOB_MS = 604_800_000


def job_name(name: str) -> str:
    """Take a job name at the server's bound, or refuse it.

    REFUSED, never shortened. This used to truncate, on the reading that a name
    is a fixed label so cutting it still names the same job. Two things are
    wrong with that. Two different jobs whose names agree for their first
    ``MAX_JOB_NAME`` characters are filed as one, silently. And -- the worse
    one -- the VALUE screen runs AFTER this, so it would be reading a string
    the value had already been cut out of: a name carrying a token past the
    bound passed the screen and travelled. A screen can only answer for the
    string it was shown, so the whole name has to be short enough to keep.

    Returns ``""`` for a name that cannot be used, which every caller already
    treats as nothing to report, and the refusal is counted and said once so it
    is never a silent drop.
    """
    if len(name) <= MAX_JOB_NAME:
        return name
    note_job_name_refused(name)
    say_job_name_refused_once(
        "length",
        f"[boosthis] Boosthis is not measuring a background job whose name "
        f"is {len(name)} characters: a job name must be {MAX_JOB_NAME} or "
        f"fewer. It is refused rather than shortened, because two names cut "
        f"to the same {MAX_JOB_NAME} characters would be filed as one job, "
        f"and a shortened name would be screened with its tail already gone. "
        f"The name is not printed here — an over-long name is the likeliest "
        f"of all to have been built out of a value, and your logs are "
        f"somewhere that value would travel to.",
    )
    return ""


#: The most declared rhythms one upload may carry — the server's own cap, and
#: the same 20 job names this kit will track.
MAX_JOB_EXPECTATIONS = 20
#: The shortest and longest rhythm Boosthis can STORE, in this kit's own unit.
#:
#: ``expect_every()`` takes milliseconds; the server keeps a declared rhythm in
#: whole MINUTES, from one minute to 45 days (``MIN_EVERY_MINUTES`` and
#: ``MAX_EVERY_MINUTES`` in the api-server's ``jobRunLimits.ts``, held equal to
#: these two by ``job-rhythm-declaration-parity.test.ts``). The mismatch in
#: units is the whole reason these live here: a developer who states thirty
#: seconds is watched at one minute, and the only honest place to say so is the
#: call that stated it — the answer carrying the real number back arrives a
#: flush later, into a handler nobody is reading.
#:
#: Neither bound narrows what may be SENT. An unstorable rhythm still travels
#: and still comes back as a named refusal, so the runs in the same upload are
#: never lost to one bad argument.
MIN_STORABLE_EVERY_MS = 60_000
MAX_STORABLE_EVERY_MS = 45 * 24 * 60 * 60 * 1000
#: How many flushes may pass with a declaration unanswered before the kit says
#: so out loud. One failure is a network blip, not a fault worth a warning.
UNDELIVERED_ROUNDS_BEFORE_WARNING = 2

#: How long an answer about a declaration is trusted before the kit asks again.
#:
#: The declaration is not the only thing that can change: the OWNER can correct
#: the rhythm, lengthen the grace, or switch the watch off from the dashboard,
#: and a kit that asked once at boot would go on judging against the answer it
#: got then while the server judges against the owner's — this task's own fault,
#: moved later in time. Re-stating an unchanged value writes nothing on the
#: server; it is the only thing that carries an owner's change back to a process
#: that is already running.
EXPECTATION_REVALIDATE_MS = 5 * 60_000

#: ``(runs, expectations) -> accepted count`` or ``{"accepted", "expectations"}``.
JobRunSubmitter = Callable[..., Any]
#: ``outcome -> None`` — where the server's answer about one declaration goes.
JobExpectationHandler = Callable[[dict[str, Any]], None]

_submitter: JobRunSubmitter | None = None
#: Each entry is ``{"job", "finished_at", "durationMs", "ok"}`` where
#: ``finished_at`` is a monotonic reading, turned into an age only at flush.
_buffer: list[dict[str, Any]] = []
_last_flush_at_ms: float = 0.0
_in_flight = False
_state_lock = threading.Lock()

# ── The rhythm the app's own code declared ──────────────────────────────────
#
# ``expect_every("reindex", 30_000)`` used to write its number into this
# process's own memory and stop. Boosthis — the only side that raises an alert,
# and the only side still watching when this process is the thing that stopped
# — was never told, so it truthfully answered "nobody declared a rhythm" for a
# job this kit was simultaneously counting missed runs for. Two answers, one
# job, both ours.
#
# So a declaration is now a thing to be DELIVERED, with the same three states a
# buffered run has: waiting to go, gone but unanswered, and acknowledged. It
# rides the runs' own upload, and the kit only judges a job against a rhythm
# Boosthis has said it is keeping.

#: Declared and not yet sent — ``{job: every_ms}``.
_pending_expectations: dict[str, float] = {}
#: Sent, awaiting this upload's answer. Restored to pending if none comes.
_in_flight_expectations: dict[str, float] = {}
#: What Boosthis last confirmed it holds, and WHEN it said so —
#: ``{job: (every_ms, answered_at_ms)}``. The timestamp is what stops a
#: restart's re-statement being re-sent on every flush without freezing the
#: answer forever: see :data:`EXPECTATION_REVALIDATE_MS`.
_acknowledged_expectations: dict[str, tuple[float, float]] = {}
#: Acknowledged declarations being re-stated on the current send, with the
#: acknowledgement they came from, so a failed round puts it back.
_revalidating_expectations: dict[str, tuple[float, float]] = {}
_on_expectation_outcome: JobExpectationHandler | None = None
_undelivered_rounds = 0
_warned_expectation_causes: set[str] = set()

#: Floor on one upkeep sleep. Guards against a clock that says "due now"
#: forever — a wait of zero would be a spin, which is the thing the work-gated
#: model exists to avoid.
_UPKEEP_MIN_WAIT_MS = 1_000
#: Its own lock: the loop calls :func:`_flush_sync`, which takes
#: ``_state_lock``, so the two must never be held together in either order.
_upkeep_lock = threading.Lock()
_upkeep_thread: threading.Thread | None = None
_upkeep_stop = threading.Event()


def set_job_run_submitter(fn: JobRunSubmitter | None) -> None:
    """Register (or clear) the function that ships buffered runs.

    :mod:`boosthis.telemetry` sets this for any registered app — background-job
    runs ride the same always-on doctrine as crash reporting, NOT the snapshot
    share gate, because a developer must be told a nightly job stopped whether
    or not they ever connected an AI. Clearing it makes :func:`report_job_run`
    inert immediately.
    """
    global _submitter
    with _state_lock:
        _submitter = fn


def has_submitter() -> bool:
    """True when a submitter is registered."""
    with _state_lock:
        return _submitter is not None


def clear_buffered_job_runs() -> None:
    """Drop the in-memory run buffer (called by ``forget()``)."""
    with _state_lock:
        _buffer.clear()
    clear_job_name_refusals()


def declare_job_rhythm(job: str, every_ms: float) -> None:
    """Queue "this job should run every ``every_ms``" for delivery.

    Called by :func:`boosthis.job_work.expect_every` after it has validated the
    name and the interval. Queuing rather than sending is deliberate:
    ``expect_every`` is normally called while modules import, routinely before
    the kit has finished starting, and dropping it there would be this same
    fault in a narrower window.

    Re-stating a rhythm Boosthis has already acknowledged costs nothing — the
    value is compared, and only a CHANGE is queued.
    """
    queued = False
    try:
        if not isinstance(job, str) or not job:
            return
        value = float(every_ms)
        if value <= 0:
            return
        with _state_lock:
            known = _acknowledged_expectations.get(job)
            if known is not None and known[0] == value:
                return
            if (
                job not in _pending_expectations
                and len(_pending_expectations) >= MAX_JOB_EXPECTATIONS
            ):
                # Not silently: a 21st distinct declared job means the app is
                # naming jobs from values, and the ones past the cap are simply
                # not watched.
                _warn_expectation_once(
                    "too_many",
                    "[boosthis] This app declared a rhythm for more than "
                    f"{MAX_JOB_EXPECTATIONS} jobs. Only the first "
                    f"{MAX_JOB_EXPECTATIONS} are sent to Boosthis; the rest are "
                    "not being watched. Job names must be fixed labels your "
                    "code chose, never built from a value.",
                )
                return
            _pending_expectations[job] = value
            queued = True
    except Exception:  # noqa: BLE001
        pass
    finally:
        # Outside the lock: the upkeep clock's own loop takes ``_state_lock``.
        # A declaration is the only thing that starts it, and from here it is
        # started whether or not this process ever reports a run.
        if queued:
            start_expectation_upkeep()


def pending_job_expectation_count() -> int:
    """Declared rhythms Boosthis has NOT acknowledged. Every one of these is a
    job that is not being watched yet. Kept apart from the buffered-RUN count:
    a different unit, and a different question."""
    with _state_lock:
        return len(_pending_expectations) + len(_in_flight_expectations)


def set_job_expectation_handler(fn: JobExpectationHandler | None) -> None:
    """Register where the server's answer about a declaration goes.

    :mod:`boosthis.job_work` sets this once, at import: it is the module that
    must know which rhythm is actually IN FORCE, because it is the one counting
    missed runs. Wiring it this way rather than importing ``job_work`` here
    keeps the dependency one-directional.
    """
    global _on_expectation_outcome
    with _state_lock:
        _on_expectation_outcome = fn


def clear_job_rhythm_declarations() -> None:
    """Forget every declared rhythm, queued or acknowledged (``forget()``)."""
    with _state_lock:
        _pending_expectations.clear()
        _in_flight_expectations.clear()
        _revalidating_expectations.clear()
        _acknowledged_expectations.clear()
    # Nothing left to keep current, so the clock ends with it. Outside the lock
    # above: stopping waits on the loop, and the loop wants ``_state_lock``.
    stop_expectation_upkeep()


def _due_revalidations(now_ms: float) -> list[tuple[str, tuple[float, float]]]:
    """Acknowledged declarations whose answer is old enough to ask about again.

    Caller holds ``_state_lock``.
    """
    return [
        (job, ack)
        for job, ack in _acknowledged_expectations.items()
        if now_ms - ack[1] >= EXPECTATION_REVALIDATE_MS
    ]


def _upkeep_wait_ms(now_ms: float) -> float | None:
    """How long until the next declaration work is due, or ``None`` when there
    is nothing left to keep current and the clock should end.

    An undelivered declaration is retried at the ordinary flush pace; once
    everything is acknowledged the only thing left is re-stating the oldest
    answer, so the wait is exactly that long. Caller must NOT hold
    ``_state_lock``.
    """
    with _state_lock:
        if _pending_expectations or _in_flight_expectations:
            return float(JOB_FLUSH_MS)
        if not _acknowledged_expectations:
            return None
        oldest = min(at for _, at in _acknowledged_expectations.values())
        return max(0.0, oldest + EXPECTATION_REVALIDATE_MS - now_ms)


def _upkeep_pass() -> None:
    """One turn of the upkeep clock: ship a declaration that never arrived,
    re-state an answer that has gone stale. Reached with NO job run involved —
    that is the entire reason this exists. Never raises."""
    try:
        if is_boosthis_disabled():
            return
        _flush_sync()
    except Exception:  # noqa: BLE001
        pass


def _upkeep_loop(stop: threading.Event) -> None:
    """The upkeep thread's body. Ends itself when nothing is declared.

    Takes its OWN stop flag rather than reading the module's: a stop whose join
    timed out (the loop was inside a slow upload) must not be revived by the
    next start clearing a shared flag, which would leave two clocks running.
    """
    global _upkeep_thread
    try:
        while not stop.is_set():
            wait_ms = _upkeep_wait_ms(time.monotonic() * 1000)
            if wait_ms is None:
                break
            if stop.wait(max(wait_ms, _UPKEEP_MIN_WAIT_MS) / 1000):
                break
            _upkeep_pass()
    except Exception:  # noqa: BLE001
        pass
    finally:
        with _upkeep_lock:
            if _upkeep_thread is threading.current_thread():
                _upkeep_thread = None


def start_expectation_upkeep() -> None:
    """Keep declared rhythms current without waiting for a job run. Idempotent.

    A daemon thread, so it can never hold the host's process open, and one that
    only exists while a rhythm is declared: :func:`_upkeep_wait_ms` returns
    ``None`` when the last declaration is forgotten and the loop ends. Skipped
    under the kill switch, and under pytest — where the tests drive
    :func:`_upkeep_pass` directly, and the one test that wants a real thread
    clears the marker itself.
    """
    global _upkeep_thread, _upkeep_stop
    try:
        if is_boosthis_disabled() or os.environ.get("PYTEST_CURRENT_TEST"):
            return
        with _upkeep_lock:
            if _upkeep_thread is not None and _upkeep_thread.is_alive():
                return
            stop = threading.Event()
            _upkeep_stop = stop
            thread = threading.Thread(
                target=_upkeep_loop,
                args=(stop,),
                name="boosthis-job-rhythm",
                daemon=True,
            )
            _upkeep_thread = thread
        thread.start()
    except Exception:  # noqa: BLE001
        # A host that refuses another thread keeps its declaration queued; the
        # work-gated path still carries it if the app reports a run.
        pass


def stop_expectation_upkeep() -> None:
    """End the upkeep clock. Safe to call when it was never started."""
    global _upkeep_thread
    _upkeep_stop.set()
    with _upkeep_lock:
        thread = _upkeep_thread
        _upkeep_thread = None
    if thread is not None and thread is not threading.current_thread():
        # Bounded: the loop may be inside an upload the host's transport is
        # still waiting on, and a shutdown must not wait on a network.
        thread.join(timeout=2.0)


def _upload_gate_reason() -> tuple[str, str] | None:
    """Why an upload could not leave, when the reason is this kit's OWN gate
    rather than the network. ``None`` when nothing local is stopping it — only
    then is "this app cannot reach Boosthis" honest advice.

    A locked, unpaid, revoked or never-activated project drops every upload
    inside the transmit gate without a request ever being made, so the plain
    undelivered wording sends the developer to inspect a network that is working
    perfectly. The kinds are ``kill_switch``'s own closed set; the env
    kill-switch cannot appear here because the flush returns before any of this
    when it is on.
    """
    try:
        from .kill_switch import get_entitlement_gate_kind

        kind = get_entitlement_gate_kind()
    except Exception:  # noqa: BLE001 — a gate we cannot read is not a verdict
        return None
    sentences = {
        "unregistered": "This app has not completed its first check-in with "
        "Boosthis, so nothing leaves it yet; the declaration is kept and sent "
        "as soon as it registers.",
        "unpaid": "This project's plan is not active, so Boosthis is measuring "
        "and watching nothing; the declaration is kept and re-sent the moment "
        "the plan is active again.",
        "revoked": "This project's access was revoked, so Boosthis is watching "
        "nothing; the declaration is kept in case access is restored.",
        "paused": "This project is paused, so Boosthis is watching nothing; the "
        "declaration is kept and re-sent when it resumes.",
        "hidden": "Boosthis is not active in this app right now, so nothing "
        "leaves it; the declaration is kept and re-sent if it becomes active.",
    }
    sentence = sentences.get(str(kind))
    return (str(kind), sentence) if sentence else None


def _warn_expectation_once(cause: str, message: str) -> None:
    """One ungated line per cause, never repeated.

    Ungated on purpose, exactly like the dropped-row notice: a declaration that
    went nowhere looks identical to one that worked — same silent process, same
    absent alerts — and the developer who most needs to hear this is the one who
    does not suspect the kit. A debug flag only helps the one who already does.
    """
    if cause in _warned_expectation_causes:
        return
    _warned_expectation_causes.add(cause)
    try:
        logging.getLogger("boosthis").warning("%s", message)
    except Exception:  # noqa: BLE001
        pass


def say_job_name_refused_once(cause: str, message: str) -> None:
    """Say, once per cause per process, that a job's NAME was refused.

    This is the other silent drop, and the worse one.  A declaration at least
    had a developer calling ``expect_every`` and looking for an effect; a
    refused RUN name was dropped before upload, so the job was not late, not
    never-reported, and not on the page -- it simply never existed, and
    nothing anywhere said so.
    """
    _warn_expectation_once(f"jobName:{cause}", message)


#: How many runs this process refused to report because of their NAME, and how
#: many distinct names those were.  A count the kit can be asked for, so "what
#: does this screen actually block?" has an answer that is not a guess.  Both
#: halves: a bare count of runs would hide one name refused a thousand times
#: behind a thousand jobs refused once.
_job_name_refused_runs = 0
_job_name_refused_names: set[int] = set()
#: Bounded for the same reason every other name set here is: a name built from
#: a value is exactly the input that would grow this without limit.
MAX_REFUSED_NAMES_TRACKED = 50


def _refused_name_fingerprint(name: str) -> int:
    """A refused name reduced to a number, for counting distinct ones.

    The set exists only to answer "how many different names", and the strings
    it would otherwise hold are by definition the ones a VALUE rule matched --
    an email address, a token, an id. Keeping them in process memory for the
    life of the process is a copy of that value nobody asked for, and one
    later log line away from being a disclosure. A hash counts just as well
    and cannot be read back.
    """
    h = 5381
    for ch in name:
        h = ((h * 33) ^ ord(ch)) & 0xFFFFFFFF
    return h


def note_job_name_refused(name: str) -> None:
    """Record one run refused for its name.  Stores a fingerprint, never the
    name: see :func:`_refused_name_fingerprint`."""
    global _job_name_refused_runs
    _job_name_refused_runs += 1
    if len(_job_name_refused_names) < MAX_REFUSED_NAMES_TRACKED:
        _job_name_refused_names.add(_refused_name_fingerprint(name))


def job_name_refusals() -> dict[str, int]:
    """What this process has refused by name -- runs, and distinct names."""
    return {
        "runs": _job_name_refused_runs,
        "names": len(_job_name_refused_names),
    }


def clear_job_name_refusals() -> None:
    """Wipe the refusal tally.  Wired into the same reset the buffers use."""
    global _job_name_refused_runs
    _job_name_refused_runs = 0
    _job_name_refused_names.clear()


def say_declaration_once(cause: str, message: str) -> None:
    """Say, once per cause per process, that a declaration did nothing.

    The refusals below are the ones Boosthis considered and turned down. This
    is the other half: the causes the SERVER never hears about, because the kit
    stopped the declaration before it could travel — a switched-off kit, a name
    the label guard would not take, an interval that is not a number, a job
    past the name cap — plus the one case where the number silently CHANGES on
    the way, a rhythm finer than the minute the server stores.

    Every one of those was a silent ``return`` inside :func:`expect_every`,
    which is the same fault this whole path exists to remove, one step earlier.
    Shares the one-line-per-cause ledger with the refusals, so a loop that
    states the same bad rhythm prints one line, and never raises: the call it
    serves runs in the host's own import path.
    """
    _warn_expectation_once(f"declared:{cause}", message)


def say_rhythm_refused_once(message: str) -> None:
    """Say, once, that a rhythm is outside the range Boosthis can store.

    The ONE cause both halves can see: the kit knows it at the call, from its
    own copy of the bounds, and the server names it ``rhythm`` in the reply a
    flush later. One cause, one ledger key, so a developer never reads the
    same sentence twice and wonders whether two different things went wrong.
    Whichever half gets there first is the line they read.
    """
    _warn_expectation_once("refused:rhythm", message)


def _refusal_message(job: str, reason: Any) -> str:
    """The developer's words for a refusal, naming the thing they can change."""
    if reason == "name":
        return (
            f"[boosthis] Boosthis will not watch the job \"{job}\": its name did "
            "not pass the guard every reported label goes through. Name a job "
            "with a fixed label your code chose, never one built from a value."
        )
    if reason == "rhythm":
        return (
            f"[boosthis] Boosthis will not watch the job \"{job}\": the rhythm "
            "expect_every() was given is outside the storable range (1 minute "
            "to 45 days). That job is NOT being watched."
        )
    if reason == "too_many":
        return (
            f"[boosthis] Boosthis will not watch the job \"{job}\": this project "
            "is already watching the most jobs it may. That job is NOT being "
            "watched."
        )
    if reason == "not_watched":
        return (
            f"[boosthis] Watching was switched off for the job \"{job}\" on its "
            "Boosthis project page, so the rhythm this code declares is not in "
            "force. Only that page can switch it back on."
        )
    return (
        f"[boosthis] Boosthis did not keep the rhythm declared for the job "
        f"\"{job}\", and gave no reason. That job is NOT being watched."
    )


def parse_expectation_outcomes(body: Any) -> list[dict[str, Any]] | None:
    """Read the per-declaration answers out of an upload reply.

    ``None`` means the reply said NOTHING about declarations — an older server,
    or a reply that never arrived. That is not the same as an empty list, which
    is a server saying "I considered them and kept none", and the two must stay
    distinguishable or an undelivered declaration hides again.
    """
    try:
        if not isinstance(body, dict):
            return None
        raw = body.get("expectations")
        if not isinstance(raw, list):
            return None
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            job = item.get("job")
            if not isinstance(job, str) or not job:
                continue
            out.append(item)
        return out
    except Exception:  # noqa: BLE001
        return None


def note_job_expectation_outcomes(outcomes: list[dict[str, Any]]) -> None:
    """Take the server's answer for each declaration: what is in force, what
    was refused and why. A refusal is spoken once and not retried — asking
    again would not change the answer."""
    global _undelivered_rounds
    try:
        handler: JobExpectationHandler | None
        with _state_lock:
            handler = _on_expectation_outcome
            for outcome in outcomes:
                job = outcome.get("job")
                if not isinstance(job, str):
                    continue
                sent = _in_flight_expectations.pop(job, None)
                _revalidating_expectations.pop(job, None)
                answered_at = time.monotonic() * 1000
                if outcome.get("stored") is True:
                    if sent is not None:
                        _acknowledged_expectations[job] = (sent, answered_at)
                    _pending_expectations.pop(job, None)
                else:
                    # Answered, and the answer is no. Keeping it pending would
                    # ask the same question on every flush forever — but it IS
                    # asked again at the ordinary revalidation pace, because a
                    # refusal can be lifted and an owner who switches a watch
                    # back on has no other way to tell a running process.
                    if sent is not None:
                        _acknowledged_expectations[job] = (sent, answered_at)
                    _pending_expectations.pop(job, None)
            if outcomes:
                _undelivered_rounds = 0
        for outcome in outcomes:
            job = outcome.get("job")
            if isinstance(job, str) and outcome.get("stored") is not True:
                _warn_expectation_once(
                    f"refused:{outcome.get('reason')}",
                    _refusal_message(job, outcome.get("reason")),
                )
            if handler is not None:
                try:
                    handler(outcome)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass


def _submitter_arity(fn: Any) -> int:
    """How many positional arguments a registered submitter will take.

    A submitter is a function a HOST may have registered, and this kit has
    gained arguments after that contract existed — first the declared rhythms,
    then the promises declared in code. Calling a one-argument function with
    three would raise, and the declarations would vanish into the same silence
    this whole change exists to end. So an older submitter still gets the runs,
    the declarations stay queued, and the kit warns that they are not being
    kept.

    A ``*args`` submitter takes whatever it is given, so it is read as current.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        # Cannot tell: assume current, and let the call decide.
        return _SUBMITTER_ARGS
    positional = 0
    for param in sig.parameters.values():
        if param.kind is param.VAR_POSITIONAL:
            return _SUBMITTER_ARGS
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            positional += 1
    return positional


#: runs, expectations, promises.
_SUBMITTER_ARGS = 3


def _accepts_expectations(fn: Any) -> bool:
    """Whether a registered submitter can be handed declared rhythms."""
    return _submitter_arity(fn) >= 2


def _accepts_promises(fn: Any) -> bool:
    """Whether a registered submitter can be handed code-declared promises."""
    return _submitter_arity(fn) >= 3


def _clamp_ms(value: Any) -> int:
    try:
        return max(0, min(MAX_JOB_MS, int(round(float(value)))))
    except Exception:  # noqa: BLE001
        return 0


def report_job_run(
    job: str,
    duration_ms: float,
    ok: bool,
    finished_ago_ms: float = 0.0,
) -> None:
    """Record that a named background job finished a run.

    Best-effort and immediate: it buffers and returns. Nothing is awaited and no
    network call happens on the caller's thread. Every failure path is
    swallowed — reporting a run must never be a reason the job itself fails.

    ``finished_ago_ms`` is for a run that finished before it is being reported
    (a job whose own bookkeeping is written after the fact); it defaults to 0,
    meaning "just now".
    """
    try:
        if is_boosthis_disabled():
            return
        name = job_name(job.strip()) if isinstance(job, str) else ""
        if not name:
            return
        # The VALUE screen, at the boundary the docstring above always said it
        # was at and where it was in fact missing: ``report_job_run`` can be
        # called directly, without ever passing ``_clean_name``, so a name
        # built out of a value reached the wire from here. Counted and said,
        # once, rather than dropped in silence.
        refused = check_job_name(name)
        if refused is not None:
            note_job_name_refused(name)
            say_job_name_refused_once(
                refused,
                f"[boosthis] Boosthis is not measuring a background job whose "
                f"name matched {refused}: its runs are dropped before upload "
                f"and the job will not appear at all. The name is "
                f"deliberately not printed — the rule that fired is a rule "
                f"about a VALUE, so echoing the name would copy that email "
                f"address, token or id into your logs to explain that it must "
                f"not travel. It was {len(name)} characters. Name jobs in "
                "code, the way nightly-billing is written — never out of a "
                "value such as an id, an email address or a customer "
                "reference.",
            )
            return
        with _state_lock:
            if _submitter is None:
                return
            _buffer.append(
                {
                    "job": name,
                    # Monotonic, so a clock change while the run sits in the
                    # buffer cannot alter the age we eventually report.
                    "finished_at": time.monotonic() * 1000 - _clamp_ms(finished_ago_ms),
                    "durationMs": _clamp_ms(duration_ms),
                    "ok": bool(ok),
                }
            )
            while len(_buffer) > MAX_BUFFERED_JOB_RUNS:
                _buffer.pop(0)
    except Exception:  # noqa: BLE001
        return
    maybe_flush_job_runs()


F = TypeVar("F", bound=Callable[..., Any])


def _report_finished(name: str, started: float, ok: bool) -> None:
    report_job_run(name, (time.monotonic() - started) * 1000, ok)


async def _await_then_report(name: str, started: float, awaitable: Any) -> Any:
    """Await the host's own awaitable, then record what really happened."""
    try:
        out = await awaitable
    except BaseException:
        _report_finished(name, started, False)
        # The host's exception, untouched. Boosthis is a witness.
        raise
    _report_finished(name, started, True)
    return out


def _watch_future(name: str, started: float, out: Any) -> bool:
    """Record when a returned Future/Task settles. True when it was watched.

    A Task or Future is handed back to the host EXACTLY as it came: wrapping it
    would change its type, and an app that cancels or inspects its own task must
    keep working. The measurement rides a completion callback instead, which can
    never raise into the host's event loop.
    """
    add = getattr(out, "add_done_callback", None)
    if not callable(add):
        return False

    def settled(fut: Any) -> None:
        ok = False
        try:
            ok = not fut.cancelled() and fut.exception() is None
        except BaseException:  # noqa: BLE001 - a cancelled future raises here
            ok = False
        _report_finished(name, started, ok)

    try:
        add(settled)
    except Exception:  # noqa: BLE001
        return False
    return True


def track_job(name: str) -> Callable[[F], F]:
    """Decorator that times a scheduled job and reports the run.

    NOT THE PUBLIC ``track_job``. ``boosthis.track_job`` is ``job_work``'s, and
    that is the one every guide names. This one shares its name, which is how
    the kit came to hold two different job-name rules at once: this module
    published ``MAX_JOB_NAME`` (80) and the other enforced a private 60, so
    which rule a name met depended on which module it reached. Both now bound a
    name by ``MAX_JOB_NAME``, by construction rather than by agreement --
    ``job_work.MAX_JOB_NAME_LEN`` IS the constant above. See
    docs/decisions/job-name-length.md.

    What is left between them is a capability difference, not a rule
    difference: this decorator measures an ``async def`` across its await and
    the public one, used as a decorator, times the call that builds the
    coroutine. That is its own defect and is filed separately.

    The one-line way to wire this up::

        @track_job("nightly-billing")
        def run_billing() -> None:
            ...

    The host's return value passes through untouched and the host's exception is
    re-raised untouched: this decorator adds a measurement and changes nothing
    else about how the job behaves. A job that raises is reported as a run that
    did not succeed — which is exactly what makes "it ran but it is failing"
    different from "it stopped running".

    ASYNC JOBS ARE MEASURED WHERE THE WORK IS. Half the Python schedulers people
    actually run background work on are async, and calling an ``async def`` only
    builds a coroutine — the work happens later, at the await. Timing the call
    would file an instant success for a job that has not started, which is worse
    than not measuring it: a job that later fails, or hangs for an hour, would
    still read "on time". So a coroutine function is awaited inside the wrapper,
    a returned Task or Future is watched to completion (and handed back
    untouched), and any other awaitable is timed across its await.
    """

    def decorate(fn: F) -> F:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                started = time.monotonic()
                try:
                    out = await fn(*args, **kwargs)
                except BaseException:
                    _report_finished(name, started, False)
                    # The host's exception, untouched. Boosthis is a witness.
                    raise
                _report_finished(name, started, True)
                return out

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            try:
                out = fn(*args, **kwargs)
            except BaseException:
                _report_finished(name, started, False)
                # The host's exception, untouched. Boosthis is a witness.
                raise
            if _watch_future(name, started, out):
                return out
            if inspect.isawaitable(out):
                return _await_then_report(name, started, out)
            _report_finished(name, started, True)
            return out

        return wrapper  # type: ignore[return-value]

    return decorate


def _flush_sync() -> int:
    """Ship up to one batch inline (no thread). Never raises.

    A queued declaration is reason enough to make this call on its own: the job
    most worth watching is the one that has stopped, and waiting for a run to
    carry the declaration would leave exactly that job unwatched.
    """
    global _in_flight, _undelivered_rounds
    with _state_lock:
        if _submitter is None or _in_flight:
            return 0
        due = _due_revalidations(time.monotonic() * 1000)
        if (
            not _buffer
            and not _pending_expectations
            and not due
            # A promise declared by an app that reports no job runs at all
            # must still travel: this upload is the only door it has.
            and not has_pending_promise_declarations()
        ):
            return 0
        submitter = _submitter
        taken = _buffer[:MAX_JOB_RUN_BATCH]
        del _buffer[: len(taken)]
        carries = _accepts_expectations(submitter)
        carries_promises = _accepts_promises(submitter)
        declared = (
            list(_pending_expectations.items())[:MAX_JOB_EXPECTATIONS]
            if carries
            else []
        )
        for job, every in declared:
            _in_flight_expectations[job] = every
            _pending_expectations.pop(job, None)
        # Then the ones whose answer has gone stale. They ride the same array
        # and the same answer handling; the only difference is that a round
        # that fails restores the acknowledgement rather than calling a watched
        # job unwatched.
        if carries:
            for job, ack in due:
                if len(declared) >= MAX_JOB_EXPECTATIONS:
                    break
                if job in _in_flight_expectations:
                    continue
                declared.append((job, ack[0]))
                _in_flight_expectations[job] = ack[0]
                _revalidating_expectations[job] = ack
                _acknowledged_expectations.pop(job, None)
        # Promises declared in code ride the same upload. Their own module
        # owns the buffering, the cap and the guards; it hands back exactly
        # what goes on the wire.
        promises = take_promise_declarations_for_send() if carries_promises else []
        if not taken and not declared and not promises:
            _in_flight = False
            if _pending_expectations:
                # Nothing can carry them. Say so rather than spin.
                _warn_expectation_once(
                    "no_transport",
                    "[boosthis] This app declared how often a job should run, "
                    "but the installed kit cannot send declarations to "
                    "Boosthis. Those jobs are NOT being watched.",
                )
            if not carries_promises and has_pending_promise_declarations():
                _warn_expectation_once(
                    "no_transport_promises",
                    "[boosthis] This app declares a promise in its code, but "
                    "the installed kit cannot send promises to Boosthis. That "
                    "promise is NOT saved.",
                )
            return 0
        _in_flight = True
    # Did this batch REACH Boosthis? Not "did the submitter return" — a refused
    # upload returns too, and reading that as delivery is how a kit ends up
    # blaming an out-of-date server for a batch the server never saw.
    delivered = False
    try:
        now_ms = time.monotonic() * 1000
        batch = [
            {
                "job": r["job"],
                "finishedAgoMs": _clamp_ms(now_ms - r["finished_at"]),
                "durationMs": r["durationMs"],
                "ok": r["ok"],
            }
            for r in taken
        ]
        expectations = [
            {"job": job, "everyMs": int(round(every))} for job, every in declared
        ]
        if carries_promises:
            out = submitter(batch, expectations, promises)
        elif carries:
            out = submitter(batch, expectations)
        else:
            out = submitter(batch)
        if isinstance(out, dict):
            # A mapping answer is itself the proof the batch reached Boosthis:
            # the transport only builds one after an upload the server took.
            delivered = True
            outcomes = out.get("expectations")
            if isinstance(outcomes, list):
                note_job_expectation_outcomes(outcomes)
            promise_outcomes = out.get("promises")
            if isinstance(promise_outcomes, list):
                note_promise_declaration_outcomes(promise_outcomes)
            return int(out.get("accepted") or 0)
        # A transport that only ships runs cannot say whether the declarations
        # reached anything. Accepted rows are the one thing a bare count proves.
        accepted = int(out or 0)
        delivered = accepted > 0
        return accepted
    except Exception:  # noqa: BLE001
        return 0
    finally:
        with _state_lock:
            _in_flight = False
            # Anything the server never answered for goes back in the queue —
            # it did not arrive, or arrived at a server too old to say. Either
            # way the job is not being watched and we must ask again.
            # A re-statement of an ALREADY acknowledged rhythm goes back where
            # it came from, dated now so the next attempt is one interval away.
            # The job is still watched: reporting it as an undelivered promise
            # would be the opposite lie to the one this path exists to prevent.
            back_at = time.monotonic() * 1000
            for job, ack in _revalidating_expectations.items():
                _in_flight_expectations.pop(job, None)
                if job not in _pending_expectations:
                    _acknowledged_expectations[job] = (ack[0], back_at)
            _revalidating_expectations.clear()
            # Judged AFTER the restore above, so only a declaration that has
            # never been acknowledged can open an undelivered round.
            unanswered = bool(_in_flight_expectations)
            for job, every in _in_flight_expectations.items():
                _pending_expectations.setdefault(job, every)
            _in_flight_expectations.clear()
            if declared and unanswered:
                _undelivered_rounds += 1
                rounds = _undelivered_rounds
            else:
                rounds = 0
        if delivered and rounds == 1:
            # The batch arrived and the server said nothing about the rhythm
            # that rode with it — an older Boosthis, not an unreachable one.
            _warn_expectation_once(
                "unanswered",
                "[boosthis] Boosthis accepted this app's job runs but said "
                "nothing about the rhythm this app declared with "
                "expect_every() — those jobs are NOT being watched yet. This "
                "usually means the Boosthis server is older than this kit; "
                "updating it fixes it, and the declaration keeps being re-sent "
                "meanwhile.",
            )
        elif not delivered and rounds >= UNDELIVERED_ROUNDS_BEFORE_WARNING:
            gate = _upload_gate_reason()
            _warn_expectation_once(
                f"undelivered:{gate[0]}" if gate else "undelivered",
                "[boosthis] This app declared how often a job should run, but "
                "Boosthis has not confirmed it. Those jobs are NOT being "
                "watched — nothing will call them late. "
                + (
                    gate[1]
                    if gate
                    else "The kit will keep trying; if this app cannot reach "
                    "Boosthis, that is why."
                ),
            )
        # The promise half, told the same two stories apart: an upload that
        # arrived and said nothing is an old server, an upload that never
        # arrived is a delivery problem — and this kit's own gate, when it is
        # the thing in the way, is named rather than leaving a developer
        # inspecting a healthy network.
        restore_promise_declarations(
            delivered=delivered,
            sent_count=len(promises),
            rounds_before_warning=UNDELIVERED_ROUNDS_BEFORE_WARNING,
            gate=_upload_gate_reason(),
        )


def flush_job_runs_now() -> None:
    """Spawn a one-shot daemon thread to ship buffered runs immediately.

    Skips entirely under the kill-switch or in tests (where the submitter is
    invoked directly). No-op without a registered submitter or an empty buffer.
    """
    if is_boosthis_disabled() or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    with _state_lock:
        if _submitter is None or (
            not _buffer
            and not _pending_expectations
            and not _due_revalidations(time.monotonic() * 1000)
        ):
            return
    threading.Thread(target=_flush_sync, daemon=True).start()


def maybe_flush_job_runs() -> None:
    """Work-gated, throttled flush trigger called from :func:`report_job_run`."""
    global _last_flush_at_ms
    if is_boosthis_disabled() or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    now = time.time() * 1000
    with _state_lock:
        if _submitter is None or not _buffer:
            return
        if now - _last_flush_at_ms < JOB_FLUSH_MS:
            return
        _last_flush_at_ms = now
    try:
        threading.Thread(target=_flush_sync, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def _reset_for_tests() -> None:
    """Clear module state between hermetic test runs."""
    global _submitter, _last_flush_at_ms, _in_flight, _undelivered_rounds
    # Before the lock: a test that started a real upkeep thread must not leave
    # it running into the next one, and stopping it waits on a loop that wants
    # ``_state_lock``.
    stop_expectation_upkeep()
    with _state_lock:
        _submitter = None
        _buffer.clear()
        _last_flush_at_ms = 0.0
        _in_flight = False
        _pending_expectations.clear()
        _in_flight_expectations.clear()
        _revalidating_expectations.clear()
        _acknowledged_expectations.clear()
        _undelivered_rounds = 0
        _warned_expectation_causes.clear()
    # Its own lock, taken outside ``_state_lock`` so the two are never held
    # together in either order.
    clear_promise_declarations()


def _buffer_len() -> int:
    """Test/introspection hook."""
    with _state_lock:
        return len(_buffer)


__all__ = [
    "MAX_JOB_RUN_BATCH",
    "MAX_BUFFERED_JOB_RUNS",
    "MAX_JOB_NAME",
    "MAX_JOB_EXPECTATIONS",
    "MIN_STORABLE_EVERY_MS",
    "MAX_STORABLE_EVERY_MS",
    "say_declaration_once",
    "say_job_name_refused_once",
    "note_job_name_refused",
    "job_name_refusals",
    "clear_job_name_refusals",
    "JOB_FLUSH_MS",
    "MAX_JOB_MS",
    "job_name",
    "set_job_run_submitter",
    "has_submitter",
    "clear_buffered_job_runs",
    "declare_job_rhythm",
    "pending_job_expectation_count",
    "set_job_expectation_handler",
    "clear_job_rhythm_declarations",
    "parse_expectation_outcomes",
    "note_job_expectation_outcomes",
    "report_job_run",
    "track_job",
    "flush_job_runs_now",
    "maybe_flush_job_runs",
    "start_expectation_upkeep",
    "stop_expectation_upkeep",
]
