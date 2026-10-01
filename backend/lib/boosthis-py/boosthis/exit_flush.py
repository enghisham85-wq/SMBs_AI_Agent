"""Getting the last measurements out before the process goes.

THE FAILURE THIS ENDS
=====================

A Python back end that boots, does a little work and exits — a worker, a
scheduled script, a container being recycled — used to upload NOTHING at all,
however much it measured.

Every measurement is buffered by :mod:`boosthis.sample_uploader` and shipped
work-gated: a batch leaves when the NEXT measurement arrives more than fifteen
seconds later, or when a hundred pile up. Registration, by contrast, goes out
immediately. So the install exists on the developer's dashboard, checks in,
shows an "active" pill — and reads *never measured*, for ever, with nothing
anywhere saying why. Eleven installs of one real project sat in exactly that
state.

THREE separate reasons, and ALL of them have to be fixed or the fix is theatre:

1. **Nothing runs on the way out.** There was no exit hook of any kind, so a
   short-lived process simply took the buffer with it.
2. **The upload rides a thread the interpreter may discard.** The work-gated
   flush runs on a ``daemon=True`` thread; at shutdown CPython does not join
   those, it freezes them. A flush that began a moment before exit is not
   waited for. So even a process that *did* trigger a flush could lose it.
3. **The ACTIVATION LOCK swallows the send, and calls it a success.** Until an
   entitlement check-in has come back, ``is_runtime_inert()`` is true and every
   upload short-circuits inside the transport with a local ``204`` and no
   network call at all. That check-in is *also* fired on a daemon thread at
   launch, so a process measured in hundreds of milliseconds never completes
   one. Fixing (1) and (2) alone changes nothing whatsoever for exactly the
   processes this module exists for — verified against a live server before
   this paragraph was written.

This module answers all three: it asks the server whether this install may
upload, inline and briefly, and only then drains the queues — also inline, in
the thread that is on its way out, bounded by one wall-clock budget.

Note what the third fix is NOT. It does not relax the lock. It performs the
check-in the launch thread never got to finish, and takes the server's answer:
a revoked, unpaid or paused install is told so, stays inert, and uploads
nothing. The lock still decides; it is merely asked in time.

WHAT CLOSES A PROCESS, AND WHAT WE DO ABOUT EACH
================================================

``atexit`` — a normal return from ``main``, ``sys.exit()``, an unhandled
exception, or a host's own SIGTERM handler that ends in ``sys.exit()``.
CPython joins non-daemon threads and *then* runs ``atexit`` callbacks, so a
daemon thread is still alive at that point and an inline HTTP call still works.
This is the ordinary case and it is covered.

``SIGTERM`` — a container being recycled, ``docker stop``, a process manager
cycling a worker. Python's default disposition kills the process outright and
``atexit`` never runs, which is precisely the shape of the installs that
started this. So we take the signal — but ONLY when the host has not already
taken it, and having taken it we **own the whole exit**: flush under a short
budget, put the default disposition back, and re-raise the signal so the
process dies exactly as it would have, with the same status. A listener that
flushes and then simply returns would have turned SIGTERM into a signal this
app IGNORES, which is a far worse bug than the one being fixed.

``SIGINT`` is deliberately left alone: its default already raises
``KeyboardInterrupt``, which unwinds and runs ``atexit``.

``SIGKILL``, a hard crash, or a host that suspends the container without
signalling cannot be helped by anything in this process, and nothing here
pretends otherwise.

RULES THIS MODULE KEEPS
=======================

* **Never change what the host does.** We install a signal handler only when
  the disposition is still the default one, only from the main thread, and we
  always re-raise. A second signal while we are flushing means *go now*: we
  restore and re-raise immediately rather than making the app unkillable.
* **Bounded.** The whole exit flush shares one wall-clock budget
  (:data:`EXIT_FLUSH_BUDGET_MS`). An unreachable server delays an exit by that
  much and no more, and a failed batch is not retried.
* **Silent and safe.** Nothing here raises, ever — an instrumentation library
  may not turn a clean exit into a stack trace.
* **Idempotent.** Installing twice registers one hook; running twice on an
  empty buffer does nothing.
"""

from __future__ import annotations

import atexit
import os
import signal
import threading
import time
from typing import Any

from boosthis.runtime_flags import is_boosthis_disabled

#: How long the whole exit flush may take, across every queue — including the
#: activation knock below. Deliberately short: this is time added to somebody's
#: shutdown, and a process manager that sends SIGTERM usually follows it with
#: SIGKILL after a few seconds.
EXIT_FLUSH_BUDGET_MS = 2_000.0

#: The most the inline activation knock may take. It is capped well under
#: :data:`EXIT_FLUSH_BUDGET_MS` on purpose: a knock that ate the whole budget
#: would unlock the uploads and then leave no time to make one.
EXIT_ACTIVATION_TIMEOUT_MS = 900.0

#: The signals we take over when nobody else has. SIGINT is NOT here — its
#: default raises KeyboardInterrupt, which unwinds and runs ``atexit`` for us.
_OWNED_SIGNALS: tuple[int, ...] = (signal.SIGTERM,)

_lock = threading.Lock()
_atexit_installed = False
_signals_installed: set[int] = set()
_flushing = False
_last_result: dict[str, Any] = {}


def _now() -> float:
    return time.monotonic()


def _activate_inline(deadline: float) -> str:
    """Ask the server, in THIS thread, whether this install may upload yet.

    The launch check-in runs on a daemon thread, so a process that lives for a
    few hundred milliseconds exits still holding the ACTIVATION LOCK — and
    while that lock is on, every upload is swallowed inside the transport with
    a local ``204`` and no network call. Draining the queues without doing this
    first sends precisely nothing, however correct the rest of this module is.

    Returns a short word describing the outcome, recorded in the flush result
    so a proof (and the status page) can tell "sent nothing because the server
    said no" apart from "sent nothing because nobody asked":

    ``already``      already activated — nothing to do, the ordinary case.
    ``no-config``    no check-in config installed (telemetry never enabled).
    ``no-budget``    the budget was spent before we got here.
    ``unreachable``  the knock did not complete; the lock stays on.
    ``active`` / ``revoked`` / ``unpaid`` / ``paused`` / ``tampered``
                     the server's own answer, applied verbatim.

    Never raises.
    """
    try:
        from boosthis import kill_switch

        if kill_switch.is_activated():
            return "already"
        remaining = _remaining_ms(deadline)
        if remaining <= 0:
            return "no-budget"
        status = kill_switch.check_entitlement_now(
            timeout_ms=min(remaining, EXIT_ACTIVATION_TIMEOUT_MS)
        )
        if status is None:
            # Either nothing is registered (no token, no config) or the knock
            # failed. Both leave the lock on; only the first is worth naming
            # separately, because it means telemetry was never enabled at all.
            return "unreachable" if kill_switch.has_checkin_config() else "no-config"
        return str(status)
    except Exception:  # noqa: BLE001
        return "error"


def run_exit_flush(budget_ms: float | None = None) -> dict[str, Any]:
    """Ship everything buffered, inline, inside one wall-clock budget.

    Runs in the CALLING thread on purpose — a daemon thread started here would
    be frozen by the interpreter on the way out, which is the second half of
    the bug this module exists to fix.

    Returns what each queue reported sending, plus the activation outcome, for
    tests and for the live proof. Never raises.
    """
    global _flushing, _last_result
    if is_boosthis_disabled():
        return {}
    budget = EXIT_FLUSH_BUDGET_MS if budget_ms is None else max(0.0, budget_ms)
    deadline = _now() + budget / 1000.0
    with _lock:
        if _flushing:
            # Already on the way out on another thread; a second concurrent
            # drain would only fight it for the same batches.
            return {}
        _flushing = True
    result: dict[str, Any] = {}
    try:
        # Imported here rather than at module import time: this module is
        # reached from telemetry, which those modules do not import, and a
        # top-level cycle would be a footgun for no gain.
        from boosthis import sample_uploader, snapshot_mirror, span_emitter

        # BEFORE anything is drained: the lock decides whether a single one of
        # these bytes may leave. See _activate_inline.
        result["activation"] = _activate_inline(deadline)

        # Samples first. They are the measurements the project page counts, and
        # the only queue whose emptiness is visible to a customer as "never
        # measured", so they get first claim on the budget.
        result["samples"] = sample_uploader.flush_samples_blocking(
            _remaining_ms(deadline)
        )
        result["spans"] = span_emitter.flush_spans_blocking(_remaining_ms(deadline))
        # The rolled-up snapshot last: it is rebuilt on demand from the same
        # measurements, so a process that got its samples out has already told
        # the truth about itself even if this one does not fit in the budget.
        result["snapshot"] = snapshot_mirror.flush_snapshot_blocking(
            _remaining_ms(deadline)
        )
    except Exception:  # noqa: BLE001
        pass
    finally:
        with _lock:
            _flushing = False
            _last_result = dict(result)
    return result


def _remaining_ms(deadline: float) -> float:
    return max(0.0, (deadline - _now()) * 1000.0)


def last_exit_flush_result() -> dict[str, Any]:
    """What the last :func:`run_exit_flush` shipped. Test/proof surface."""
    with _lock:
        return dict(_last_result)


def _atexit_handler() -> None:
    run_exit_flush()


def _signal_handler(signum: int, _frame: Any) -> None:
    """Flush, then die exactly as we would have without this handler.

    Whatever listens to a termination signal OWNS the exit. Everything in here
    is written for that: we hand the buffer over under a short bounded wait,
    put the default disposition back, and re-raise — so the process still dies,
    with the status it would have had. A second signal arriving while we flush
    means *go now*, and takes the same route without waiting.
    """
    try:
        with _lock:
            second = _flushing
        if not second:
            run_exit_flush()
    except Exception:  # noqa: BLE001
        # A flush that throws may not become a process that will not die.
        pass
    _die_by(signum)


def _die_by(signum: int) -> None:
    """Restore the default disposition and re-raise, so the exit is the host's
    own. Falls back to ``os._exit`` with the conventional status if even that
    cannot be done — never returning is the point."""
    try:
        signal.signal(signum, signal.SIG_DFL)
        _signals_installed.discard(signum)
        signal.raise_signal(signum)
    except Exception:  # noqa: BLE001
        pass
    # raise_signal for a default-disposition SIGTERM does not return, so
    # reaching here at all means something refused it.
    try:
        os._exit(128 + signum)
    except Exception:  # noqa: BLE001
        pass


def _install_signal_flush(force: bool = False) -> list[int]:
    """Take the termination signals nobody else has taken.

    Returns the signal numbers newly installed (empty when there was nothing
    to take, which is the normal case on a second call). Never raises.

    Refuses in three situations, each of which would make things worse:

    * not on the main thread — ``signal.signal`` is only legal there;
    * the host already installed a handler — theirs almost certainly ends in a
      clean shutdown, which reaches ``atexit`` anyway, and stacking on top of
      somebody's shutdown sequence is not ours to do;
    * under the test runner (unless ``force``), where taking SIGTERM off the
      runner buys nothing and confuses a failing run.
    """
    if is_boosthis_disabled():
        return []
    if not force and os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    taken: list[int] = []
    try:
        if threading.current_thread() is not threading.main_thread():
            return []
    except Exception:  # noqa: BLE001
        return []
    for signum in _OWNED_SIGNALS:
        try:
            if signum in _signals_installed:
                continue
            if signal.getsignal(signum) is not signal.SIG_DFL:
                continue
            signal.signal(signum, _signal_handler)
            _signals_installed.add(signum)
            taken.append(signum)
        except Exception:  # noqa: BLE001
            # A platform without the signal, a nested interpreter, a host that
            # blocks the change: the atexit half still stands.
            continue
    return taken


def install_exit_flush() -> None:
    """Arm the exit path. Idempotent; safe to call from anywhere; never raises.

    Called when a submitter is wired (i.e. the moment there is something that
    could be lost) and again from ``enable_telemetry``, which usually runs on
    the host's main thread — the signal half needs the main thread, and the
    first caller is not always on it.
    """
    global _atexit_installed
    try:
        with _lock:
            first = not _atexit_installed
            _atexit_installed = True
        if first:
            atexit.register(_atexit_handler)
        _install_signal_flush()
    except Exception:  # noqa: BLE001
        pass


def exit_flush_armed() -> dict[str, Any]:
    """What is actually armed, for the status page and the live proof. A claim
    nobody can check is how this kind of fix rots."""
    with _lock:
        return {
            "atexit": _atexit_installed,
            "signals": sorted(_signals_installed),
        }


def _reset_for_tests() -> None:
    """Clear module state between hermetic test runs. The ``atexit`` callback
    is deliberately NOT unregistered — it is harmless once the submitters are
    cleared, and unregistering it would make the flag lie."""
    global _flushing, _last_result
    with _lock:
        _flushing = False
        _last_result = {}
    for signum in list(_signals_installed):
        try:
            signal.signal(signum, signal.SIG_DFL)
        except Exception:  # noqa: BLE001
            pass
        _signals_installed.discard(signum)


__all__ = [
    "EXIT_ACTIVATION_TIMEOUT_MS",
    "EXIT_FLUSH_BUDGET_MS",
    "install_exit_flush",
    "run_exit_flush",
    "last_exit_flush_result",
    "exit_flush_armed",
]
