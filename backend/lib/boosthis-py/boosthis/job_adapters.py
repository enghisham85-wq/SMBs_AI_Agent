"""Attach only to job systems the host has already imported.

The hard rule is that observation never imports a queue on the application's
behalf. Detection reads :data:`sys.modules` only; every patch is reversible and
all measurement failures leave the host call and its value alone.
"""

from __future__ import annotations

import functools
import inspect
import sys
import time
from datetime import datetime
from typing import Any, Callable

from boosthis.job_work import (
    JobRun,
    begin_job,
    measured_job_scope,
    note_job_system_attached,
    note_job_system_unattached,
)
from boosthis.runtime_flags import is_boosthis_disabled

WATCHABLE_JOB_SYSTEMS = ("celery", "rq", "apscheduler", "starlette")
UNWATCHABLE_JOB_SYSTEMS = ("dramatiq", "huey", "arq", "django_q", "schedule")

_patches: list[tuple[str, Any, str, Any, Any]] = []
_signal_links: list[tuple[Any, Callable[..., Any]]] = []
_installed: set[str] = set()
_last_arm_at = 0.0
ARM_RETRY_MS = 30_000


def _loaded(name: str) -> bool:
    return any(key == name or key.startswith(name + ".") for key in sys.modules)


def _epoch_ms(value: Any) -> float | None:
    try:
        if isinstance(value, datetime):
            return value.timestamp() * 1000
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:  # noqa: BLE001
        pass
    return None


async def _await_and_close(awaitable: Any, run: JobRun | None) -> Any:
    # The scope is re-entered here because the awaited half of the handler
    # resumes in its own step: without it, a mark made after the first `await`
    # would be counted a second time.
    with measured_job_scope():
        try:
            out = await awaitable
        except BaseException as exc:
            if run is not None:
                run.failed(exc)
            raise
    if run is not None:
        run.done()
    return out


def _invoke_measured(run: JobRun | None, call: Callable[[], Any]) -> Any:
    """Call the host's own handler, then close the run exactly once.

    Everything the handler does is marked as already measured, so a project
    that both auto-attaches AND still marks its handlers by hand records one
    run rather than two. A synchronous throw is recorded as a failure here and
    re-raised unchanged — the host's own error is never swallowed or replaced.
    """
    with measured_job_scope():
        try:
            out = call()
        except BaseException as exc:
            if run is not None:
                run.failed(exc)
            raise
    if inspect.isawaitable(out):
        return _await_and_close(out, run)
    if run is not None:
        run.done()
    return out


def _remember(system: str, owner: Any, attr: str, original: Any, wrapper: Any) -> None:
    setattr(owner, attr, wrapper)
    _patches.append((system, owner, attr, original, wrapper))
    _installed.add(system)
    note_job_system_attached(system)


def _install_rq() -> bool:
    try:
        module = sys.modules.get("rq.worker")
        worker = getattr(module, "Worker", None)
        original = getattr(worker, "perform_job", None)
        if not callable(original):
            return False

        @functools.wraps(original)
        def wrapper(self: Any, job: Any, *args: Any, **kwargs: Any) -> Any:
            run: JobRun | None = None
            try:
                retries_left = getattr(job, "retries_left", None)
                retry = getattr(job, "retry", None)
                max_retries = getattr(retry, "max", None)
                attempt = (
                    max(1, int(max_retries) - int(retries_left) + 1)
                    if max_retries is not None and retries_left is not None
                    else 1
                )
                run = begin_job(
                    getattr(job, "func_name", None) or "rq",
                    queued_at_ms=_epoch_ms(getattr(job, "enqueued_at", None)),
                    attempt=attempt,
                    system="rq",
                )
            except Exception:  # noqa: BLE001
                run = None
            return _invoke_measured(run, lambda: original(self, job, *args, **kwargs))

        _remember("rq", worker, "perform_job", original, wrapper)
        return True
    except Exception:  # noqa: BLE001
        return False


def _install_starlette() -> bool:
    try:
        module = sys.modules.get("starlette.background")
        cls = getattr(module, "BackgroundTask", None)
        original = getattr(cls, "__call__", None)
        if not callable(original):
            return False

        @functools.wraps(original)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            run: JobRun | None = None
            try:
                fn = getattr(self, "func", None)
                name = getattr(fn, "__name__", None) or "starlette-background"
                run = begin_job(name, system="starlette")
            except Exception:  # noqa: BLE001
                run = None
            with measured_job_scope():
                try:
                    out = await original(self, *args, **kwargs)
                except BaseException as exc:
                    if run is not None:
                        run.failed(exc)
                    raise
            if run is not None:
                run.done()
            return out

        _remember("starlette", cls, "__call__", original, wrapper)
        return True
    except Exception:  # noqa: BLE001
        return False


def _install_celery() -> bool:
    """Use Celery's public signals; no task body or arguments are inspected."""
    try:
        signals = sys.modules.get("celery.signals")
        if signals is None:
            return False
        active: dict[str, JobRun] = {}
        # Celery hands us a pair of signals rather than a callable to wrap, so
        # the "already measured" mark is set when the task starts and cleared
        # when it ends. Both signals fire in the worker's own execution
        # context, around the task body, which is exactly the span a nested
        # by-hand mark would sit in.
        scopes: dict[str, Any] = {}

        def key(task_id: Any, task: Any) -> str:
            return str(task_id or id(task))

        def enter_scope(k: str) -> None:
            try:
                scope = measured_job_scope()
                scope.__enter__()
                scopes[k] = scope
            except Exception:  # noqa: BLE001
                pass

        def leave_scope(k: str) -> None:
            try:
                scope = scopes.pop(k, None)
                if scope is not None:
                    scope.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

        def prerun(sender: Any = None, task_id: Any = None, task: Any = None, **_: Any) -> None:
            try:
                request = getattr(task or sender, "request", None)
                attempt = int(getattr(request, "retries", 0) or 0) + 1
                queued = (
                    _epoch_ms(getattr(request, "eta", None))
                    or _epoch_ms(getattr(request, "sent_at", None))
                    or _epoch_ms(getattr(request, "published_at", None))
                )
                name = getattr(sender or task, "name", None) or "celery"
                k = key(task_id, task or sender)
                active[k] = begin_job(
                    name, queued_at_ms=queued, attempt=attempt, system="celery"
                )
                enter_scope(k)
            except Exception:  # noqa: BLE001
                pass

        def finish(task_id: Any = None, task: Any = None, sender: Any = None, **_: Any) -> None:
            try:
                k = key(task_id, task or sender)
                leave_scope(k)
                run = active.pop(k, None)
                if run is not None:
                    run.done()
            except Exception:  # noqa: BLE001
                pass

        def fail(
            task_id: Any = None, task: Any = None, sender: Any = None,
            exception: Any = None, reason: Any = None, **_: Any
        ) -> None:
            try:
                k = key(task_id, task or sender)
                leave_scope(k)
                run = active.pop(k, None)
                if run is not None:
                    run.failed(exception if exception is not None else reason)
            except Exception:  # noqa: BLE001
                pass

        for signal_name, receiver in (
            ("task_prerun", prerun),
            ("task_postrun", finish),
            ("task_failure", fail),
            ("task_retry", fail),
        ):
            signal = getattr(signals, signal_name, None)
            if signal is None or not callable(getattr(signal, "connect", None)):
                continue
            signal.connect(receiver, weak=False)
            _signal_links.append((signal, receiver))
        if not _signal_links:
            return False
        _installed.add("celery")
        note_job_system_attached("celery")
        return True
    except Exception:  # noqa: BLE001
        return False


def _install_apscheduler() -> bool:
    """Attach a listener when a scheduler starts, using APScheduler's API.

    Unlike the other three, this one measures from events rather than from a
    call it wraps, and the events are raised on the scheduler's own thread
    while the job body runs on an executor thread. There is therefore no
    execution context to mark as already measured: a project that also marks
    an APScheduler job by hand would record it twice. That limit is named in
    ``track_job``'s own documentation rather than papered over — the
    alternative, a process-wide flag, would silence unrelated jobs running at
    the same moment.
    """
    try:
        base_mod = sys.modules.get("apscheduler.schedulers.base")
        events = sys.modules.get("apscheduler.events")
        cls = getattr(base_mod, "BaseScheduler", None)
        original = getattr(cls, "start", None)
        if not callable(original) or events is None:
            return False
        submitted = getattr(events, "EVENT_JOB_SUBMITTED", 0)
        executed = getattr(events, "EVENT_JOB_EXECUTED", 0)
        errored = getattr(events, "EVENT_JOB_ERROR", 0)
        mask = submitted | executed | errored

        @functools.wraps(original)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                active: dict[str, JobRun] = {}

                def listener(event: Any) -> None:
                    try:
                        job_id = str(getattr(event, "job_id", "") or "apscheduler")
                        code = getattr(event, "code", 0)
                        if code & submitted:
                            active[job_id] = begin_job(
                                job_id, system="apscheduler"
                            )
                        elif code & (executed | errored):
                            run = active.pop(job_id, None)
                            if run is not None:
                                if code & errored:
                                    run.failed(getattr(event, "exception", None))
                                else:
                                    run.done()
                    except Exception:  # noqa: BLE001
                        pass

                self.add_listener(listener, mask)
            except Exception:  # noqa: BLE001
                pass
            return original(self, *args, **kwargs)

        _remember("apscheduler", cls, "start", original, wrapper)
        return True
    except Exception:  # noqa: BLE001
        return False


def arm_job_systems() -> None:
    """Attach to imported systems and expose known blind spots. Never raises."""
    global _last_arm_at
    if is_boosthis_disabled():
        return
    try:
        now = time.monotonic() * 1000
        if _last_arm_at and now - _last_arm_at < ARM_RETRY_MS:
            return
        _last_arm_at = now
        installers = {
            "celery": _install_celery,
            "rq": _install_rq,
            "apscheduler": _install_apscheduler,
            "starlette": _install_starlette,
        }
        for name, installer in installers.items():
            if name in _installed or not _loaded(name):
                continue
            if not installer():
                note_job_system_unattached(name)
        for name in UNWATCHABLE_JOB_SYSTEMS:
            if _loaded(name):
                note_job_system_unattached(name)
    except Exception:  # noqa: BLE001
        pass


def unpatch_job_systems() -> None:
    """Restore only patches still owned by this module. Never raises."""
    global _last_arm_at
    try:
        for signal, receiver in _signal_links[:]:
            try:
                signal.disconnect(receiver)
            except Exception:  # noqa: BLE001
                pass
        _signal_links.clear()
        for system, owner, attr, original, wrapper in _patches[:]:
            try:
                if getattr(owner, attr, None) is wrapper:
                    setattr(owner, attr, original)
            except Exception:  # noqa: BLE001
                pass
        _patches.clear()
        _installed.clear()
        _last_arm_at = 0.0
    except Exception:  # noqa: BLE001
        pass


__all__ = ["arm_job_systems", "unpatch_job_systems"]