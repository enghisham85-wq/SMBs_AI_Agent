"""Boosthis: remote kill-switch / entitlement client (Python).

Faithful port of ``lib/boosthis-runtime-node/src/killSwitch.ts`` (itself a port
of ``lib/boosthis-runtime-rn/src/killSwitch.ts``), Rung 1 of the kit-protection
design. The RN/Node doctrine applies verbatim: the VALUE lives on the server and
the kit only ever asks "am I still entitled?". When the server says anything
other than "active" the kit goes fully INERT: no bubble panel, no measuring, no
telemetry of any kind, no fix/community fetch.

Python differences from the Node port (kept idiomatic):
  - persistence is a single JSON file under ``~/.boosthis/`` (the same home dir
    the telemetry config lives in — one config per home, so a single cache file
    is correct). Note that the install id itself is NOT simply auto-generated:
    ``telemetry.resolve_install_id`` reads an explicit argument, then
    ``BOOSTHIS_INSTALL_ID``, then the persisted config, and only then mints;

  - the check-in fetch uses ``urllib`` bounded by the socket ``timeout=`` (NO
    AbortController equivalent — parity with the RN/Node contract of a bounded,
    never-hanging request);
  - the monotonic clock is ``time.monotonic()``;
  - the heartbeat runs on a daemon ``threading.Timer`` (never keeps the process
    alive), and ``force_entitlement_check`` dispatches the check on a daemon
    thread so it is non-blocking;
  - there is no React UI: ``get_entitlement_gate_kind()`` drives the injected
    dev bubble's blocking lock overlay served by ``bubble.py``.

Offline policy = GRACE (7 days). Cache the last confirmed-"active" answer and
keep working offline for ``grace_seconds``; die the moment we (a) learn a
non-active status (sticky immediately, across restarts) or (b) the grace window
lapses with no successful re-check.

ACTIVATION LOCK (ships locked): a copy that has NEVER completed a successful
server handshake is fully inert. "Activated" = the PRESENCE of a persisted
entitlement cache (even a revoked one — a registered-but-killed install must
keep heartbeating so it can learn it was restored). ``forget()``/``clear...``
wipes the cache, re-locking the kit.

CRITICAL invariants (identical to RN/Node):
 - ``is_runtime_inert()`` is SYNCHRONOUS (transmit hot path + panel endpoint call
   it). It reads an in-memory flag hydrated from cache.
 - The check-in itself MUST bypass the inert gate (it is the recovery path).
 - The check-in fetch bounds itself with the socket timeout, never hangs.
 - This module imports ONLY runtime_flags / pii — never telemetry / transmit
   (those import IT), so there is no import cycle.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Literal

from boosthis.pii import check_no_pii
from boosthis.runtime_flags import is_boosthis_disabled

# The wire entitlement states. Mirrors the server's ``EntitlementCheckResponse``
# status enum. "paused" is the calm variant of the owner's dashboard Disconnect
# pause; the kit goes inert on it exactly like "revoked".
EntitlementStatus = Literal["active", "revoked", "unpaid", "tampered", "paused"]

# How the kit UI should present the current gate (see ``get_entitlement_gate_kind``).
EntitlementGateKind = Literal[
    "none", "unregistered", "hidden", "revoked", "unpaid", "paused"
]

_VALID_STATUSES = frozenset(
    {"active", "revoked", "unpaid", "tampered", "paused"}
)

# Cache file for the last-good answer, versioned so the shape can evolve without
# misreading an old record. Lives in the same home dir the telemetry config uses.
_CACHE_DIR = Path.home() / ".boosthis"
_CACHE_PATH = _CACHE_DIR / "entitlement.json"

# The host-declared state directory. The telemetry config already honours it
# (it is a rung of the identity ladder), so an app told to keep its state
# somewhere of its own gets its own config.json — but this cache used to ignore
# it and go on reading ~/.boosthis/entitlement.json. Two installs deliberately
# given separate state therefore SHARED one entitlement verdict, and a lapsed
# one from a neighbour silenced an app that had never checked in at all. The
# suite could not see it: its isolation comes from monkeypatching the constant
# below, which the shipped kit never does.
_STATE_DIR_ENV = "BOOSTHIS_STATE_DIR"


def _cache_path() -> Path:
    """Where this install's cached answer lives, resolved at CALL time so the
    host's declared state directory wins over the shared home dir. Falls back
    to ``_CACHE_PATH`` when nothing is declared (and when the declared value is
    unusable), which is also the seam the test suite redirects."""
    declared = os.environ.get(_STATE_DIR_ENV)
    if declared:
        try:
            return Path(declared) / _CACHE_PATH.name
        except (TypeError, ValueError):  # unusable path: keep the home rung
            return _CACHE_PATH
    return _CACHE_PATH

# Default offline grace if the server answer omits one. 7 days — matches the
# server's ``GRACE_SECONDS``. Only a fallback; the server is canonical.
DEFAULT_GRACE_SECONDS = 7 * 24 * 60 * 60

# Hard ceiling on a cached grace window (30 days) — clamps a corrupt/tampered
# grace so it can never indefinitely defeat the kill-switch.
MAX_GRACE_SECONDS = 30 * 24 * 60 * 60

# Clock-jitter tolerance for a cache whose ``checked_at`` sits in the future.
CLOCK_SKEW_TOLERANCE_MS = 5 * 60 * 1000

# Periodic heartbeat interval once started. 6h — unchanged from RN/Node.
DEFAULT_INTERVAL_MS = 6 * 60 * 60 * 1000

# Network timeout for a single check-in.
DEFAULT_TIMEOUT_MS = 8000

# Minimum spacing between FORCED on-open entitlement checks. The dev bubble /
# panel opening is a user-driven enforcement edge (VAULT contract), but a user
# who opens/closes repeatedly must not hammer the server, so a forced on-open
# check runs at most once per this window. The periodic 6h heartbeat and the
# unconditional launch check are unaffected.
FORCE_CHECK_THROTTLE_MS = 60 * 1000

_LOCKED_MESSAGE = (
    "Boosthis is locked. Connect this app to Boosthis with your project key "
    "to activate it."
)

# ─── In-memory state (the synchronous source of truth for the gate) ──────

_lock = threading.RLock()

_mem_status: EntitlementStatus = "active"
_mem_inert = False
_mem_message: str | None = None
_hydrated = False

# ACTIVATION LOCK tri-state: None = pre-hydration (LOCKED, fail-closed),
# False = no valid cache (LOCKED), True = handshake proven (normal model).
_mem_activated: bool | None = None

# Active-answer grace timing (see RN notes).
_mem_checked_at: int | None = None
_mem_grace_seconds: int = DEFAULT_GRACE_SECONDS
_mem_initial_age_ms = 0
_mem_mono_at: float | None = None
_mem_max_seen_wall: int | None = None

# Monotonic timestamp (ms) of the last forced on-open check dispatched, or None
# if none this run. In-memory only.
_last_forced_check_at: float | None = None


# ─── Check-in configuration (installed by enable_telemetry) ──────────────


class EntitlementCheckinConfig:
    """Config installed by ``start_entitlement_checkin``.

    ``get_token`` is a lazy getter — the token (read OR delete) is issued by
    consent AFTER enable runs, so we read the latest value on every check-in.
    """

    __slots__ = (
        "endpoint",
        "install_id",
        "get_token",
        "kit_version",
        "integrity",
        "interval_ms",
        "timeout_ms",
        "transport",
    )

    def __init__(
        self,
        *,
        endpoint: str,
        install_id: str,
        get_token: Callable[[], str | None],
        kit_version: str,
        integrity: dict[str, Any] | None = None,
        interval_ms: int | None = None,
        timeout_ms: int | None = None,
        transport: Callable[[str, dict[str, Any], str, float],
                            tuple[int, dict[str, Any]] | None] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.install_id = install_id
        self.get_token = get_token
        self.kit_version = kit_version
        self.integrity = integrity
        self.interval_ms = interval_ms
        self.timeout_ms = timeout_ms
        # Optional injectable transport for tests: (url, body, token, timeout_s)
        # -> (status, json) | None. Defaults to the urllib path.
        self.transport = transport


_config: EntitlementCheckinConfig | None = None
_timer: threading.Timer | None = None


# ─── Change subscription (lets the panel re-read when the gate flips) ────

_listeners: set[Callable[[], None]] = set()


def _notify() -> None:
    for listener in list(_listeners):
        try:
            listener()
        except Exception:  # noqa: BLE001
            # a listener throwing must never break the kit
            pass


def subscribe_entitlement(listener: Callable[[], None]) -> Callable[[], None]:
    """Subscribe to inert-state changes. Returns an unsubscribe callable. On
    first subscription this also kicks a lazy hydrate so a host that serves the
    bubble panel before ``enable_telemetry`` still respects a cached kill from a
    previous launch."""
    _listeners.add(listener)
    if not _hydrated:
        hydrate_entitlement()

    def _unsub() -> None:
        _listeners.discard(listener)

    return _unsub


# ─── The synchronous gate ────────────────────────────────────────────────


def is_runtime_inert() -> bool:
    """The single combined gate every hot path consults. True ⇒ the kit must do
    NOTHING. The global env kill-switch always wins; the ACTIVATION LOCK comes
    next; the server-controlled entitlement state is the lever on top of both."""
    return (
        is_boosthis_disabled()
        or _mem_activated is not True
        or _mem_inert
        or _active_grace_expired()
    )


def get_entitlement_gate_kind() -> EntitlementGateKind:
    """Classify how the kit UI should present the gate. SYNCHRONOUS. The env
    kill-switch always resolves to "hidden" (it must stay a silent, total
    off-switch — never a visible overlay).

    - "none"    — not inert: render normally / render nothing.
    - "hidden"  — inert, but the kit must vanish rather than show a lock overlay
                  (env kill-switch, tampered, grace-expired offline). NOT the
                  never-checked-in case.
    - "unregistered" — running, but no check-in has ever succeeded. The bubble
                  DRAWS and the panel renders its own "Not registered yet"
                  notice: no lock overlay, nothing measured or uploaded (that
                  gate is ``is_runtime_inert``). See
                  docs/kit-bubble-draw-contract.md.
    - "revoked" — the account owner revoked this project's access.
    - "unpaid"  — the account's subscription is unpaid/frozen.
    - "paused"  — the owner paused (reversible) — calm blocking notice.
    """
    if is_boosthis_disabled():
        return "hidden"
    if not is_runtime_inert():
        return "none"
    # Never handshaken (ACTIVATION LOCK). NOT a silent state: the bubble draws
    # and the panel renders its own "Not registered yet" notice, which is the
    # only readable explanation a developer gets when the check-in cannot
    # complete at all. Measuring/uploading stay gated on ``is_runtime_inert``.
    if _mem_activated is not True:
        return "unregistered"
    if _mem_status == "revoked":
        return "revoked"
    if _mem_status == "unpaid":
        return "unpaid"
    if _mem_status == "paused":
        return "paused"
    # "active" here means the offline grace window lapsed: no server verdict to
    # explain → vanish. "tampered" also hides (no user-facing overlay copy).
    return "hidden"


def _holds_credentials() -> bool:
    """True when THIS process holds the install credential a check-in is made
    with — i.e. it is the install a cached answer could be about. A process
    that holds none has never completed a handshake from here. Never throws."""
    cfg = _config
    if cfg is None:
        return False
    try:
        return bool(cfg.get_token())
    except Exception:  # noqa: BLE001
        return False


def _is_runtime_killed_internal() -> bool:
    """Killed-only variant of the gate: True when the env kill-switch, a
    non-active server answer, or a lapsed grace window silences the kit — but
    NOT when the kit is merely LOCKED (never activated). Used only by the
    consent/registration transmit path so a fresh install can still register.

    The lapsed-grace term is the one that can trap an install for good, because
    registration is the only way back and this gate stands in front of it: an
    install that holds NO credential cannot check in (the check-in needs the
    token it does not have), so refusing its registration leaves nothing that
    could ever clear the state. It happened — a neighbouring app's cached
    "active" answer, an hour past its grace, silenced every newly started kit
    on the machine, and each one reported it as the network being unreachable.
    So a lapsed grace silences the install that EARNED it, and no other."""
    if is_boosthis_disabled():
        return True
    if _mem_status != "active":
        # A real server answer — revoked / unpaid / paused / tampered. Sticky,
        # and registration is not a way around it.
        return _mem_inert
    # Inert under an ACTIVE answer can only be a grace window that ran out.
    if not (_mem_inert or _active_grace_expired()):
        return False
    return _holds_credentials()


def has_checkin_config() -> bool:
    """True once a check-in config is installed — i.e. telemetry was enabled
    and this install has somewhere to knock. Lets a caller tell "the knock
    failed" apart from "there was never anything to knock with"."""
    return _config is not None


def is_activated() -> bool:
    """True once this copy has proven a completed server handshake."""
    return _mem_activated is True


def _clamp_grace(grace_seconds: Any) -> int:
    try:
        g = int(grace_seconds)
    except (TypeError, ValueError):
        g = 0
    if not g > 0:
        g = DEFAULT_GRACE_SECONDS
    if g > MAX_GRACE_SECONDS:
        g = MAX_GRACE_SECONDS
    return g


def _now_ms() -> int:
    return int(time.time() * 1000)


def _mono_now() -> float:
    """Read a monotonic clock (ms), never throwing — the synchronous gate stays
    crash-proof."""
    try:
        return time.monotonic() * 1000
    except Exception:  # noqa: BLE001
        return float(_now_ms())


def _record_active_timing(
    checked_at: int, grace_seconds: Any, max_seen_wall_ms: int | None = None
) -> None:
    global _mem_checked_at, _mem_grace_seconds, _mem_initial_age_ms
    global _mem_mono_at, _mem_max_seen_wall
    _mem_checked_at = checked_at
    _mem_grace_seconds = _clamp_grace(grace_seconds)
    _mem_initial_age_ms = max(0, _now_ms() - checked_at)
    _mem_mono_at = _mono_now()
    _mem_max_seen_wall = (
        max_seen_wall_ms
        if isinstance(max_seen_wall_ms, int) and max_seen_wall_ms > checked_at
        else checked_at
    )


def _clear_active_timing() -> None:
    global _mem_checked_at, _mem_mono_at, _mem_initial_age_ms, _mem_max_seen_wall
    _mem_checked_at = None
    _mem_mono_at = None
    _mem_initial_age_ms = 0
    _mem_max_seen_wall = None


def _active_grace_expired() -> bool:
    """True iff we hold an ACTIVE answer whose grace window has now lapsed.
    Effective age = MAX(wall delta, monotonic age, high-water age) so rolling
    the clock backward can only make the kit expire sooner, never later."""
    if _mem_status != "active" or _mem_checked_at is None:
        return False
    wall_age_ms = _now_ms() - _mem_checked_at
    mono_age_ms = (
        float("-inf")
        if _mem_mono_at is None
        else _mem_initial_age_ms + (_mono_now() - _mem_mono_at)
    )
    high_water_age_ms = (
        float("-inf")
        if _mem_max_seen_wall is None
        else _mem_max_seen_wall - _mem_checked_at
    )
    age_ms = max(wall_age_ms, mono_age_ms, high_water_age_ms)
    if age_ms < 0:
        return False
    return age_ms > _mem_grace_seconds * 1000


def _maybe_expire_active_grace() -> None:
    global _mem_inert
    if not _mem_inert and _active_grace_expired():
        _mem_inert = True
        _notify()


def _bump_and_persist_max_seen_wall() -> None:
    global _mem_max_seen_wall
    if _mem_status != "active" or _mem_checked_at is None:
        return
    now = _now_ms()
    if _mem_max_seen_wall is not None and now <= _mem_max_seen_wall:
        return
    _mem_max_seen_wall = now
    try:
        _write_cache(
            {
                "status": "active",
                "checkedAt": _mem_checked_at,
                "graceSeconds": _mem_grace_seconds,
                "maxSeenWallMs": _mem_max_seen_wall,
            }
        )
    except Exception:  # noqa: BLE001
        # non-fatal — the in-memory high-water mark already advanced
        pass


def _on_offline_checkin() -> None:
    _bump_and_persist_max_seen_wall()
    _maybe_expire_active_grace()


def get_entitlement_status() -> EntitlementStatus:
    """Last known entitlement status (cached). For the panel's inert message."""
    return _mem_status


def get_entitlement_message() -> str | None:
    """Human-readable reason for the current non-active status, if any."""
    if not is_boosthis_disabled() and _mem_activated is not True and not _mem_inert:
        return _LOCKED_MESSAGE
    return _mem_message


# ─── Cache persistence ────────────────────────────────────────────────────


def _read_cache() -> dict[str, Any] | None:
    path = _cache_path()
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_cache(cache: dict[str, Any]) -> None:
    # Owner-only perms, matching the telemetry config's hardening.
    path = _cache_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = json.dumps(cache) 
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _remove_cache() -> None:
    try:
        _cache_path().unlink()
    except FileNotFoundError:
        pass


# ─── Hydration from cache ──────────────────────────────────────────────────


def _apply_cache(cache: dict[str, Any]) -> None:
    global _mem_activated, _mem_status, _mem_inert
    _mem_activated = True
    _mem_status = cache["status"]
    if cache["status"] != "active":
        _mem_inert = True
        _clear_active_timing()
        return
    checked_at = cache["checkedAt"]
    age_ms = _now_ms() - checked_at
    if age_ms < 0:
        if -age_ms <= CLOCK_SKEW_TOLERANCE_MS:
            checked_at = _now_ms()
        else:
            _mem_inert = True
            _clear_active_timing()
            return
    _record_active_timing(
        checked_at, cache.get("graceSeconds"), cache.get("maxSeenWallMs")
    )
    _mem_inert = _active_grace_expired()


def hydrate_entitlement() -> None:
    """Read the cached last-good answer into the in-memory gate. Idempotent —
    runs its real work at most once. No valid cache ⇒ the ACTIVATION LOCK stays
    engaged (fail-closed). Never throws."""
    global _mem_activated, _hydrated
    with _lock:
        if _hydrated:
            return
        activated = False
        try:
            parsed = _read_cache()
            if (
                parsed
                and parsed.get("status") in _VALID_STATUSES
                and isinstance(parsed.get("checkedAt"), int)
            ):
                _apply_cache(
                    {
                        "status": parsed["status"],
                        "checkedAt": parsed["checkedAt"],
                        "graceSeconds": (
                            parsed["graceSeconds"]
                            if isinstance(parsed.get("graceSeconds"), int)
                            else DEFAULT_GRACE_SECONDS
                        ),
                        "maxSeenWallMs": (
                            parsed["maxSeenWallMs"]
                            if isinstance(parsed.get("maxSeenWallMs"), int)
                            else None
                        ),
                    }
                )
                activated = True
        except Exception:  # noqa: BLE001
            # storage unreadable ⇒ LOCKED (fail-closed)
            pass
        finally:
            # Never DOWNGRADE: a live server answer may have activated the kit
            # while the storage read was in flight.
            if _mem_activated is not True:
                _mem_activated = activated
            _hydrated = True
            _notify()


# ─── The check-in (recovery path — never gated by inert) ────────────────────


def _apply_server_answer(
    status: EntitlementStatus, grace_seconds: Any, message: str | None
) -> None:
    global _mem_activated, _mem_status, _mem_message, _mem_inert
    was_inert = is_runtime_inert()
    _mem_activated = True
    _mem_status = status
    _mem_message = None if status == "active" else message
    if status == "active":
        _record_active_timing(_now_ms(), grace_seconds)
        _mem_inert = False
    else:
        _mem_inert = True
        _clear_active_timing()
    if is_runtime_inert() != was_inert:
        _notify()


def _post_checkin(
    cfg: EntitlementCheckinConfig, token: str, timeout_ms: float | None = None
) -> tuple[int, dict[str, Any]] | None:
    """POST the check-in, bounded by the socket timeout. Returns (status, body)
    or None when the check couldn't complete. Never raises.

    ``timeout_ms`` overrides the configured timeout for THIS call only. The
    exit path uses it: a process on its way out can spare a second for the
    knock that unlocks its uploads, but not the eight the heartbeat allows."""
    url = f"{cfg.endpoint.rstrip('/')}/entitlements/check"
    body: dict[str, Any] = {
        "installId": cfg.install_id,
        "kitVersion": cfg.kit_version,
    }
    integ = cfg.integrity
    if isinstance(integ, dict) and integ.get("status") in ("ok", "mismatch"):
        integ_block: dict[str, Any] = {"status": integ["status"]}
        mh = integ.get("manifestHash")
        if isinstance(mh, str) and mh:
            integ_block["manifestHash"] = mh
        body["integrity"] = integ_block
    # Defence-in-depth: the payload is fully code-defined, but run the shared
    # PII guard anyway. A hit means abort the send (never raise).
    if check_no_pii(body) is not None:
        return None
    timeout_s = (timeout_ms or cfg.timeout_ms or DEFAULT_TIMEOUT_MS) / 1000.0
    if cfg.transport is not None:
        try:
            return cfg.transport(url, body, token, timeout_s)
        except Exception:  # noqa: BLE001
            return None
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        # Keep a 401 body long enough to distinguish an authoritative
        # credentials cut from an ordinary, recoverable unauthorized answer.
        # Reading/parsing an error body is always best-effort.
        parsed = {}
        if e.code == 401:
            try:
                raw = e.read()
                candidate = json.loads(raw) if raw else {}
                if isinstance(candidate, dict):
                    parsed = candidate
            except Exception:  # noqa: BLE001
                pass
        return e.code, parsed
    except Exception:  # noqa: BLE001
        return None


def check_entitlement_now(
    timeout_ms: float | None = None,
) -> EntitlementStatus | None:
    """Run ONE check-in using the installed config. Returns the resolved status,
    or None when the check couldn't complete. A None result deliberately leaves
    the cached state intact — only an explicit server answer changes it
    (fail-open / sticky-kill).

    ``timeout_ms`` bounds this one knock more tightly than the configured
    heartbeat timeout; :mod:`boosthis.exit_flush` passes it so a shutdown is
    delayed by a known, short amount.

    NOTE: intentionally NOT gated by ``is_runtime_inert()`` — this is the path
    by which a revoked install learns it has been restored, and the path by
    which an install that has never yet activated learns that it may upload."""
    global _hydrated
    if is_boosthis_disabled():
        return None
    cfg = _config
    if cfg is None:
        return None
    token = cfg.get_token()
    if not token:
        return None  # not registered yet — the kit stays LOCKED
    try:
        result = _post_checkin(cfg, token, timeout_ms=timeout_ms)
        if result is None or not (200 <= result[0] < 300):
            if (
                result is not None
                and result[0] == 401
                and isinstance(result[1], dict)
                and result[1].get("error") == "credentials_cut"
            ):
                data = result[1]
                reason = data.get("reason")
                if reason not in {"revoked", "unpaid", "tampered", "paused"}:
                    reason = "revoked"
                detail = (
                    data.get("detail")
                    if isinstance(data.get("detail"), str)
                    else None
                )
                with _lock:
                    _apply_server_answer(
                        reason, DEFAULT_GRACE_SECONDS, detail
                    )
                    try:
                        _write_cache(
                            {
                                "status": reason,
                                "checkedAt": _now_ms(),
                                "graceSeconds": DEFAULT_GRACE_SECONDS,
                            }
                        )
                    except Exception:  # noqa: BLE001
                        pass  # cache write failure is non-fatal
                    _hydrated = True
                return reason
            # Unreachable / 401 / 429 → keep the cached answer, advance the
            # high-water marker, and let a lapsed active grace expire.
            with _lock:
                _on_offline_checkin()
            return None
        data = result[1]
        status = data.get("status")
        if status not in _VALID_STATUSES:
            return None  # unexpected shape — don't act on it
        grace_seconds = (
            data["graceSeconds"]
            if isinstance(data.get("graceSeconds"), int)
            else DEFAULT_GRACE_SECONDS
        )
        with _lock:
            _apply_server_answer(status, grace_seconds, data.get("message"))
            try:
                now = _now_ms()
                cache: dict[str, Any] = {
                    "status": status,
                    "checkedAt": now,
                    "graceSeconds": grace_seconds,
                }
                if status == "active":
                    cache["maxSeenWallMs"] = now
                _write_cache(cache)
            except Exception:  # noqa: BLE001
                pass  # cache write failure is non-fatal
            _hydrated = True
        return status
    except Exception:  # noqa: BLE001
        with _lock:
            _on_offline_checkin()
        return None  # never let a check-in raise into the host


def force_entitlement_check(force: bool = False) -> None:
    """Trigger a FRESH server entitlement check — the on-open / cold-start
    enforcement edge of the VAULT contract. Unlike the cached synchronous gate,
    this always attempts to reconfirm with the server (subject to the throttle);
    it does NOT short-circuit on a cache-satisfied "active" answer.

    Non-blocking (runs on a daemon thread), fully self-guarded. Offline /
    unreachable stays UNCHANGED (a network failure keeps the cached answer +
    grace window) — EXCEPT a sticky revoked/killed answer, which stays inert.

    :param force: When True (cold start / init), bypass the throttle. When False
        (on-open), dispatch at most once per ``FORCE_CHECK_THROTTLE_MS``.
    """
    global _last_forced_check_at
    if is_boosthis_disabled():
        return
    if _config is None:
        return
    with _lock:
        if not force:
            now = _mono_now()
            if (
                _last_forced_check_at is not None
                and now - _last_forced_check_at < FORCE_CHECK_THROTTLE_MS
            ):
                return  # throttled — a forced check ran within the window
            _last_forced_check_at = now
        else:
            # A forced (launch) check resets the throttle window so an on-open
            # check immediately after cold start doesn't fire a redundant knock.
            _last_forced_check_at = _mono_now()
    t = threading.Thread(target=check_entitlement_now, daemon=True)
    t.start()


# ─── Lifecycle (called by enable_telemetry / forget) ────────────────────────


def _stop_timer() -> None:
    global _timer
    if _timer is not None:
        try:
            _timer.cancel()
        except Exception:  # noqa: BLE001
            pass
        _timer = None


def _schedule_next_tick(interval_ms: int) -> None:
    global _timer

    def _tick() -> None:
        try:
            check_entitlement_now()
        finally:
            # Re-arm only while a config is still installed.
            if _config is not None:
                _schedule_next_tick(interval_ms)

    _timer = threading.Timer(interval_ms / 1000.0, _tick)
    _timer.daemon = True  # never keep the process alive just for the heartbeat
    _timer.start()


def start_entitlement_checkin(cfg: EntitlementCheckinConfig) -> None:
    """Install the check-in config and start the heartbeat: hydrate the cache,
    run an immediate FORCED launch check-in, then poll on an interval. Safe to
    call repeatedly — it replaces any prior config and timer. All async work is
    fire-and-forget and self-guarded so it can never crash the host."""
    global _config, _last_forced_check_at
    _config = cfg
    _stop_timer()

    def _launch() -> None:
        try:
            hydrate_entitlement()
            # Cold-start / init enforcement edge (VAULT contract): a FORCED
            # launch check that bypasses the cache-satisfied fast path.
            check_entitlement_now()
        except Exception:  # noqa: BLE001
            pass  # never raise into the host

    threading.Thread(target=_launch, daemon=True).start()
    # A launch check just ran — seed the throttle so an immediate on-open check
    # doesn't fire a redundant second knock.
    with _lock:
        _last_forced_check_at = _mono_now()
    interval = cfg.interval_ms or DEFAULT_INTERVAL_MS
    _schedule_next_tick(interval)


def stop_entitlement_checkin() -> None:
    """Stop the heartbeat and drop the config. Called from ``forget()``."""
    global _config, _last_forced_check_at
    _stop_timer()
    _config = None
    _last_forced_check_at = None


def clear_entitlement_cache() -> None:
    """Erase the persisted entitlement cache and RE-LOCK the kit. Called from
    ``forget()``. Best-effort, never throws."""
    global _mem_activated, _mem_status, _mem_inert, _mem_message
    global _mem_grace_seconds, _hydrated
    with _lock:
        was_inert = is_runtime_inert()
        _mem_activated = False
        _mem_status = "active"
        _mem_inert = False
        _mem_message = None
        _clear_active_timing()
        _mem_grace_seconds = DEFAULT_GRACE_SECONDS
        # the (now empty) state IS the truth — don't rehydrate over it
        _hydrated = True
        try:
            _remove_cache()
        except Exception:  # noqa: BLE001
            pass  # storage failure is non-fatal — the in-memory lock is engaged
        if is_runtime_inert() != was_inert:
            _notify()


# ─── Test helpers ────────────────────────────────────────────────────────


def _reset_entitlement_for_tests() -> None:
    """Reset ALL module state to first-run defaults (pre-hydration ⇒ LOCKED)."""
    global _config, _mem_activated, _mem_status, _mem_inert, _mem_message
    global _mem_checked_at, _mem_grace_seconds, _mem_initial_age_ms
    global _mem_mono_at, _mem_max_seen_wall, _last_forced_check_at
    global _hydrated
    _stop_timer()
    _config = None
    _mem_activated = None
    _mem_status = "active"
    _mem_inert = False
    _mem_message = None
    _mem_checked_at = None
    _mem_grace_seconds = DEFAULT_GRACE_SECONDS
    _mem_initial_age_ms = 0
    _mem_mono_at = None
    _mem_max_seen_wall = None
    _last_forced_check_at = None
    _hydrated = False
    _listeners.clear()


def _set_entitlement_state_for_tests(
    status: EntitlementStatus, inert: bool, message: str | None = None
) -> None:
    """Force the in-memory gate (bypasses cache/network) for assertions."""
    global _mem_activated, _mem_status, _mem_inert, _mem_message
    global _mem_checked_at, _mem_grace_seconds, _mem_initial_age_ms
    global _mem_mono_at, _mem_max_seen_wall, _hydrated
    _mem_activated = True
    _mem_status = status
    _mem_inert = inert
    _mem_message = message
    _mem_checked_at = None
    _mem_grace_seconds = DEFAULT_GRACE_SECONDS
    _mem_initial_age_ms = 0
    _mem_mono_at = None
    _mem_max_seen_wall = None
    _hydrated = True
    _notify()


__all__ = [
    "EntitlementStatus",
    "EntitlementGateKind",
    "EntitlementCheckinConfig",
    "is_runtime_inert",
    "get_entitlement_gate_kind",
    "is_activated",
    "get_entitlement_status",
    "get_entitlement_message",
    "hydrate_entitlement",
    "check_entitlement_now",
    "force_entitlement_check",
    "start_entitlement_checkin",
    "stop_entitlement_checkin",
    "clear_entitlement_cache",
    "subscribe_entitlement",
]
