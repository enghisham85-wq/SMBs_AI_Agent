"""Background work: queue, scheduled, and manually marked job runs.

Counts, timings, and guarded code-defined names are the whole data model. Job
arguments and payloads are deliberately never accepted, inspected, or retained.
"""

from __future__ import annotations

import asyncio
import functools
import math
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Generator

from boosthis import job_reporter, samples
from boosthis.pii import check_job_name
from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.score import get_duration_rating

JOB_PREFIX = "job:"
MAX_JOB_NAMES = 20
JOB_OTHER_NAME = "other"
#: Longest job name kept -- THE SAME NUMBER THE REPORTER PUBLISHES, by
#: construction rather than by coincidence. This was its own literal, 60, while
#: ``job_reporter.MAX_JOB_NAME`` (80) was the exported constant, the wire bound
#: and the server's rule. A name of 61-80 characters was therefore inside the
#: published limit and refused by the very call the docs point at: the job ran
#: and never existed here. See docs/decisions/job-name-length.md.
MAX_JOB_NAME_LEN = job_reporter.MAX_JOB_NAME
MAX_FOLDED_NAMES = 500
DURATION_RING = 500
GAP_RING = 12
CADENCE_MIN_GAPS = 4
CADENCE_MAX_SPREAD = 0.25
MISSED_GAP_FACTOR = 1.5
MAX_ACTIVE_TRACKED = 512


@dataclass
class _Tally:
    runs: int = 0
    failed: int = 0
    retried: int = 0
    retry_worst: int = 1
    worst_ms: float = 0.0
    active: int = 0
    overlaps: int = 0
    last_start_ms: float | None = None
    gaps: list[float] = field(default_factory=list)
    #: What ``expect_every()`` asked for. Recorded so the kit knows a
    #: declaration is outstanding — NEVER judged against: an interval Boosthis
    #: never received cannot raise an alert, and counting missed runs against
    #: it locally is how one job came to carry two disagreeing answers.
    requested_every_ms: float | None = None
    #: The rhythm IN FORCE — set only from Boosthis's own answer, so this side
    #: and the alerting side judge the same job by the same number.
    stated_every_ms: float | None = None
    #: The WHOLE allowance Boosthis said it gives this job before calling it
    #: late (rhythm + grace, in ms), read straight back out of the same answer.
    #: Applying ``MISSED_GAP_FACTOR`` to a declared rhythm instead is a second
    #: verdict, not a rounding difference: minutes are the stored unit and the
    #: grace has a floor, so a one-minute declaration is late over there after
    #: two minutes and would be "missed" here after ninety seconds.
    stated_allowance_ms: float | None = None
    missed: int = 0


_lock = threading.RLock()
_tallies: dict[str, _Tally] = {}
_folded_names: set[str] = set()
_durations: list[float] = []
_waits: list[float] = []
_wait_runs = 0
_hosted_runs = 0
_manual_runs = 0
_attached_systems: set[str] = set()
_unattached_systems: set[str] = set()


def _js_round(value: float) -> int:
    """JavaScript Math.round for the non-negative readings used here."""
    return int(math.floor(value + 0.5))


def _clean_name(name: Any) -> str | None:
    """A job name is a label written by hand beside a schedule.

    Spaces are fine -- "queue drain" and "Send Weekly Report" are names, not
    interpolated route labels.  That was the bug: ``check_route_label``
    blocks every space, so an ordinary job simply never existed on any
    surface.  See ``check_job_name`` and docs/job-name-screening.md.
    """
    if not isinstance(name, str):
        return None
    clean = name.strip()
    # Refuse rather than truncate: truncation silently aliases two jobs.
    if not clean or len(clean) > MAX_JOB_NAME_LEN:
        return None
    if check_job_name(clean) is not None:
        return None
    return clean


def _name_refusal_cause(name: Any) -> str:
    """The ledger key for a name refusal -- the RULE, never the name."""
    if not isinstance(name, str):
        return "type"
    clean = name.strip()
    if not clean:
        return "empty"
    if len(clean) > MAX_JOB_NAME_LEN:
        return "length"
    return check_job_name(clean) or "value"


def _tally_for(name: str) -> tuple[str, _Tally]:
    existing = _tallies.get(name)
    if existing is not None:
        return name, existing
    if len(_tallies) >= MAX_JOB_NAMES:
        if len(_folded_names) < MAX_FOLDED_NAMES:
            _folded_names.add(name)
        other = _tallies.setdefault(JOB_OTHER_NAME, _Tally())
        return JOB_OTHER_NAME, other
    tally = _Tally()
    _tallies[name] = tally
    return name, tally


def _cadence_of(tally: _Tally) -> float | None:
    if tally.stated_every_ms is not None and tally.stated_every_ms > 0:
        return tally.stated_every_ms
    if len(tally.gaps) < CADENCE_MIN_GAPS:
        return None
    ordered = sorted(tally.gaps)
    mid = len(ordered) // 2
    median = (
        (ordered[mid - 1] + ordered[mid]) / 2
        if len(ordered) % 2 == 0
        else ordered[mid]
    )
    if median <= 0:
        return None
    if (ordered[-1] - ordered[0]) / median > CADENCE_MAX_SPREAD:
        return None
    return median


def _missed_after_ms(tally: _Tally) -> float | None:
    """How long this job may be silent before a run counts as never happening.

    For a DECLARED rhythm this is Boosthis's own allowance, read back out of
    its answer rather than recomputed here, so the same job cannot be late over
    there and steady here. For a LEARNED cadence the factor is all there is —
    nobody else knows about that rhythm, so there is no second answer to
    disagree with. A server that stored the rhythm without saying what it
    allows leaves the allowance unset, and the factor stands in.
    """
    cadence = _cadence_of(tally)
    if cadence is None:
        return None
    if tally.stated_every_ms is not None and tally.stated_every_ms > 0:
        if tally.stated_allowance_ms is not None and tally.stated_allowance_ms > 0:
            return tally.stated_allowance_ms
    return cadence * MISSED_GAP_FACTOR


def _note_start(tally: _Tally, now_ms: float) -> None:
    if tally.last_start_ms is not None:
        gap = now_ms - tally.last_start_ms
        if gap > 0:
            # Judge the gap against the cadence known BEFORE learning this gap.
            # Otherwise one outage can poison the learner and erase itself.
            cadence = _cadence_of(tally)
            allowance = _missed_after_ms(tally)
            if cadence is not None and allowance is not None and gap > allowance:
                skipped = _js_round(gap / cadence) - 1
                if skipped > 0:
                    tally.missed += skipped
            tally.gaps.append(gap)
            del tally.gaps[:-GAP_RING]
    tally.last_start_ms = now_ms


class JobRun:
    """Idempotent, exception-proof handle returned by :func:`begin_job`."""

    def __init__(self, close: Callable[[bool, Any], None] | None = None) -> None:
        self._close = close

    def done(self) -> None:
        try:
            if self._close is not None:
                self._close(False, None)
        except Exception:  # noqa: BLE001
            pass

    def failed(self, error: Any = None) -> None:
        try:
            if self._close is not None:
                self._close(True, error)
        except Exception:  # noqa: BLE001
            pass


_NOOP_RUN = JobRun()

# Whether the code running right now is already inside a measured run.
#
# A project can be in both situations at once: the kit attached to its queue
# library by itself AND the developer marked their handlers by hand — because
# they followed the instructions for a system the kit cannot attach to, or
# because the marks predate the upgrade that added the adapter. Counting both
# would silently double every number that project sees, which is worse than
# measuring nothing: an empty dashboard at least looks empty.
#
# So the OUTER measurement wins and anything nested inside it is handed
# straight through. A ContextVar, not a plain flag, because a worker runs many
# jobs at once: each asyncio task and each thread carries its own copy, so one
# job's guard can never silence another job running beside it.
_inside_measured_job: ContextVar[bool] = ContextVar(
    "boosthis_inside_measured_job", default=False
)


@contextmanager
def measured_job_scope() -> Generator[None, None, None]:
    """Mark everything run inside as already measured by an outer run.

    Used by the adapters and by the public wrappers. Never swallows: the
    host's own failure stays the caller's to handle.
    """
    token = _inside_measured_job.set(True)
    try:
        yield
    finally:
        try:
            _inside_measured_job.reset(token)
        except ValueError:
            # The scope was entered in a different context (a handler that
            # hopped threads). Falling back to a plain clear is still correct:
            # the outer run is over either way.
            _inside_measured_job.set(False)


def run_inside_measured_job(fn: Callable[[], Any]) -> Any:
    """Call the host's own job function with nested marks suppressed."""
    with measured_job_scope():
        return fn()


def begin_job(
    name: str,
    *,
    queued_at_ms: float | None = None,
    attempt: float | None = None,
    system: str | None = None,
) -> JobRun:
    """Begin one run. The returned ``done``/``failed`` methods never raise."""
    global _wait_runs, _manual_runs
    try:
        if is_boosthis_disabled():
            return _NOOP_RUN
        # Already being measured by whoever called us — see above.
        if _inside_measured_job.get() is True:
            return _NOOP_RUN
        clean = _clean_name(name)
        if clean is None:
            # Said, once per cause. This is the drop that made a job
            # invisible: measured nowhere, on no surface, with no counter and
            # no line — so the owner could not tell a job that never ran from
            # one whose name was turned away here.
            job_reporter.note_job_name_refused(
                name if isinstance(name, str) else str(name)
            )
            job_reporter.say_job_name_refused_once(
                _name_refusal_cause(name),
                f"[boosthis] Boosthis is not measuring "
                f"{_refused_job_name_shape(name)}: {_why_name_refused(name)} "
                "The name itself is not printed: it is exactly the string the "
                "rule says must not travel, and your logs are somewhere it "
                "would travel to. Its runs "
                "are not counted and it will not appear anywhere — it is not "
                'late, and it is not "never reported"; it does not exist.',
            )
            return _NOOP_RUN
        now_ms = time.time() * 1000
        started = time.perf_counter()
        with _lock:
            key, tally = _tally_for(clean)
            _note_start(tally, now_ms)
            if tally.active > 0:
                tally.overlaps += 1
            tracked = sum(t.active for t in _tallies.values()) < MAX_ACTIVE_TRACKED
            if tracked:
                tally.active += 1
            if isinstance(system, str) and system:
                _attached_systems.add(system)
            else:
                _manual_runs += 1
            try:
                attempt_n = max(1, math.floor(float(attempt))) if attempt is not None else 1
                if not math.isfinite(float(attempt_n)):
                    attempt_n = 1
            except (TypeError, ValueError, OverflowError):
                attempt_n = 1
            try:
                queued = float(queued_at_ms) if queued_at_ms is not None else None
                if queued is not None and math.isfinite(queued):
                    waited = now_ms - queued
                    if waited >= 0:
                        _waits.append(waited)
                        del _waits[:-DURATION_RING]
                        _wait_runs += 1
            except (TypeError, ValueError, OverflowError):
                pass

        closed = False

        def close(failed_run: bool, error: Any) -> None:
            nonlocal closed
            try:
                with _lock:
                    if closed:
                        return
                    closed = True
                    duration = (time.perf_counter() - started) * 1000
                    tally.runs += 1
                    if tracked and tally.active > 0:
                        tally.active -= 1
                    if attempt_n > 1:
                        tally.retried += 1
                        tally.retry_worst = max(tally.retry_worst, attempt_n)
                    tally.worst_ms = max(tally.worst_ms, duration)
                    if failed_run:
                        tally.failed += 1
                    _durations.append(duration)
                    del _durations[:-DURATION_RING]
                    duration_ms = _js_round(duration)
                # A fast crash is not a fast success, so failure overrides the
                # ordinary duration rating on the additive sample row.
                samples.record(
                    f"{JOB_PREFIX}{key}",
                    duration_ms,
                    "poor" if failed_run else get_duration_rating(duration_ms),
                )
                if failed_run and error is not None:
                    try:
                        from boosthis import crash_reporter

                        crash_reporter.capture(error, "job")
                    except Exception:  # noqa: BLE001
                        pass
                # The finished run is also filed through the run reporter, so a
                # job measured here lands in the project's per-day job history
                # and is judged against a stated rhythm exactly like a run an
                # app reports by hand. Name, outcome and duration only.
                try:
                    from boosthis import job_reporter

                    job_reporter.report_job_run(
                        job=key,
                        duration_ms=duration_ms,
                        ok=not failed_run,
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    from boosthis import snapshot_mirror

                    snapshot_mirror.maybe_flush_snapshot()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass

        return JobRun(close)
    except Exception:  # noqa: BLE001
        return _NOOP_RUN


@contextmanager
def _job_context(name: str, options: dict[str, Any]) -> Generator[None, None, None]:
    run = begin_job(name, **options)
    # The block is now inside a measured run, so a mark nested within it —
    # by hand or by an adapter that attaches later — is handed through
    # instead of counted a second time.
    with measured_job_scope():
        try:
            yield
        except BaseException as exc:
            run.failed(exc)
            raise
        else:
            run.done()


def track_job(
    name: str,
    fn: Callable[..., Any] | None = None,
    *,
    queued_at_ms: float | None = None,
    attempt: float | None = None,
    system: str | None = None,
) -> Any:
    """Measure a context block, or wrap and immediately call a zero-arg function.

    ``with track_job("nightly")`` is the Python-native form. Passing ``fn``
    mirrors Node's convenience wrapper and preserves its return/exception.

    Anything marked inside the block is handed through rather than counted
    again, so leaving old by-hand marks in place while the kit attaches to the
    queue itself records one run, not two. The one exception is a scheduler
    that reports through events rather than a call the kit wraps (APScheduler),
    where the job body runs on another thread with no context to inherit.
    """
    options = {"queued_at_ms": queued_at_ms, "attempt": attempt, "system": system}
    if fn is None:
        return _job_context(name, options)
    if asyncio.iscoroutinefunction(fn):
        async def run_async() -> Any:
            run = begin_job(name, **options)
            with measured_job_scope():
                try:
                    out = await fn()
                except BaseException as exc:
                    run.failed(exc)
                    raise
                run.done()
                return out
        return run_async()
    run = begin_job(name, **options)
    with measured_job_scope():
        try:
            out = fn()
        except BaseException as exc:
            run.failed(exc)
            raise
        run.done()
        return out


def expect_every(name: str, interval_ms: float) -> None:
    """Declare how often a job is meant to run, and SEND that to Boosthis.

    The interval is queued for delivery on the job-runs upload, and only the
    rhythm Boosthis answers that it is keeping is used to judge this job here.
    That is the point: Boosthis is the side that is still watching when this
    process is the thing that stopped, so a declaration that never left the
    process could never produce the warning it was called for — while a local
    missed-run count made it look as though it had.

    If the declaration cannot be delivered — or cannot be taken at all — the
    kit says so on the ``boosthis`` logger, once per cause, rather than
    accepting it in silence.

    THE UNIT. This takes milliseconds; Boosthis stores a rhythm in whole
    MINUTES, from 1 minute to 45 days. A finer rhythm is watched at one minute
    and this call says so; a longer one is recorded as a declaration that could
    not be kept. ``docs/decisions/sub-minute-job-rhythms.md`` says why.

    THE OTHER DOOR. The same rhythm can be typed on the project's page in the
    Boosthis dashboard, by whoever holds the schedule but not the commit
    access. Between the two, the LAST deliberate statement is the one in
    force: restating the same rhythm at every restart writes nothing, so a
    correction typed on the page stands until the number on this line itself
    changes — and a watch the owner switched off is never turned back on from
    code, however often this call runs.
    ``docs/decisions/kit-declared-job-rhythm-precedence.md`` says why.
    """
    try:
        shown = _shown_job_name(name)
        if is_boosthis_disabled():
            job_reporter.say_declaration_once(
                "disabled",
                f"[boosthis] Boosthis is switched off in this process, so the "
                f"rhythm declared for {shown} was not sent and that job is NOT "
                "being watched. Nothing here is broken — unset BOOSTHIS_DISABLED "
                "and the declaration travels with the next upload.",
            )
            return
        clean = _clean_name(name)
        if clean is None:
            job_reporter.say_declaration_once(
                "name",
                f"[boosthis] Boosthis did not take the rhythm declared for "
                f"{shown}, so that job is NOT being watched: "
                f"{_why_name_refused(name)}",
            )
            return
        try:
            interval = float(interval_ms)
        except (TypeError, ValueError):
            interval = float("nan")
        if not math.isfinite(interval) or interval <= 0:
            job_reporter.say_declaration_once(
                "interval",
                f"[boosthis] Boosthis did not take the rhythm declared for "
                f"{shown}, so that job is NOT being watched: expect_every() "
                "wants how often the job runs in MILLISECONDS, above zero, and "
                f"was given {interval_ms!r}.",
            )
            return
        with _lock:
            key, tally = _tally_for(clean)
            folded = key != clean
            if not folded:
                tally.requested_every_ms = interval
        if folded:
            # Past the name cap this job's runs are folded into the
            # everything-else group, so it has no row of its own for a rhythm
            # to be judged against — and declaring one under the fold's name
            # would watch a group of unrelated jobs instead. Refused out loud
            # rather than retargeted in silence.
            job_reporter.say_declaration_once(
                "folded",
                f"[boosthis] Boosthis is already tracking {MAX_JOB_NAMES} job "
                f"names in this app, so {shown} is folded into "
                f'"{JOB_OTHER_NAME}" and is NOT being watched. Its runs are '
                "still counted, inside that group. Report fewer distinct job "
                "names — a name built from a value (an id, a tenant, a date) is "
                "the usual cause.",
            )
            return
        if interval < job_reporter.MIN_STORABLE_EVERY_MS:
            # Not refused — CHANGED. Minutes are the stored unit, so this is
            # the moment the developer's own number stops being the number in
            # force, and the answer carrying the real one back lands in a
            # handler nobody is reading.
            job_reporter.say_declaration_once(
                "sub_minute",
                f"[boosthis] Boosthis stores a job's rhythm in whole minutes, "
                f"so the {interval:g}ms declared for {shown} will be kept as "
                "every 1 minute — the shortest rhythm it can keep — plus the "
                "lateness Boosthis allows on top. Nothing is refused for "
                "being short: once Boosthis takes this declaration the job is "
                "watched against one minute rather than against the interval "
                "stated here, and if it cannot be taken the kit says that on "
                "a line of its own.",
            )
        elif interval > job_reporter.MAX_STORABLE_EVERY_MS:
            # The one cause BOTH halves can see, so it shares the server
            # refusal's ledger key rather than getting one of its own: the
            # developer hears it here, at the call, and the ``rhythm`` refusal
            # arriving a flush later does not say the same thing again.
            job_reporter.say_rhythm_refused_once(
                f"[boosthis] The rhythm declared for {shown} is longer than the "
                "45 days Boosthis can store, so it will be recorded as a "
                "declaration that could not be kept and that job will NOT be "
                "watched. Declare a rhythm between 1 minute and 45 days.",
            )
        job_reporter.declare_job_rhythm(clean, interval)
        job_reporter.flush_job_runs_now()
    except Exception:  # noqa: BLE001
        pass


def _shown_job_name(name: Any) -> str:
    """The job name as it may be echoed to this process's OWN log.

    A developer cannot fix a call the kit will not name. Bounded, and it goes
    nowhere else: a name the label guard refused is exactly the one that must
    not travel.
    """
    if not isinstance(name, str):
        return f"a job name of type {type(name).__name__}"
    clean = name.strip()
    if not clean:
        return "an empty job name"
    short = (
        clean[:MAX_JOB_NAME_LEN] + "\u2026"
        if len(clean) > MAX_JOB_NAME_LEN
        else clean
    )
    return f'"{short}"'


def _refused_job_name_shape(name: Any) -> str:
    """A REFUSED job name, described but never echoed.

    :func:`_shown_job_name` above is for a name that PASSED the screen, where
    echoing it is how a developer finds the call. This is the other case, and
    it cannot use it: the names this describes are precisely the ones a VALUE
    rule matched, so printing one copies an email address, a token or a
    customer id into the host's log in order to say that it must not travel.
    Production stderr is collected and shipped like any other destination.

    What is left is still enough to find the call: the RULE that fired (from
    :func:`_why_name_refused`) and the length, which is bounded and cannot be
    read back into the name.
    """
    if not isinstance(name, str):
        return f"a job name of type {type(name).__name__}"
    clean = name.strip()
    if not clean:
        return "an empty job name"
    return f"a background job whose name is {len(clean)} characters"


def _why_name_refused(name: Any) -> str:
    """Why :func:`_clean_name` turned a declaration away, in the developer's
    terms and naming the thing they can change. Never "invalid name"."""
    if not isinstance(name, str):
        return (
            "Boosthis wants the job's name as a string and was given "
            f"{type(name).__name__}."
        )
    clean = name.strip()
    if not clean:
        return "the name was empty."
    if len(clean) > MAX_JOB_NAME_LEN:
        return (
            f"the name is {len(clean)} characters and a job name here must be "
            f"{MAX_JOB_NAME_LEN} or fewer. It is refused rather than shortened, "
            f"because two names cut to the same {MAX_JOB_NAME_LEN} characters "
            "would be filed as one job."
        )
    # The RULE, named. "Did not pass the guard" told a developer nothing they
    # could act on, and it was also misleading: spaces are allowed in a job
    # name, so the rule that fired is always one about a VALUE.
    rule = check_job_name(clean) or "a value rule"
    return (
        f"the name matched {rule}, so it reads as something built out of a "
        'value rather than a label. Spaces are fine — "queue drain" is a '
        "name. An email address, a token, an IP address, a phone number, a "
        "UUID or a run of six or more digits is not: name the job the way "
        "nightly-billing is written."
    )


def _take_expectation_outcome(outcome: dict[str, Any]) -> None:
    """Boosthis's answer about one declared rhythm.

    Accepted: adopt the rhythm the SERVER stored, not the one that was asked
    for — it is stored in whole minutes, so 30 seconds comes back as 1 minute.
    Judging against the requested value would put this kit's missed count back
    out of step with the alerts, in a smaller way.

    Refused: drop any rhythm we were judging against. A cadence learned from
    observed runs may still take over, which is a different claim honestly made.
    """
    try:
        job = outcome.get("job")
        if not isinstance(job, str) or not job:
            return
        with _lock:
            tally = _tallies.get(job)
            if tally is None:
                return
            if outcome.get("stored") is True:
                minutes = outcome.get("everyMinutes")
                try:
                    every_ms = float(minutes) * 60_000 if minutes else 0.0
                except (TypeError, ValueError):
                    every_ms = 0.0
                tally.stated_every_ms = every_ms if every_ms > 0 else None
                # The allowance rides with the rhythm, because a missed run is
                # judged by it here. A server that answers without one leaves
                # this unset and ``_missed_after_ms`` says what happens then.
                grace = outcome.get("graceMinutes")
                try:
                    grace_ms = float(grace) * 60_000 if grace is not None else None
                except (TypeError, ValueError):
                    grace_ms = None
                tally.stated_allowance_ms = (
                    every_ms + grace_ms
                    if tally.stated_every_ms is not None
                    and grace_ms is not None
                    and grace_ms >= 0
                    else None
                )
            else:
                tally.stated_every_ms = None
                tally.stated_allowance_ms = None
    except Exception:  # noqa: BLE001
        pass


job_reporter.set_job_expectation_handler(_take_expectation_outcome)


def note_job_system_attached(system: str) -> None:
    try:
        if isinstance(system, str) and system:
            with _lock:
                _attached_systems.add(system)
                _unattached_systems.discard(system)
    except Exception:  # noqa: BLE001
        pass


def note_job_system_unattached(system: str) -> None:
    try:
        if isinstance(system, str) and system:
            with _lock:
                if system not in _attached_systems:
                    _unattached_systems.add(system)
    except Exception:  # noqa: BLE001
        pass


def unattached_job_system_count() -> int:
    with _lock:
        return len(_unattached_systems)


def attached_job_system_count() -> int:
    """Job systems this kit really got onto in this process.

    Read as evidence that background work IS being watched here — the other
    half of :func:`unattached_job_system_count`, which is what the coverage
    inventory reports as a blind spot."""
    with _lock:
        return len(_attached_systems)


def _percentile(values: list[float], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil((p / 100) * len(ordered)) - 1))
    return _js_round(ordered[idx])


def get_job_work_stats(now: float | None = None) -> dict[str, Any]:
    """Return the exact label-free aggregate consumed by the wire axis."""
    now_ms = time.time() * 1000 if now is None else float(now)
    with _lock:
        runs = failed = retried = overlaps = missed = recurring = 0
        retry_worst = 1
        worst_ms = 0.0
        for tally in _tallies.values():
            runs += tally.runs
            failed += tally.failed
            retried += tally.retried
            retry_worst = max(retry_worst, tally.retry_worst)
            overlaps += tally.overlaps
            worst_ms = max(worst_ms, tally.worst_ms)
            missed += tally.missed
            cadence = _cadence_of(tally)
            if cadence is not None:
                recurring += 1
                if tally.last_start_ms is not None and tally.active == 0:
                    quiet = now_ms - tally.last_start_ms
                    allowance = _missed_after_ms(tally) or cadence * MISSED_GAP_FACTOR
                    if quiet > allowance:
                        skipped = _js_round(quiet / cadence) - 1
                        if skipped > 0:
                            missed += skipped
        return {
            "runs": runs,
            "jobNames": len(_tallies),
            "otherNames": len(_folded_names),
            "failed": failed,
            "retried": retried,
            "retryWorst": retry_worst,
            "overlaps": overlaps,
            "worstMs": _js_round(worst_ms),
            "p95Ms": _percentile(_durations, 95),
            "waitRuns": _wait_runs,
            "waitP95Ms": _percentile(_waits, 95) if _wait_runs > 0 else None,
            "missed": missed,
            "recurring": recurring,
            "hostedRuns": _hosted_runs,
            "manualRuns": _manual_runs,
            "attachedSystems": len(_attached_systems),
            "unattachedSystems": len(_unattached_systems),
        }


def clear_job_work() -> None:
    global _wait_runs, _hosted_runs, _manual_runs
    try:
        with _lock:
            _tallies.clear()
            _folded_names.clear()
            _durations.clear()
            _waits.clear()
            _wait_runs = _hosted_runs = _manual_runs = 0
            _attached_systems.clear()
            _unattached_systems.clear()
        # The declarations go with them: the tallies they belong to are gone, so
        # an answer arriving later would have nowhere to land.
        job_reporter.clear_job_rhythm_declarations()
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "JOB_PREFIX", "MAX_JOB_NAMES", "CADENCE_MIN_GAPS", "CADENCE_MAX_SPREAD",
    "MISSED_GAP_FACTOR", "JobRun", "track_job", "begin_job", "expect_every",
    "get_job_work_stats", "clear_job_work", "note_job_system_attached",
    "note_job_system_unattached", "unattached_job_system_count",
]