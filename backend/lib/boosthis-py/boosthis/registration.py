"""Server-authoritative answer to whether this install is registered."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Literal

RegistrationState = Literal["pending", "registered", "unregistered", "unreachable"]
RegistrationVerdict = Literal["registered", "unregistered", "unknown"]

_RECHECK_SECONDS = 30.0
_lock = threading.Lock()
_state: RegistrationState = "pending"
_in_flight = False
_dispatching = False
_last_attempt_at = 0.0


def _set_state(next_state: RegistrationState) -> None:
    global _state
    with _lock:
        _state = next_state


def mark_registration_confirmed() -> None:
    _set_state("registered")


def mark_registration_refused() -> None:
    _set_state("unregistered")


def mark_registration_unreachable() -> None:
    global _state
    with _lock:
        if _state not in ("registered", "unregistered"):
            _state = "unreachable"


def get_registration_state() -> RegistrationState:
    with _lock:
        return _state


def get_registration_verdict() -> RegistrationVerdict:
    state = get_registration_state()
    if state == "registered":
        return "registered"
    if state == "unregistered":
        return "unregistered"
    return "unknown"


def check_registration_once(
    endpoint: str | None,
    install_id: str | None,
    project_key: str | None,
    *,
    now: float | None = None,
) -> None:
    """Run one bounded probe. Never raises and is self-throttled."""
    global _in_flight, _last_attempt_at
    try:
        with _lock:
            if _state in ("registered", "unregistered") or _in_flight:
                return
            current = time.monotonic() if now is None else now
            if _last_attempt_at and current - _last_attempt_at < _RECHECK_SECONDS:
                return
            if not endpoint or not install_id:
                _set_state_without_lock("unregistered")
                return
            if not project_key:
                _set_state_without_lock("unregistered")
                return
            _in_flight = True
            _last_attempt_at = current

        base = endpoint.rstrip("/")
        encoded_id = urllib.parse.quote(install_id, safe="")
        request = urllib.request.Request(
            f"{base}/installs/{encoded_id}/registration",
            headers={
                "Authorization": f"Bearer {project_key}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=8.0) as response:
                status = response.getcode()
                raw = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            raw = b""

        if status == 200:
            try:
                body = json.loads(raw)
                registered = body.get("registered") if isinstance(body, dict) else None
            except Exception:  # noqa: BLE001
                registered = None
            if registered is True:
                mark_registration_confirmed()
            elif registered is False:
                mark_registration_refused()
            else:
                mark_registration_unreachable()
        elif status in (401, 403):
            mark_registration_refused()
        else:
            mark_registration_unreachable()
    except Exception:  # noqa: BLE001
        mark_registration_unreachable()
    finally:
        with _lock:
            _in_flight = False


def _set_state_without_lock(next_state: RegistrationState) -> None:
    global _state
    _state = next_state


def ask_registration(
    endpoint: str | None, install_id: str | None, project_key: str | None
) -> None:
    """Fire the probe in a daemon thread so a panel/status read never waits."""
    global _dispatching
    try:
        with _lock:
            if _state in ("registered", "unregistered") or _in_flight or _dispatching:
                return
            if not endpoint or not install_id or not project_key:
                _set_state_without_lock("unregistered")
                return
            _dispatching = True
        def run() -> None:
            global _dispatching
            try:
                check_registration_once(endpoint, install_id, project_key)
            finally:
                with _lock:
                    _dispatching = False
        threading.Thread(
            target=run,
            daemon=True,
            name="boosthis-registration",
        ).start()
    except Exception:  # noqa: BLE001
        with _lock:
            _dispatching = False
        mark_registration_unreachable()


def _reset_registration_for_tests() -> None:
    global _state, _in_flight, _dispatching, _last_attempt_at
    with _lock:
        _state = "pending"
        _in_flight = False
        _dispatching = False
        _last_attempt_at = 0.0