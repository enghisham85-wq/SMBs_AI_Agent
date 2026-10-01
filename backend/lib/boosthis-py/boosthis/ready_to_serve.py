"""When this app became ready to serve — the boundary a cold start is measured to.

WHY THIS EXISTS
---------------

The cold-start reading used to end at OUR enable call: process start -> the
moment the host called ``enable_telemetry``. Everything the app did after that
— building a pool, warming a cache, running migrations, binding the port — fell
outside the interval, and the number was rated anyway. A service with a
deliberate delay planted after the enable call reported a fraction of a second
and was rated *good*. The reading was correct about an interval nobody asked
about, under a name that promises the one they did.

A Python process can watch the moment that matters without asking the developer
for anything. Every server in this runtime — uvicorn, gunicorn, waitress,
hypercorn, the Werkzeug/Django development servers, anything built on
``socketserver`` or ``asyncio.loop.create_server`` — reaches the network through
``socket.socket.listen``, which is the call that makes the socket start
accepting connections. That is the one boundary they all share, and it is a
Python-level method on a Python-level class, so it can be wrapped exactly the
way the Node kit wraps ``net.Server.prototype.listen``.

WHAT IT DOES
------------

Replaces that one method with a wrapper that freezes the process age the FIRST
time any socket in this process begins listening, then calls straight through.
Nothing is ever re-read, and the wrapper adds one function call to a call a
server makes once in its life.

WHAT IT REFUSES TO DO
---------------------

It never decides anything about a request, never delays a bind, and never lets a
failure of its own reach the host: an unpatchable ``socket`` module, a
restricted interpreter, or a throw anywhere inside simply leaves the mark unset.
An unset mark is not a zero and not a guess — ``runtime_vitals`` then reports the
interval it CAN see (process start -> the kit was switched on) as an explicitly
partial, unrated reading.

Servers that never surface a Python-level listen are exactly the case that
fallback exists for: a gunicorn worker inherits an already-listening socket from
its master and never calls ``listen`` itself, so a pre-forked worker keeps
reporting the partial interval rather than inventing a readiness it never saw.

See docs/cold-start-boundary-contract.md.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from .runtime_flags import is_boosthis_disabled

# Marks our own wrapper so a second arm() (or a second copy of the kit in one
# process) never wraps a wrapper.
_WRAPPER_MARK = "_boosthis_ready_to_serve_wrapper"

_lock = threading.Lock()

# Frozen process age (ms) at the first `listen` call in this process. None =
# never seen: nothing has bound a socket yet, it was bound before we were armed,
# it was inherited from a parent, or this is not a server at all.
_ready_ms: Optional[int] = None

# The unwrapped method, kept so a host (and the tests) can have the interpreter
# back exactly as it was found.
_original_listen: Optional[Any] = None
_armed = False


def arm(read_age_ms: Callable[[], Optional[int]]) -> None:
    """Start watching for the first socket that begins listening.

    IDEMPOTENT — a second call while armed does nothing, and a wrapper this kit
    already installed is never wrapped again. ``read_age_ms`` is the caller's
    process-age reader (``runtime_vitals``' /proc reader in the live kit, a stub
    in the tests): passing it in keeps this module free of a circular import.

    Guest-safe: any failure leaves the mark unset and the host untouched.
    """
    global _armed, _original_listen

    if is_boosthis_disabled():
        return

    try:
        import socket as socket_module

        with _lock:
            if _armed:
                return
            original = socket_module.socket.listen
            if getattr(original, _WRAPPER_MARK, False):
                # Someone else's copy of this kit already watches this call; its
                # mark is as good as ours would be.
                _armed = True
                return

            def listen(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                # The mark is taken BEFORE the call so a slow bind cannot be
                # counted as startup, and a failure here can never stop it.
                try:
                    _mark(read_age_ms)
                except Exception:  # noqa: BLE001 - never reach the host
                    pass
                return original(self, *args, **kwargs)

            setattr(listen, _WRAPPER_MARK, True)
            socket_module.socket.listen = listen  # type: ignore[assignment]
            _original_listen = original
            _armed = True
    except Exception:  # noqa: BLE001 - an unpatchable runtime is a partial read
        pass


def _mark(read_age_ms: Callable[[], Optional[int]]) -> None:
    """Freeze the process age, first call wins. Never raises."""
    global _ready_ms

    with _lock:
        if _ready_ms is not None:
            return
    age: Optional[int] = None
    try:
        age = read_age_ms()
    except Exception:  # noqa: BLE001
        age = None
    if age is None:
        return
    with _lock:
        if _ready_ms is None:
            _ready_ms = int(age)


def ready_ms() -> Optional[int]:
    """The frozen process age (ms) at the first listen, or None if never seen."""
    with _lock:
        return _ready_ms


def is_armed() -> bool:
    """Whether the watch is in place. Used by the kit's own tests."""
    with _lock:
        return _armed


def disarm() -> None:
    """Put ``socket.socket.listen`` back exactly as it was found.

    The frozen mark is deliberately KEPT: it is a fact about this process that
    stays true after we stop watching. Never raises.
    """
    global _armed, _original_listen

    try:
        import socket as socket_module

        with _lock:
            if _original_listen is not None:
                current = socket_module.socket.listen
                if getattr(current, _WRAPPER_MARK, False):
                    socket_module.socket.listen = _original_listen  # type: ignore[assignment]
            _original_listen = None
            _armed = False
    except Exception:  # noqa: BLE001
        pass


def _reset_for_test() -> None:
    """Unwatch AND forget the mark. Test-only."""
    global _ready_ms

    disarm()
    with _lock:
        _ready_ms = None
