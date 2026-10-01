"""Opt-in telemetry. Default: OFF.

Telemetry only runs after the user explicitly calls ``enable_telemetry()``
or runs ``boosthis telemetry enable`` on the CLI. State lives in
``~/.boosthis/config.json`` and contains:

- ``install_id`` — random UUIDv4 generated on first enable
- ``enabled`` — bool
- ``endpoint`` — ingest URL
- ``consent_at`` — ISO timestamp
- ``delete_token`` — bearer token returned by the server, used to authorize
  subsequent /samples and /installs/forget calls. ``None`` until the first
  successful consent registration.

The PII guard in :mod:`boosthis.pii` is still the canonical chokepoint; this
module just makes the opt-in/opt-out lifecycle explicit and provides a
default endpoint so users don't have to wire one up themselves.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
import sys
import urllib.error
import uuid
from dataclasses import dataclass

from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.project_key import (
    ProjectKeySource,
    ResolvedProjectKey,
    resolve_project_key,
)
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from boosthis import (
    build_identity,
    crash_reporter,
    event_loop_lag,
    exit_flush,
    extra_meters,
    host_surface,
    job_reporter,
    live_detectors,
    meter_axes,
    promise_declaration,
    runtime_vitals,
    snapshot_mirror,
    sample_uploader,
    route_inventory,
    span_emitter,
)
from boosthis import job_adapters
from boosthis.candidates import (
    clear_all_candidates,
    set_candidate_submitter,
    set_resolution_submitter,
)
from boosthis.kill_switch import (
    EntitlementCheckinConfig,
    clear_entitlement_cache,
    force_entitlement_check,
    start_entitlement_checkin,
    stop_entitlement_checkin,
)
from boosthis import os_meters
from boosthis.drop_report import note_server_drops
from boosthis.hosting import hosting_facts
from boosthis.pii import (
    assert_no_pii,
    check_no_pii,
    check_route_label,
    looks_like_address,
)
from boosthis.dependency_kinds import dependency_kind_for
from boosthis.project_identity import (
    _reset_kit_project,
    get_kit_project,
    parse_kit_project,
    parse_kit_promises,
    serialize_kit_project,
    serialize_kit_promises,
    set_kit_project,
    set_kit_promises,
)
from boosthis.registration import (
    ask_registration as _ask_registration,
    mark_registration_confirmed,
    mark_registration_refused,
    mark_registration_unreachable,
)
from boosthis.start_announce import say_after_startup_line
from boosthis.thresholds import RUNTIME_VERSION
from boosthis.transmit import (
    LOCKED_NOT_SENT,
    _safe_transmit_internal,
    _safe_transmit_with_response_internal,
    safe_transmit,
    safe_transmit_with_response,
)
from boosthis.sample_uploader import NOT_SENT

DEFAULT_ENDPOINT = "https://www.boosthis.com/api"
CONFIG_DIR = Path.home() / ".boosthis"
CONFIG_PATH = CONFIG_DIR / "config.json"
# The key decision, and the one-shot latch for the override notice. Declared
# here rather than only inside enable_telemetry(), because get_active_project_key()
# and the panel read them before any enable call has run.
_active_project_key = resolve_project_key(environ={})
_warned_project_key_override = False
# Positive declaration only. Ordinary Python apps did not previously report a
# server kind; a portless worker cannot be detected from absence, so worker()
# states it before registration.
#
# Read on EVERY consent registration: without this line the only process that
# can register is one that imported the worker helper first, and every ordinary
# app fails registration with a NameError it reports as "could not reach the
# Boosthis API" while the network is perfectly fine.
_declared_server_kind: str | None = None

# ── WHERE THE INSTALL ID LIVES ──────────────────────────────────────────────
#
# The install id is the axis every page, every daily rollup and every verdict
# hangs on. A process that mints a fresh one at each launch is not one project
# measured for three days; it is twenty projects measured for six minutes each,
# and daily history — which is only ever written for COMPLETED days — can never
# contain a single day of it.
#
# This kit already wrote its id to ``~/.boosthis/config.json``, so the id was
# MEANT to survive. In production it did not: eleven installs of one Python
# service in four days. One fixed path is the reason. If the home directory is
# read-only, absent, or shared in a way that stops us making it owner-only, the
# write simply raised and the next launch started again from nothing.
#
# So the path is now a LADDER, probed by actually writing, in the same order and
# with the same words as the Node kit's storage layer:
#
#   configured  BOOSTHIS_STATE_DIR — a directory the host explicitly named.
#   home        ~/.boosthis (whatever CONFIG_DIR points at).
#   scratch     the system temp directory. As durable as the container and no
#               more — which is a real answer, and one we now SAY.
#   memory      nothing writable was found. Every restart is a new install, and
#               that is reported rather than left to look like a fault.
#
# SECURING THE DIRECTORY IS PART OF THE PROBE, not a step after it. The file
# holds live credentials (delete_token, invite_key), so a rung that cannot be
# made owner-only is not usable — we fall to the next one rather than leaving
# tokens somewhere another local account can read.
IDENTITY_STATE_DIR_ENV = "BOOSTHIS_STATE_DIR"

# ── WHICH INSTALL AM I ──────────────────────────────────────────────────────
#
# The ladder above decides WHERE the id is kept. This decides WHICH id it is,
# and the two are not the same question: every route that loses an identity is
# a route where the store itself is unreliable, so the store cannot be the
# primary answer. An id named in the environment is the one mechanism that
# survives a different working directory, an unwritable state directory AND an
# ephemeral filesystem, so it outranks whatever happens to be on disk.
#
# One order, shared with every other kit — docs/install-identity-precedence.md:
#
#   explicit argument > BOOSTHIS_INSTALL_ID > persisted > freshly minted
#
# ...and then, and only then, a recorded rotation OF THE ID JUST CHOSEN is
# adopted (see ``baked_install_id``). Rotation is a continuation of the chosen
# identity, never an override of it.
#
# Both halves of this kit read this one resolver: the telemetry startup that
# registers, and ``app_scope``, which scopes the meters' own files. They used to
# be two resolvers that disagreed — the environment was honoured in the half
# that does not register and ignored in the half that does.
INSTALL_ID_ENV = "BOOSTHIS_INSTALL_ID"
LEGACY_INSTALL_ID_ENV = "BOOSTEN_INSTALL_ID"

# The server rejects anything that is not a UUID at registration
# (``invalid_install_id``), so a mis-typed value passed through here would
# register nothing and explain nothing. It is refused where it is READ, naming
# the variable it came from, exactly like a mis-pasted project key.
INSTALL_ID_SHAPE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_refused_install_id_sources: set[str] = set()

# The decision for this process, cached against the inputs that produced it so
# that a test which redirects CONFIG_DIR is never answered from a stale probe.
_store_choice: tuple[Any, Path | None, str] | None = None
# The last-resort store. Keeps the process consistent with itself for as long as
# it lives; it is explicitly NOT durability, and the consent payload says so.
_memory_config: dict[str, Any] | None = None
# Did this launch pick up an id that a previous one wrote? The one fact that
# distinguishes "durable, and proven" from "durable, first run".
_identity_restored = False
# A probe that passed is a PREDICTION; the real write is the fact. When the
# chosen rung then refuses the write, the id lives only in this process — and
# saying "home" at that point tells the server the install is restart-safe when
# it is not, which is the exact false all-clear this whole task exists to remove.
_store_degraded_to_memory = False
# Has this process already said, out loud, that it cannot keep its identity?
# One line per process: the notice is a warning about the NEXT restart, and
# repeating it on every config write would be noise, not evidence.
_identity_notice_said = False
# Was an id named for us (argument or environment)? Then the store does not
# decide who we are and there is nothing to warn about.
_identity_pinned = False


# O_NOFOLLOW is POSIX. Where it is absent (Windows) the flag is 0, and both the
# directory walk below and _open_state_file fall back to looking at the name
# without following it and then confirming that what was opened is what the name
# pointed at — see each of them.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

# POSIX-only, and the kit must still import where it is absent. Without it the
# non-blocking flag simply stays set, which is a no-op on the regular file this
# only ever proceeds with.
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - not POSIX
    _fcntl = None  # type: ignore[assignment]

_WRITE_PROBE_NAME = ".boosthis-write-probe"

# Module state that setters below re-bind with ``global``. Declaring a global
# inside a function does NOT create it: until the setter runs the name does not
# exist, and every READER of it raises NameError instead of seeing a default.
# That is not a theoretical hazard — consent registration reads
# ``_declared_server_kind`` on the very first call, so with the name absent the
# whole registration threw and every ordinary Python app "stayed inactive"
# with one line of stderr and no other symptom. Bind all three here.
_declared_server_kind: str | None = None
_warned_project_key_override: bool = False
_active_project_key: ResolvedProjectKey | None = None


def get_active_project_key() -> ResolvedProjectKey:
    """Return the key decision frozen by the latest enable call.

    Before any enable call there is nothing frozen, so resolve the decision the
    same way the enable call would — from the environment and the persisted
    config — rather than leaving callers to read a name that does not exist.
    """
    if _active_project_key is not None:
        return _active_project_key
    existing = _read_config()
    return resolve_project_key(configured=existing.invite_key if existing else None)


@dataclass
class TelemetryConfig:
    install_id: str
    enabled: bool
    endpoint: str
    consent_at: str | None
    # Bearer token returned by the server on first successful consent. Used to
    # authorize POST /samples and POST /installs/forget. ``None`` until the
    # server has registered us — in that case the next transmit will retry
    # consent first.
    delete_token: str | None = None
    # Project key issued from the account dashboard. Sent as ``Authorization:
    # Bearer <invite_key>`` on the /installs/consent call only — registration
    # is rejected with 403 when no valid project key is presented. ``None``
    # until one is supplied (the kit then measures on-device only, and against
    # a local/self-hosted server that requires no key). Persisted so the
    # consent-retry paths (transmit/candidates) can re-present it.
    invite_key: str | None = None
    # Read-only token returned by the server on consent. Strictly narrower than
    # delete_token (read-only, self-scoped): it authorizes GET /snapshot and the
    # other per-install reads but is rejected by /installs/forget. Persisted so
    # the "Connect your AI" surface can hand it to the developer's own AI. We
    # only STORE it here (the actual snapshot upload authenticates with the
    # delete_token); it is the credential the developer copies into their AI.
    read_token: str | None = None
    # Developer's explicit opt-in to mirror the full perf snapshot so their own
    # AI can read the whole meter page. Default False (privacy by default).
    share_meter_with_ai: bool = False
    # Rising-edge latch of the server's ``shareMeterWithAI`` consent directive.
    # The server emits it only once the developer presses "Connect AI" in the
    # web dashboard (which provisions a web read token), so a private app starts
    # mirroring its snapshot with NO code change. Persisted so the directive
    # survives restarts.
    server_share: bool = False
    # BOTH-edge mirror of the server's ``fullTelemetry`` consent directive (the
    # dashboard's per-app "Full telemetry" switch). Unlike ``server_share`` it
    # follows the server value on every consent: ON grants the full meter
    # picture for this install, OFF withdraws ONLY the server grant (the
    # code-configured ``share_meter_with_ai`` opt-in still stands). The grant
    # governs every screen-bearing auto-upload: the snapshot mirror, the trace
    # spans and the per-request sample batches. ``disable_telemetry()``,
    # ``forget()``, and BOOSTHIS_DISABLED always win over the grant. Persisted
    # so the directive survives restarts between consents.
    server_full: bool = False
    # Optional developer-chosen display name for this service, sent on consent so
    # the developer can tell their installs apart in their own dashboard. It is
    # developer-authored metadata (never end-user data): the value passes the
    # same PII guard as every other field before it leaves the process and is
    # shown only to the owning developer. ``None`` until one is provided.
    app_name: str | None = None
    # Serialised server-resolved project identity. Written under the dynamic
    # ``project:<installId>`` store key beside this install's credentials.
    project_identity_json: str | None = None
    # Serialised standing promises, under ``promises:<installId>``. Persisted
    # for the same reason the identity is: the kit's page is a local view a
    # developer keeps open while they work, and a restart should not blank it.
    project_promises_json: str | None = None
    # The id this install was ASKED to be, before any self-healing rotation.
    # ``None`` until a rotation happens, and equal to ``install_id`` after one
    # only in the degenerate case. It exists so a rotation can be adopted on a
    # later launch WITHOUT persisted state having to outrank the developer's
    # own configuration: the launch resolves its id first (explicit > env >
    # persisted > mint) and then adopts the rotation only if this field says
    # the rotation replaced exactly that id. See
    # docs/install-identity-precedence.md.
    baked_install_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "install_id": self.install_id,
            "enabled": self.enabled,
            "endpoint": self.endpoint,
            "consent_at": self.consent_at,
            "delete_token": self.delete_token,
            "invite_key": self.invite_key,
            "read_token": self.read_token,
            "share_meter_with_ai": self.share_meter_with_ai,
            "server_share": self.server_share,
            "server_full": self.server_full,
            "app_name": self.app_name,
        }
        if self.baked_install_id:
            out["baked_install_id"] = self.baked_install_id
        if self.project_identity_json:
            out[f"project:{self.install_id}"] = self.project_identity_json
        if self.project_promises_json:
            out[f"promises:{self.install_id}"] = self.project_promises_json
        return out


def _open_state_file(path: Path, flags: int) -> int:
    """Open the credential file, refusing anything that is not a real file of
    ours.

    The directory being owner-only is not enough on its own. A configured state
    directory can be one an attacker already writes to — a shared build cache, a
    volume mounted from elsewhere — and a config.json planted there as a SYMLINK
    would have our delete token, read token and project key written straight
    through it to a file the attacker can read. Refusing the link after a
    separate lstat would still leave the window between the check and the open.

    So the refusal rides the open itself (O_NOFOLLOW), and the descriptor is
    then confirmed to be a plain file.

    Where O_NOFOLLOW does not exist — Windows — the refusal cannot ride the
    open, so it is done in two halves that together leave no window: the name is
    looked at WITHOUT following it, and after the open the descriptor is
    confirmed to BE that same file (device and inode). A link swapped in
    between the look and the open, or planted where nothing existed a moment
    ago, resolves to a different file than the name now names, and is refused.

    O_NONBLOCK is on the open and not merely on the check, because opening a
    FIFO blocks until the other end appears — the open would never return, and
    this runs inside the host application's startup. It is cleared again once
    the descriptor is known to be a regular file, where the flag means nothing
    anyway, so ordinary reads and writes behave normally.

    None of that helps if the DIRECTORY can be swapped for a link between the
    check and the open — O_NOFOLLOW says nothing about the path above the file.
    So the open happens relative to a pinned directory descriptor wherever the
    platform has one, and the name resolved here is a single component that
    cannot lead anywhere else.
    """
    dfd = _pinned_state_dir_fd(path.parent, create=False)
    if dfd is not None:
        try:
            fd = os.open(
                path.name,
                flags | _O_NOFOLLOW | _O_NONBLOCK,
                0o600,
                dir_fd=dfd,
            )
        finally:
            os.close(dfd)
        return _finish_state_fd(fd, path, confirm_by_name=False)

    pre: os.stat_result | None = None
    if not _O_NOFOLLOW:
        try:
            pre = os.lstat(path)
        except OSError:
            # Nothing there yet, or the name cannot be read at all. Either way
            # the post-open identity check below is what actually decides.
            pre = None
        if pre is not None and stat.S_ISLNK(pre.st_mode):
            raise OSError(errno.ELOOP, "state file is a symlink", str(path))
        if pre is not None and not stat.S_ISREG(pre.st_mode):
            raise OSError(errno.EINVAL, "state file is not a regular file", str(path))

    fd = os.open(path, flags | _O_NOFOLLOW | _O_NONBLOCK, 0o600)
    return _finish_state_fd(fd, path, confirm_by_name=not _O_NOFOLLOW)

def _finish_state_fd(fd: int, path: Path, *, confirm_by_name: bool) -> int:
    """Judge a freshly opened descriptor, and hand it back or close it.

    Shared by both ways in, because the judgement is the same either way: it
    has to be a regular file of ours, and it must not be left non-blocking.
    """
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "state file is not a regular file", str(path))
        if confirm_by_name:
            # The descriptor must be the file the NAME refers to, not something
            # a link points at. lstat does not follow, so for an honest file the
            # two agree, and for a link they cannot: the link has its own inode.
            here = os.lstat(path)
            if here.st_ino != info.st_ino or here.st_dev != info.st_dev:
                raise OSError(
                    errno.EPERM,
                    "state file changed between the check and the open",
                    str(path),
                )
        if _O_NONBLOCK and _fcntl is not None:
            try:
                current = _fcntl.fcntl(fd, _fcntl.F_GETFL)
                _fcntl.fcntl(fd, _fcntl.F_SETFL, current & ~_O_NONBLOCK)
            except OSError:
                pass
    except Exception:
        os.close(fd)
        raise
    return fd
def _read_config() -> TelemetryConfig | None:
    global _identity_restored
    path, _rung = _resolve_store()
    raw: Any = None
    from_disk = False
    if path is not None and path.exists():
        try:
            fd = _open_state_file(path, os.O_RDONLY)
            try:
                with os.fdopen(fd, "r", encoding="utf-8") as fh:
                    raw = json.loads(fh.read())
            finally:
                pass
            from_disk = True
        except (json.JSONDecodeError, OSError, ValueError):
            # A link, a fifo, a truncated write, a file we may not read. None of
            # them is an identity, and none of them may stop the host starting.
            raw = None
    if raw is None:
        # Either nothing durable was found at all, or the rung passed its probe
        # and then refused the real write. Either way this process can still be
        # consistent with itself — but nothing here survives it, and
        # identity_facts() reports the condition rather than letting a silent
        # stream of new installs look like a fault.
        if _memory_config is None:
            return None
        raw = _memory_config
    if not isinstance(raw, dict) or "install_id" not in raw:
        return None
    # An id written by an EARLIER run of this process is the whole point of the
    # ladder above. Recording that it was found is what lets the server tell
    # "durable, and proven" from "durable, first launch".
    if from_disk:
        _identity_restored = True
    project_raw = raw.get(f"project:{raw['install_id']}")
    stored_project = parse_kit_project(project_raw)
    if stored_project is not None:
        # This is the same startup/config restore path that restores the delete
        # and read tokens. Best-effort: malformed project data is simply ignored.
        try:
            set_kit_project(stored_project["name"], stored_project["code"])
        except Exception:  # noqa: BLE001
            pass
    promises_raw = raw.get(f"promises:{raw['install_id']}")
    stored_promises = parse_kit_promises(promises_raw)
    if stored_promises is not None:
        # Same path, same best-effort rule: unreadable promises mean the kit's
        # page shows none until the next consent reply lands.
        try:
            set_kit_promises(stored_promises)
        except Exception:  # noqa: BLE001
            stored_promises = None
    return TelemetryConfig(
        install_id=str(raw["install_id"]),
        enabled=bool(raw.get("enabled", False)),
        endpoint=str(raw.get("endpoint") or DEFAULT_ENDPOINT),
        consent_at=raw.get("consent_at"),
        delete_token=raw.get("delete_token") or None,
        invite_key=raw.get("invite_key") or None,
        read_token=raw.get("read_token") or None,
        share_meter_with_ai=bool(raw.get("share_meter_with_ai", False)),
        server_share=bool(raw.get("server_share", False)),
        server_full=bool(raw.get("server_full", False)),
        app_name=(str(raw["app_name"]) if raw.get("app_name") else None),
        project_identity_json=(
            project_raw if isinstance(project_raw, str) and stored_project else None
        ),
        project_promises_json=(
            promises_raw if isinstance(promises_raw, str) and stored_promises else None
        ),
        baked_install_id=(
            str(raw["baked_install_id"]) if raw.get("baked_install_id") else None
        ),
    )

def _write_all(fd: int, data: bytes) -> None:
    """Hand the file every byte, however many writes that takes.

    A single ``os.write`` is allowed to accept only PART of what it was given,
    and a short write is a success, not an error. Believing it here would leave
    half a JSON document where the identity is kept: the next launch cannot
    parse it, so it mints a brand-new install id and the delete token, read
    token and project key in that file are gone with it — the endless run of
    one-shot installs this whole change exists to stop, caused by the fix.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "the state file accepted no bytes")
        view = view[written:]
def _write_config(cfg: TelemetryConfig) -> None:
    global _memory_config, _store_degraded_to_memory
    path, _rung = _resolve_store()
    if path is None:
        # Nowhere durable was found. Keep the config in memory so THIS process
        # stays one install, and let identity_facts() report the condition
        # rather than quietly minting a new id on every launch and leaving the
        # developer to wonder why their project page is empty.
        _memory_config = cfg.to_dict()
        return
    directory = path.parent
    # Force owner-only read/write on the config file regardless of the process
    # umask. The file stores live credentials (delete_token, invite_key) that
    # must not be readable by other local accounts.
    #
    # Use os.open + os.fchmod rather than Path.write_text so that:
    # - The mode=0o600 arg applies when the file is newly created.
    # - os.fchmod(fd, 0o600) hardens the permissions AFTER the write even
    #   when the file already existed with looser permissions (e.g. 0o644
    #   from an older Boosthis version that relied on the process umask).
    #   open(2) only honours the mode argument on creation, not on truncation
    #   of an existing file, so the explicit fchmod is required to close the
    #   upgrade-path gap.
    content = json.dumps(cfg.to_dict(), indent=2) + "\n"
    try:
        # Create the directory with owner-only access (0o700) so no other local
        # user on a shared host can list or read files under it — through the
        # pinned walk, which also hardens a directory an older version of the
        # kit left at the process umask, and refuses one that is no longer ours.
        _ensure_state_dir(directory)
        # First, judge the entry we are about to replace, through the hardened
        # open — a link there, or anything that is not our own regular file,
        # means this directory is not ours to keep credentials in, and the
        # honest answer is the reported "memory" store. NOT O_TRUNC, and not a
        # single byte is written through this descriptor: it exists only to
        # refuse.
        os.close(_open_state_file(path, os.O_WRONLY | os.O_CREAT))
        # Then write the new contents beside it and move them into place in one
        # step, so an interrupted write cannot eat the identity that is already
        # there. See _replace_state_file.
        _replace_state_file(path, content.encode())
    except OSError:
        # The rung passed its probe and then refused the real write — a full
        # disk, a volume unmounted mid-run, a symlink planted where our file
        # goes. Degrade to memory rather than raising into the host
        # application, and let the next launch re-probe.
        #
        # The reported store degrades WITH it. Nothing about this process
        # survives now, and the consent payload has to say so.
        _memory_config = cfg.to_dict()
        _store_degraded_to_memory = True
        # And say so: this is the moment the identity stopped surviving, so it
        # is the moment to tell the developer, rather than leaving them to infer
        # it later from a duplicate row.
        _announce_identity_at_risk()
        return

def _refuse_install_id_once(raw: str, source: str) -> None:
    """Say, once per source, that a configured install id cannot be used.

    The same shape as the project-key refusal: a value that CANNOT be an install
    id is refused where it is read, naming where it came from. Silently minting a
    replacement is how the far more common quiet failure hides — the loud refusal
    this kit already had was reserved for a hand-edited id and never reached.
    """
    if source in _refused_install_id_sources:
        return
    _refused_install_id_sources.add(source)
    try:
        say_after_startup_line(
            f"[boosthis] Boosthis will not use the install id from {source}: "
            "it is not a UUID (8-4-4-4-12 hex, e.g. from uuidgen). The server "
            "rejects any other shape at registration, so this install would "
            "never appear. Using this install's own id instead."
        )
    except BaseException:  # noqa: BLE001 — evidence must never block the host
        pass
def get_config() -> TelemetryConfig | None:
    """Return the on-disk config, or ``None`` if telemetry was never enabled."""
    return _read_config()


def ask_registration() -> None:
    """Fire the server-authoritative registration probe without blocking."""
    try:
        cfg = _read_config()
        if cfg is None:
            _ask_registration(None, None, None)
            return
        if cfg.delete_token or cfg.read_token:
            mark_registration_confirmed()
            return
        _ask_registration(cfg.endpoint, cfg.install_id, cfg.invite_key)
    except Exception:  # noqa: BLE001
        mark_registration_unreachable()


def is_enabled() -> bool:
    cfg = _read_config()
    return bool(cfg and cfg.enabled)


# Longest display name the server stores (matches the ``.max(60)`` on the
# consent schema). Clamp client-side so a long auto-detected name is trimmed
# rather than rejected at ingest.
APP_NAME_MAX = 60

# Launcher / generic file names that are NOT a useful app name — skip them so
# auto-detection never surfaces "python", "uvicorn", "main", etc.
_GENERIC_NAMES = {
    "",
    "-c",
    "app",
    "asgi",
    "flask",
    "gunicorn",
    "hypercorn",
    "main",
    "manage",
    "python",
    "python2",
    "python3",
    "run",
    "server",
    "uvicorn",
    "wsgi",
    "__main__",
}


def _detect_app_name() -> str | None:
    """Best-effort detection of the host app's own name so the dashboard shows a
    friendly name instead of a churny per-run install id. Purely optional and
    never raises: it tries the running entry script's file stem, then the current
    working directory's folder name, skipping generic launcher names. Returns
    ``None`` when nothing meaningful is found. The value is the app's own
    project/script name — not PII — but the caller still clamps + PII-screens it,
    so detection can never break registration.
    """
    # 1. The entry script's file stem (e.g. ``store_api.py`` -> ``store_api``).
    try:
        argv0 = sys.argv[0] if sys.argv else ""
        stem = Path(argv0).stem if argv0 else ""
        if stem and stem.lower() not in _GENERIC_NAMES:
            return stem
    except Exception:  # noqa: BLE001
        pass
    # 2. The current working directory's folder name (the project folder).
    # Skip the home directory itself: its basename is the OS username, which
    # would surface a person's name as the "app name" — never useful, and
    # against the spirit of the privacy contract even though it stays on the
    # developer's own dashboard.
    try:
        cwd = Path.cwd()
        if cwd != Path.home():
            base = cwd.name
            if base and base.lower() not in _GENERIC_NAMES:
                return base
    except Exception:  # noqa: BLE001
        pass
    return None


def _resolve_app_name(explicit: str | None) -> str | None:
    """Decide the display name for consent, friction-first: an explicit name
    (this call or previously persisted) wins and is only clamped; else a
    best-effort auto-detected name; else ``None``. The AUTO path clamps to
    :data:`APP_NAME_MAX` and DROPS a name that would trip the PII guard, so
    auto-detection can never turn a working registration into a rejected one.
    """
    if explicit is not None and explicit.strip():
        return explicit.strip()[:APP_NAME_MAX]
    try:
        auto = _detect_app_name()
    except Exception:  # noqa: BLE001
        auto = None
    if not auto:
        return None
    clamped = auto[:APP_NAME_MAX]
    return clamped if check_no_pii({"appName": clamped}) is None else None


def enable_telemetry(
    endpoint: str | None = None,
    invite_key: str | None = None,
    share_meter_with_ai: bool | None = None,
    app_name: str | None = None,
    install_id: str | None = None,
    ai_endpoints: Any = None,
) -> TelemetryConfig:
    """Opt in. Resolves this install's id, registers consent with the server,
    captures the returned delete_token, and persists the config to
    ``~/.boosthis/config.json``.

    Which install this is, in order: ``install_id`` passed here >
    ``BOOSTHIS_INSTALL_ID`` in the environment > the id this copy already
    persisted > a freshly minted UUID kept for this install and no other. Pin
    the environment variable to a fixed UUID on a host whose filesystem does not
    survive a redeploy — it is the only rung that does. The same answer is used
    by every other part of the kit (see ``resolve_install_id``).

    Registration requires a project key issued from your account dashboard:
    pass ``invite_key`` (or set ``BOOSTHIS_INVITE_KEY`` in the environment). It
    is sent as ``Authorization: Bearer <invite_key>`` on the consent call;
    without a valid key the server rejects registration with 403, no telemetry
    can flow, and the kit measures on-device only.

    Pass ``share_meter_with_ai=True`` to also mirror the full privacy-safe perf
    snapshot (the whole meter page — per-route rows, findings, axes) to the
    server so the developer's OWN AI can read the live picture back over the
    hosted MCP live-read tools. It carries only code-defined route labels +
    numeric timings/ratings (never user values, source, or PII) and passes the
    same PII guard as every other transmit. Default None / privacy by default:
    even without this flag, sharing turns on automatically once the developer
    presses "Connect AI" in the web dashboard (the server then reports the
    ``shareMeterWithAI`` directive on the next consent). The opt-in is sticky
    across restarts once on; pass ``share_meter_with_ai=False`` explicitly to
    turn it back off, or omit it to leave the persisted choice untouched.

    Idempotent — repeat calls just refresh ``consent_at`` and re-register.
    """
    # Declarations remain process-local: only their fixed provider wire code
    # can enter telemetry. The setter also merges BOOSTHIS_AI_ENDPOINTS.
    try:
        if ai_endpoints is not None:
            from boosthis.ai_providers import set_declared_ai_endpoints
            set_declared_ai_endpoints(ai_endpoints)
    except Exception:  # noqa: BLE001
        pass
    existing = _read_config()
    pinned = _clean_install_id(install_id) is not None or env_install_id() is not None
    install_id = resolve_install_id(explicit=install_id, existing=existing)
    # Say it BEFORE the consequence arrives: a launch that cannot keep its
    # identity anywhere durable becomes a brand new install on the next restart,
    # leaving a dead row on the dashboard that explains itself to nobody.
    # Nothing to say when the id was pinned — it does not depend on the store.
    _announce_identity_at_risk(pinned)
    chosen_endpoint = endpoint or os.environ.get("BOOSTHIS_INGEST_URL") or os.environ.get("BOOSTEN_INGEST_URL") or (
        existing.endpoint if existing else DEFAULT_ENDPOINT
    )
    # Never let Boosthis's OWN upload flushes self-trigger a retry-storm finding
    # naming our API host (the opt-in socket audit hook would observe them too).
    live_detectors.ignore_host(chosen_endpoint)
    # Arm request-boundary event-loop-lag sampling (async apps only; sync WSGI
    # apps simply never accrue samples, so the axis stays omitted).
    event_loop_lag.start_event_loop_lag()
    # Start runtime-vitals collection: wires the gc.callbacks pause timer and
    # arms request-boundary memory + reliability sampling. Additive, display-only
    # meters (memory stability, GC pressure, reliability) — never feed the Speed
    # score; each axis stays omitted until it has enough data to be honest.
    runtime_vitals.start_vitals()
    # Start the scheduler-latency heartbeat: a single daemon thread that sleeps
    # a fixed interval and measures how late it actually woke, giving sync/WSGI
    # apps (which have no asyncio loop to probe) an honest stall meter. Daemon
    # thread → never blocks interpreter shutdown; costs effectively nothing.
    # Additive, display-only — never feeds the Speed score; omitted until warm.
    meter_axes.start_scheduler_heartbeat()
    # A worker may never cross a request boundary. Queue observation therefore
    # arms at telemetry enable as well as remaining safe to call repeatedly.
    job_adapters.arm_job_systems()
    # Arm the way out, from the thread the host started us on. A worker, a
    # scheduled script or a container being recycled exits long before the
    # work-gated flush window comes round, and without this everything it
    # measured leaves with it. Called here as well as when the submitter is
    # wired because the signal half is only legal on the main thread, and this
    # is the call a host makes from it.
    exit_flush.install_exit_flush()
    # Freeze the cold-start / boot-time proxy ONCE, right here at enable: the
    # process age (OS start -> this call). Best-effort — a failure must never
    # break enable, and the coldStart axis is simply omitted. Additive,
    # display-only; never feeds the Speed score.
    try:
        runtime_vitals.capture_cold_start()
    except Exception:  # noqa: BLE001
        pass
    # Freeze the import-graph size once (startupImport) and arm the 2026-08
    # meter batch (GIL probe, task backlog, worker recycling, swallowed errors,
    # thread-pool starvation, blocking-async, fork churn). All additive,
    # display-only; each stays omitted until honestly computable.
    try:
        runtime_vitals.capture_startup_import()
    except Exception:  # noqa: BLE001
        pass
    # Freeze the build identity ONCE (commit / build time / age) — the source
    # for the cross-runtime Patch Lag ("exposure window") meter. Best-effort; a
    # failure must never break enable, and the build object + patchLag axis are
    # simply omitted. Additive, display-only; never feeds the Speed score.
    try:
        build_identity.capture_build_identity()
    except Exception:  # noqa: BLE001
        pass
    extra_meters.start_extra_meters()
    # Arm the 2026-08 "honest limit" batch (allocator churn, peak RSS, context
    # switching, page faults, I/O pressure, CPU entitlement, descriptor mix,
    # import churn, async slow callbacks, GC occupancy, runtime capability).
    # Boundary-sampled + snapshot-read only; Linux-only sources stay absent
    # elsewhere. Additive, display-only.
    os_meters.start_os_meters()
    global _active_project_key, _warned_project_key_override
    _active_project_key = resolve_project_key(
        explicit=invite_key,
        configured=existing.invite_key if existing else None,
    )
    if _active_project_key.overrode_shared and not _warned_project_key_override:
        _warned_project_key_override = True
        print(
            "[boosthis] Python project key overridden by "
            f"BOOSTHIS_PROJECT_KEY_PY ({_active_project_key.display}).",
            file=sys.stderr,
        )
    chosen_invite_key = _active_project_key.key
    if chosen_invite_key is None and _active_project_key.no_key_absent:
        if _active_project_key.no_key_chosen:
            _warn_stayed_inactive_once(-1, {"no_key": "chosen"})
        else:
            _warn_stayed_inactive_once(-1, {"no_key": "missing"})
    cfg = TelemetryConfig(
        install_id=install_id,
        enabled=True,
        endpoint=chosen_endpoint,
        consent_at=datetime.now(timezone.utc).isoformat(),
        delete_token=existing.delete_token if existing else None,
        # Carry the read token forward so that a second enable_telemetry() call
        # (e.g. a sibling app sharing the same HOME) does not lose it. The
        # server only returns readToken on a proven re-consent when the install
        # predates read tokens (null readTokenHash), so without this carry-over
        # the config would be written without a read_token and /_boosthis/api/
        # connect would return {"connected": false} until a full forget/re-
        # register cycle. Idempotent: if _try_register_consent returns a fresh
        # readToken, it overwrites this value; if the server returns none, the
        # persisted credential is preserved.
        read_token=existing.read_token if existing else None,
        invite_key=chosen_invite_key,
        # Sticky opt-in: once turned on it stays on across restarts. Passing
        # ``share_meter_with_ai=False`` explicitly turns it back off; omitting
        # it (None) leaves the previously persisted choice untouched.
        share_meter_with_ai=(
            bool(share_meter_with_ai)
            if share_meter_with_ai is not None
            else (existing.share_meter_with_ai if existing else False)
        ),
        server_share=existing.server_share if existing else False,
        server_full=existing.server_full if existing else False,
        # Display name precedence, friction-first: an explicit name passed to
        # THIS call wins, else the previously persisted name, else a best-effort
        # auto-detected name (entry-script stem / project folder) so the
        # dashboard shows a friendly name instead of a churny install id with no
        # wiring. Sticky: once a name is stored, later runs keep it (detection
        # only fills the gap). Clamped to APP_NAME_MAX; a guard-tripping
        # auto-detected name is dropped so detection can never break consent.
        app_name=_resolve_app_name(
            app_name if app_name is not None else (existing.app_name if existing else None)
        ),
        project_identity_json=(
            existing.project_identity_json if existing else None
        ),
        project_promises_json=(
            existing.project_promises_json if existing else None
        ),
    )
    _write_config(cfg)
    # Install the server-authority kill-switch heartbeat (VAULT contract). This
    # is the on-launch enforcement edge: start_entitlement_checkin hydrates the
    # cached last-good answer, runs a FORCED launch check-in (bypassing any
    # cache-satisfied fast path), then polls on a 6h interval. ``get_token`` is
    # lazy so the delete/read token minted by consent below is picked up on the
    # next check. Installed BEFORE consent so a cached kill takes effect
    # immediately; consent success fires a fresh forced check to adopt any
    # newly-minted token.
    _install_checkin_config(cfg.endpoint, cfg.install_id)
    # Register with the server. Best-effort — local state is still saved
    # even if the network call fails so the user can retry later. We only
    # learn the delete_token (and read_token + server share directive) from a
    # successful server response.
    _try_register_consent(cfg, force=True)
    # Self-register the auto-submitter so `ingest_findings` will upload
    # any recurring signature as soon as it crosses the local threshold.
    # No host wiring needed; opting out (disable/forget) clears it again.
    set_candidate_submitter(_submit_candidates)
    # ...and the other half of the story: when one of those problems is later
    # seen at a better severity, somebody fixed it, and a fix nobody hears about
    # teaches nothing. Same always-on doctrine, same credential, same gates —
    # see docs/kit-problem-reporting-contract.md.
    set_resolution_submitter(_submit_resolutions)
    # Crash reporting is ALWAYS-ON for a registered app (same model as the
    # candidate channel): register the submitter unconditionally, then install
    # the chained excepthook/threading.excepthook. Installing restores + flushes
    # any crashes persisted from a previous (possibly fatal) session.
    #
    # PYTHON-SPECIFIC DEVIATION vs RN: under pytest we register the submitter but
    # do NOT replace the interpreter's global sys.excepthook/threading.excepthook,
    # so the test runner's own crash reporting is never hijacked for the rest of
    # the session (mirrors the PYTEST guard snapshot_mirror already uses for its
    # daemon-thread flush). Production installs the hooks normally.
    crash_reporter.set_crash_submitter(_submit_crashes)
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        crash_reporter.install_crash_handlers()
    # Background-job runs ride the same always-on doctrine as crash reporting,
    # NOT the snapshot share gate: a run carries only a code-defined job name,
    # and a developer must be told a nightly job stopped whether or not they
    # ever connected an AI. Only forget() or the kill-switch stop it.
    job_reporter.set_job_run_submitter(_transmit_job_runs)
    # Wire (or clear) the snapshot mirror per the current share policy. Fires
    # one immediate flush on the off→on rising edge so the AI has data to read
    # the moment sharing is enabled. Re-read from disk: _try_register_consent
    # may have latched the server directive.
    _sync_snapshot_submitter(_read_config() or cfg)
    return cfg


def set_invite_key(invite_key: str) -> TelemetryConfig:
    """Persist a project key WITHOUT enabling telemetry or contacting the
    server.

    Lets ``boosthis init --project-key <key>`` wire the key into
    ``~/.boosthis/config.json`` ahead of time, so a later
    ``enable_telemetry()`` (or the always-on issue channel after the app
    registers) presents it. If no config exists yet a minimal *disabled*
    config is created with a fresh install id; telemetry stays off until the
    user explicitly opts in.
    """
    existing = _read_config()
    if existing is not None:
        existing.invite_key = invite_key
        _write_config(existing)
        return existing
    cfg = TelemetryConfig(
        install_id=str(uuid.uuid4()),
        enabled=False,
        endpoint=os.environ.get("BOOSTHIS_INGEST_URL")
        or os.environ.get("BOOSTEN_INGEST_URL")
        or DEFAULT_ENDPOINT,
        consent_at=None,
        delete_token=None,
        invite_key=invite_key,
    )
    _write_config(cfg)
    return cfg


def _submit_candidates(signatures: list[dict[str, Any]]) -> int:
    """Auto-submit privacy-safe rule-candidate signatures. Called by
    ``boosthis.candidates.ingest_findings`` whenever a new signature
    crosses the local recurrence threshold.

    Always-on for registered apps: issue reporting is NOT gated by the
    user-facing enable/disable toggle. Once the app has registered (and
    therefore holds a ``delete_token``), candidates report automatically.
    Only the emergency kill-switch (``BOOSTHIS_DISABLED``) or erasure
    (``forget()``, which clears the token) can stop it. Returns the number
    accepted by the server, or 0 if disabled / no token / network fails.
    """
    cfg = _read_config()
    if cfg is None or not signatures:
        return 0
    if is_boosthis_disabled():
        return 0
    if not cfg.delete_token:
        _try_register_consent(cfg)
        cfg = _read_config() or cfg
    # Race re-check: forget() may have cleared the token while the consent
    # retry was in flight. Re-read disk so we always honor the latest
    # state before any bytes leave the process. (disable_telemetry() no
    # longer stops issues — they are always-on for registered apps.)
    cfg = _read_config() or cfg
    if cfg is None or not cfg.delete_token:
        return 0
    batch = signatures[:50]
    payload = {
        "installId": cfg.install_id,
        "packageVersion": RUNTIME_VERSION,
        "signatures": batch,
    }
    assert_no_pii(payload)
    try:
        status, body = _safe_transmit_with_response_internal(
            f"{cfg.endpoint}/candidates",
            payload,
            auth_header=f"Bearer {cfg.delete_token}",
        )
    except Exception as exc:  # noqa: BLE001
        _note_upload_exception(exc)
        return 0
    if 200 <= status < 300:
        note_upload_accepted(body)
        return len(batch)
    _note_upload_response(status)
    return 0


def _submit_resolutions(resolutions: list[dict[str, Any]]) -> int:
    """Auto-submit privacy-safe FIX OUTCOMES — "this problem got better".
    Called by ``boosthis.candidates.ingest_findings`` when a kind is observed at
    a better severity than the worst ever recorded for it.

    Same doctrine as :func:`_submit_candidates`, and for the same reason: a
    problem we reported and a fix we never heard about teach only half a lesson.
    Always-on for registered apps, NOT gated by the enable/disable toggle; only
    the kill-switch (``BOOSTHIS_DISABLED``) or erasure (``forget()``, which
    clears the token) can stop it.

    The payload carries the rule kind plus the bucketed before -> after rating
    and the circumstances the problem held BEFORE the fix. No code, no diff, no
    route name, no raw value. Returns the number the server accepted (the caller
    only lowers a baseline for what was taken), or 0 if disabled / no token /
    the network fails. Never raises.
    """
    cfg = _read_config()
    if cfg is None or not resolutions:
        return 0
    if is_boosthis_disabled():
        return 0
    if not cfg.delete_token:
        _try_register_consent(cfg)
        cfg = _read_config() or cfg
    # Race re-check: forget() may have cleared the token while the consent
    # retry was in flight. Re-read disk before any bytes leave the process.
    cfg = _read_config() or cfg
    if cfg is None or not cfg.delete_token:
        return 0
    batch = resolutions[:50]
    payload = {
        "installId": cfg.install_id,
        "packageVersion": RUNTIME_VERSION,
        "resolutions": batch,
    }
    assert_no_pii(payload)
    try:
        status, body = _safe_transmit_with_response_internal(
            f"{cfg.endpoint}/resolutions",
            payload,
            auth_header=f"Bearer {cfg.delete_token}",
        )
    except Exception as exc:  # noqa: BLE001
        _note_upload_exception(exc)
        return 0
    if 200 <= status < 300:
        note_upload_accepted(body)
        return len(batch)
    _note_upload_response(status)
    return 0


def _submit_crashes(crashes: list[dict[str, Any]]) -> int:
    """Crash auto-submitter: ship a batch of privacy-safe crash fingerprints to
    ``POST /crashes``, authenticated with the per-install DELETE token (Bearer)
    — the same credential the snapshot/candidate uploads use. Registered by
    :func:`enable_telemetry` via ``crash_reporter.set_crash_submitter``.

    Always-on for registered apps: like the issue/candidate channel, crash
    reporting is NOT gated by the user-facing enable/disable toggle. Once the app
    has registered (holds a ``delete_token``) crashes report automatically. Only
    the emergency kill-switch (``BOOSTHIS_DISABLED``) or erasure (``forget()``,
    which clears the token + wipes the pending store) can stop it.

    Returns the number the server accepted (so the crash reporter can subtract
    only what was sent), or 0 if disabled / no token / network fails. Never
    raises — instrumentation stays silent.
    """
    cfg = _read_config()
    if cfg is None or not crashes:
        return 0
    if is_boosthis_disabled():
        return 0
    if not cfg.delete_token:
        _try_register_consent(cfg)
        cfg = _read_config() or cfg
    # Race re-check: forget() may have cleared the token while the consent retry
    # was in flight. Re-read disk before any bytes leave the process.
    cfg = _read_config() or cfg
    if cfg is None or not cfg.delete_token:
        return 0
    batch = crashes[:50]
    payload = {
        "installId": cfg.install_id,
        "packageVersion": RUNTIME_VERSION,
        "crashes": batch,
    }
    # The whole envelope must clear the PII guard before any bytes leave the
    # process (the server re-runs the same guard on ingest). The crash shape is
    # code-derived + redacted, but a poisoned detailed payload must be dropped
    # rather than uploaded — assert_no_pii raises, the caller swallows it, and
    # the batch is NOT sent (returns 0), so nothing leaks.
    assert_no_pii(payload)
    try:
        status, body = _safe_transmit_with_response_internal(
            f"{cfg.endpoint}/crashes",
            payload,
            auth_header=f"Bearer {cfg.delete_token}",
        )
    except Exception as exc:  # noqa: BLE001
        _note_upload_exception(exc)
        return 0
    if 200 <= status < 300:
        note_upload_accepted(body)
        return len(batch)
    _note_upload_response(status)
    return 0


def _effective_share(cfg: TelemetryConfig | None) -> bool:
    """Whether the perf snapshot may be mirrored to the server right now.

    True only for an ENABLED, non-killed install whose developer has opted in
    (``share_meter_with_ai``) OR whose server reported the ``shareMeterWithAI``
    directive (``server_share``). Mirrors Node's ``effectiveSnapshotUploadAllowed``
    plus the enabled/kill-switch gate.

    ONE gate governs every screen-bearing auto-upload: the snapshot mirror, the
    trace spans, and the per-request sample batches. They carry the same class
    of data (code-defined route label + duration + rating bucket), so they may
    never diverge on when they are allowed to ship. Privacy by default: it stays
    False until the developer opts in or actually connects an AI from the web
    dashboard.
    """
    return bool(
        cfg
        and cfg.enabled
        and not is_boosthis_disabled()
        and (cfg.share_meter_with_ai or cfg.server_share or cfg.server_full)
    )


def _sync_snapshot_submitter(cfg: TelemetryConfig | None) -> None:
    """Register or clear the snapshot submitter to match the current share
    policy. Fires ONE immediate flush on the off→on rising edge (submitter newly
    registered) so the developer's AI has data to read the instant sharing turns
    on. Clearing the submitter (sharing off / disabled / forgotten) also blocks
    any queued manual flush, so a snapshot can never leak after opt-out."""
    if _effective_share(cfg):
        had = snapshot_mirror.has_submitter()
        snapshot_mirror.set_snapshot_submitter(_transmit_snapshot)
        # Trace spans carry code-defined route labels too, so they ride the EXACT
        # same allow-gate as the snapshot mirror: nothing span-shaped is even
        # buffered until sharing is authorized (see span_emitter.enqueue_span).
        span_emitter.set_span_submitter(_transmit_spans)
        # Per-request samples carry the same code-defined route label, duration
        # and rating a span already carries, so they ride the EXACT same
        # allow-gate: nothing is buffered for upload until sharing is authorized
        # (see sample_uploader.enqueue_sample).
        sample_uploader.set_sample_submitter(_transmit_sample_batch)
        # There is now something that can be LOST, so arm the way out. A
        # measurement is buffered until the next one arrives fifteen seconds
        # later, which a worker, a scheduled script or a container being
        # recycled never reaches — without this the whole queue leaves with the
        # process. Idempotent, and called again from enable_telemetry() because
        # the signal half needs the main thread and this can run on any thread.
        exit_flush.install_exit_flush()
        if not had:
            snapshot_mirror.flush_snapshot_now()
    else:
        snapshot_mirror.set_snapshot_submitter(None)
        # Clearing the span submitter makes enqueue_span inert immediately, so no
        # span can leak after opt-out/disable.
        span_emitter.set_span_submitter(None)
        # Same for per-request samples: unwire the submitter AND drop whatever
        # was already buffered, so a measurement taken while sharing was on can
        # never ship after the developer turned it off.
        sample_uploader.set_sample_submitter(None)
        sample_uploader.clear_buffered_samples()


def _transmit_snapshot(snapshot: dict[str, Any]) -> int:
    """Snapshot submitter: ship one privacy-safe perf snapshot to
    ``POST /api/snapshots``, authenticated with the per-install DELETE token
    (Bearer) — the read token is the credential the developer hands their AI,
    not the upload credential. Mirrors Node's ``transmitSnapshot``. Returns 1 on
    success else 0, and never raises (instrumentation stays silent).
    """
    cfg = _read_config()
    # Re-check the share policy against the on-disk source of truth: disable(),
    # forget(), or the kill-switch may have flipped while a flush was queued.
    if not _effective_share(cfg):
        return 0
    assert cfg is not None  # guaranteed by _effective_share
    # Lazy consent — retry if we never captured a delete_token (e.g. the network
    # was down at enable() time).
    if not cfg.delete_token:
        _try_register_consent(cfg)
        cfg = _read_config() or cfg
    # Race re-check after the (possibly slow) consent round-trip.
    if not _effective_share(cfg) or cfg is None or not cfg.delete_token:
        return 0
    # Filter every label field before the PII guard. Route-row keys and
    # cross-cutting finding names are code-defined identifiers, but a caller
    # could pass a user-derived string — drop any entry whose label carries a
    # PII pattern. Mirrors Node's transmitSnapshot label filter (same guard the
    # sample path uses: check_route_label returns non-None to drop).
    raw = dict(snapshot)
    rows = raw.get("rows")
    if isinstance(rows, list):
        raw["rows"] = [
            r
            for r in rows
            if isinstance(r, dict)
            and check_route_label(str(r.get("key", ""))) is None
        ]
    cross = raw.get("crossCutting")
    if isinstance(cross, list):
        kept = [
            f
            for f in cross
            if isinstance(f, dict)
            and check_route_label(str(f.get("name", ""))) is None
        ]
        # ...and one label the PII guard cannot judge: the retry-storm finding
        # names the outbound HOST it saw hammered.  Right on the developer's own
        # screen, wrong on the wire -- "hostnames" sit in our published list of
        # what never reaches us.  So the address is replaced by the closed
        # dependency-kind vocabulary ("outbound:payments"), keeping the finding
        # useful without the address leaving the app.  Mirrors Node.
        redacted: list[Any] = []
        for f in kept:
            name = str(f.get("name", ""))
            if looks_like_address(name):
                f = {**f, "name": f"outbound:{dependency_kind_for(name)}"}
            redacted.append(f)
        raw["crossCutting"] = redacted
    payload = {
        "installId": cfg.install_id,
        "packageVersion": RUNTIME_VERSION,
        "capturedAt": raw.get("capturedAt"),
        "snapshot": raw,
    }
    # Defence in depth: the whole envelope must clear the PII guard before any
    # bytes leave the process (the server re-runs the same guard on ingest).
    assert_no_pii(payload)
    try:
        status, body = _safe_transmit_with_response_internal(
            f"{cfg.endpoint}/snapshots",
            payload,
            auth_header=f"Bearer {cfg.delete_token}",
        )
    except Exception as exc:  # noqa: BLE001
        _note_upload_exception(exc)
        return 0
    if 200 <= status < 300:
        note_upload_accepted(body)
        return 1
    _note_upload_response(status)
    return 0


def _transmit_spans(spans: list[dict[str, Any]]) -> int:
    """Span submitter: ship a batch of privacy-safe full-stack-trace spans to
    ``POST /api/spans``, authenticated with the per-install DELETE token (Bearer)
    — the same credential the snapshot/candidate/crash uploads use. Mirrors
    Node's ``transmitSpans`` (byte-identical gate + filter + wire). Returns the
    number the server accepted, or 0 if not shareable / disabled / no token /
    network fails. Never raises (instrumentation stays silent).
    """
    cfg = _read_config()
    # Spans carry code-defined route labels, so they ride the SAME share gate as
    # the snapshot mirror: off in issues-only mode unless the developer opted in
    # (share_meter_with_ai) OR the server directive fired. Always off when
    # disabled or killed.
    if not _effective_share(cfg):
        return 0
    assert cfg is not None  # guaranteed by _effective_share
    if is_boosthis_disabled() or not spans:
        return 0
    # Lazy consent — retry if we never captured a delete_token.
    if not cfg.delete_token:
        _try_register_consent(cfg)
        cfg = _read_config() or cfg
    # Race re-check after the (possibly slow) consent round-trip.
    if not _effective_share(cfg) or cfg is None or not cfg.delete_token:
        return 0
    # Drop any span whose label carries a PII pattern. The labels are the
    # already-guarded @track_perf/perf names, but a caller could pass a
    # user-derived string — use check_no_pii (the general guard, no route-label
    # space heuristic) so a canonical "GET /path"-shaped label passes while
    # value-level PII (email/JWT/etc.) is dropped, byte-identical to Node.
    safe = [
        s
        for s in spans
        if check_no_pii(str(s.get("routeLabel", ""))) is None
    ]
    batch = safe[: span_emitter.MAX_SPAN_BATCH]
    if not batch:
        return 0
    payload = {
        "installId": cfg.install_id,
        "packageVersion": RUNTIME_VERSION,
        "spans": batch,
    }
    # Defence in depth — the per-label filter already dropped any PII-bearing
    # span, but run the whole-payload guard before any bytes leave the process
    # (the server re-runs the same guard on ingest).
    assert_no_pii(payload)
    try:
        status, body = _safe_transmit_with_response_internal(
            f"{cfg.endpoint}/spans",
            payload,
            auth_header=f"Bearer {cfg.delete_token}",
        )
    except Exception as exc:  # noqa: BLE001
        _note_upload_exception(exc)
        return 0
    if status == LOCKED_NOT_SENT:
        # Never reached the network — see the same branch in transmit_samples.
        return NOT_SENT
    if 200 <= status < 300:
        note_upload_accepted(body)
        return len(batch)
    _note_upload_response(status)
    return 0


def _transmit_sample_batch(batch: list[dict[str, Any]]) -> int:
    """Sample submitter: ship a batch of per-request measurements to
    ``POST /api/samples``, authenticated with the per-install DELETE token
    (Bearer) — the same credential every other upload uses. Mirrors Node's
    sample queue drain.

    Per-request samples are what fill the dashboard's per-runtime measurement
    count and the project card's busiest routes; without them a registered
    Python back end shows a permanent zero there. They carry a strict subset of
    what a span already carries, so they ride the SAME share gate as the
    snapshot mirror and the span emitter — off in issues-only mode unless the
    developer opted in (``share_meter_with_ai``) or the server directive fired,
    and always off when disabled or killed. The wire work (PII re-check, closed
    metadata schema, payload guard) lives in :func:`transmit_samples`. Returns
    the number the server accepted, or 0. Never raises.
    """
    cfg = _read_config()
    if not _effective_share(cfg):
        return 0
    if is_boosthis_disabled() or not batch:
        return 0
    return transmit_samples(batch[: sample_uploader.MAX_SAMPLE_BATCH])


def _transmit_job_runs(
    runs: list[dict[str, Any]],
    expectations: list[dict[str, Any]] | None = None,
    promises: list[dict[str, Any]] | None = None,
) -> Any:
    """Job-run submitter: ship a batch of finished background-job runs to
    ``POST /api/job-runs``, authenticated with the per-install DELETE token
    (Bearer) — the same credential every other upload uses. Mirrors Node's
    ``transmit_job_runs``. Never raises (instrumentation stays silent).

    NOT behind the snapshot/AI share gate, on purpose: a run carries a
    code-defined job name and three numbers, the same class of label a route
    sample already sends in every mode, and the whole point of the feature is a
    warning that cannot be contingent on having connected an AI.

    The same call carries any rhythm the app's own code declared through
    ``expect_every()``. Same route, same credential, same round trip: the
    declaration is about these jobs, and a second endpoint would be a second
    thing to fail. A declaration alone is reason enough to make the call — the
    job most worth watching is the one that has stopped reporting runs.

    A PROMISE the app's own code declared rides here too, for the same
    reasons and through the same door.

    Returns the accepted count, or ``{"accepted", "expectations", "promises"}``
    when the server answered about declarations, so the reporter can tell
    "kept" from "never arrived".
    """
    declared = expectations or []
    stated = promises or []
    cfg = _read_config()
    if cfg is None or is_boosthis_disabled() or (not runs and not declared and not stated):
        return 0
    # Lazy consent — retry if we never captured a delete_token.
    if not cfg.delete_token:
        _try_register_consent(cfg)
        cfg = _read_config() or cfg
    if cfg is None or is_boosthis_disabled() or not cfg.delete_token:
        return 0
    # Same label guard as every other upload. The emitter already screened each
    # name; this is the second pass, at the edge.
    safe = [r for r in runs if check_no_pii(str(r.get("job", ""))) is None]
    batch = safe[: job_reporter.MAX_JOB_RUN_BATCH]
    safe_declared = [
        d for d in declared if check_no_pii(str(d.get("job", ""))) is None
    ][: job_reporter.MAX_JOB_EXPECTATIONS]
    # A promise's SUBJECT LABEL rides the same guard for the same reason — it
    # is a reporting label. Its WORDING deliberately does not: that is a
    # sentence a human wrote, and the transmit guard would refuse the whole
    # upload over a developer's own email address in it, losing the job runs
    # travelling in the same body. The server screens the wording by itself,
    # refuses that one declaration by name, and says why.
    safe_stated = [
        p
        for p in stated
        if not p.get("subjectLabel")
        or check_no_pii(str(p.get("subjectLabel", ""))) is None
    ][: promise_declaration.MAX_PROMISE_DECLARATIONS]
    if not batch and not safe_declared and not safe_stated:
        return 0
    payload: dict[str, Any] = {
        "installId": cfg.install_id,
        "packageVersion": RUNTIME_VERSION,
        "runs": batch,
    }
    if safe_declared:
        payload["expectations"] = safe_declared
    # Screened WITHOUT the wordings, exactly as the server screens the same
    # body: the guard is for machine-shaped fields, and a sentence a human
    # wrote is the server's job to screen.
    assert_no_pii(
        {
            **payload,
            **(
                {
                    "promises": [
                        {k: v for k, v in p.items() if k != "wording"}
                        for p in safe_stated
                    ]
                }
                if safe_stated
                else {}
            ),
        }
    )
    if safe_stated:
        payload["promises"] = safe_stated
    try:
        status, body = _safe_transmit_with_response_internal(
            f"{cfg.endpoint}/job-runs",
            payload,
            auth_header=f"Bearer {cfg.delete_token}",
        )
    except Exception as exc:  # noqa: BLE001
        _note_upload_exception(exc)
        return 0
    if 200 <= status < 300:
        note_upload_accepted(body)
        if safe_declared or safe_stated:
            # ALWAYS the mapping form once the upload itself succeeded, even
            # when the reply said nothing about the declarations. The mapping
            # is how the reporter knows the batch REACHED Boosthis: a bare
            # count cannot separate "delivered, and the server never mentioned
            # the rhythm" from "refused, nothing arrived at all", and telling a
            # developer their server is out of date when their upload was
            # actually turned away sends them to fix the wrong thing.
            outcomes = job_reporter.parse_expectation_outcomes(body)
            answer: dict[str, Any] = {"accepted": len(batch)}
            if outcomes is not None:
                answer["expectations"] = outcomes
            promise_outcomes = (
                promise_declaration.parse_promise_declaration_outcomes(body)
            )
            if promise_outcomes is not None:
                answer["promises"] = promise_outcomes
            return answer
        return len(batch)
    _note_upload_response(status)
    return 0


def _post_forget_raw(url: str, payload: dict[str, Any]) -> None:
    """Raw POST that bypasses ``safe_transmit``'s PII guard.

    Only used by :func:`forget` because the GDPR erasure payload carries
    the server-issued ``deleteToken`` field, whose tokenized form
    contains the denylisted fragment ``token``. The payload shape is
    fixed and contains only the install_id + the token we're returning,
    no user data — so the bypass is safe.

    Extracted to a module-level helper so tests can monkeypatch it.
    """
    try:
        import json
        import urllib.request
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5.0) as _resp:
            _resp.read()
    except Exception:  # noqa: BLE001
        pass


# One-shot guard so a registration that did not go through prints exactly ONE
# line per process — never on every consent retry. The Python runtime has no
# locked-screen UI (unlike RN), so this log line is the parity hint that tells
# the developer WHY Boosthis stayed inactive. It never carries the invite key,
# any endpoint, or any server text — only a coarse, code-defined reason derived
# from the HTTP status + the frozen error/orphan markers.
#
# Silence is the wrong answer here and this is the one place the kit says so out
# loud. A kit that cannot register looks identical to a kit that is working —
# same silent process, same absent numbers — so the developer goes looking in
# the wrong place, usually for hours. It is NOT gated on any debug flag: a debug
# flag only helps the developer who already suspects the kit, and this exists
# for the one who does not. One line, once, is the whole budget.
_warned_registration_rejected = False
_inactive_notice: str | None = None


def _classify_transport_error(err: object) -> str:
    """Turn a caught transport exception into a FIXED, kit-owned parenthetical
    (with its leading space), e.g. ``" (the certificate store rejected the
    connection)"``.

    The contract for the one-shot line is kit-generated text ONLY — never a key,
    token, URL, or server/client prose. A TLS/proxy/HTTP client error message is
    NOT kit-generated: it can embed the request URL, a credential-bearing URL, or
    remote-supplied text. So the error's message and type name are read for
    MATCHING ONLY; the message is never emitted, in whole or in part. On no
    match we emit the sanitised TYPE NAME alone (step 3). Guest-safe: never
    throws, never allocates unboundedly, never touches the network."""
    try:
        # Each input is capped BEFORE it is copied or case-converted, so a
        # pathological message can never drive unbounded work in a host process.
        message = ""
        try:
            message = (str(err) or "")[:512]
        except Exception:  # noqa: BLE001
            message = ""
        type_name = ""
        try:
            type_name = (type(err).__name__ or "")[:128]
        except Exception:  # noqa: BLE001
            type_name = ""
        hay = f"{message}\n{type_name}".lower()

        # First match wins — coarse, code-defined transport classes.
        if any(
            needle in hay
            for needle in (
                "certificate",
                "tls",
                "ssl",
                "x509",
                "self signed",
                "self-signed",
                "unable to verify",
                "certificate_verify_failed",
                "sslhandshake",
                "trust anchor",
                "unable to get local issuer",
            )
        ):
            return " (the certificate store rejected the connection)"
        if any(
            needle in hay
            for needle in (
                "getaddrinfo",
                "name or service not known",
                "enotfound",
                "unknownhost",
                "nodename nor servname",
                "no such host",
                "failed to resolve",
                "dns",
            )
        ):
            return " (the API host name did not resolve)"
        if any(needle in hay for needle in ("econnrefused", "connection refused")):
            return " (the connection was refused)"
        if any(needle in hay for needle in ("etimedout", "timeout", "timed out")):
            return " (the connection timed out)"
        if "proxy" in hay:
            return " (a proxy rejected the connection)"
        if any(
            needle in hay
            for needle in ("enetunreach", "network is unreachable", "no route to host")
        ):
            return " (the network was unreachable)"

        # No match: the TYPE NAME only (never the message), sanitised to
        # [A-Za-z0-9_.] and capped at 60 chars. Empty -> no parenthetical.
        safe = "".join(c for c in type_name if c.isascii() and (c.isalnum() or c in "_."))[:60]
        if not safe:
            return ""
        return f" ({safe})"
    except Exception:  # noqa: BLE001
        return ""


def _registration_failure_hint(
    status: int,
    body: dict[str, Any] | None,
    error_detail: str | None,
) -> tuple[str, str]:
    """``(why, what-to-do)`` for the one-shot line. Derived from the HTTP status
    and the frozen error/orphan markers only — never from server prose. Mirrors
    the Ruby kit's ``registration_failure_hint`` case-for-case."""
    marker = body.get("error") if isinstance(body, dict) else None
    orphan = isinstance(body, dict) and body.get("status") == "already_registered_no_token"

    if isinstance(body, dict) and body.get("no_key") == "chosen":
        return ("this app is set to run with no project key", "That is a deliberate setting: this app measures itself and never appears on your Boosthis dashboard. Nothing is being sent.")
    if isinstance(body, dict) and body.get("no_key") == "missing":
        return ("no project key is wired into this app", "Without one this app measures itself and never appears on your Boosthis dashboard. Copy a project key from your Boosthis Setup page into this app, then restart.")
    if status == 200 and orphan:
        # The one case that reads like success on the wire: HTTP 200, and the
        # install still cannot upload. Name it plainly.
        return (
            "this install id already belongs to another copy of the app, and this copy holds none of its credentials",
            "Unset BOOSTHIS_INSTALL_ID (or give this copy its own fresh UUID) and restart.",
        )
    if status == 0:
        # No HTTP answer at all: DNS, TLS, egress or a proxy. We name the coarse
        # transport class so a broken certificate store or a blocked egress is
        # not left unexplained — the 19-minute case. ``error_detail`` here is a
        # FIXED, kit-owned parenthetical (already carrying its leading space)
        # produced by ``_classify_transport_error`` — NEVER the raw client/OS
        # error message, which can embed a URL or a credential-bearing URL.
        detail = error_detail or ""
        return (
            f"it could not reach the Boosthis API from this process{detail}",
            "Check outbound HTTPS and certificate trust from this app, then "
            "restart.",
        )
    if status in (401, 403):
        if marker == "invite_key_revoked":
            return ("its project key has been revoked", "A revoked key never works again. Mint a new project key in your Boosthis dashboard, put it in this app, then restart.")
        if marker in ("plan_required", "account_closure_pending"):
            return ("its project key is paused by a billing problem on the account", "Settle the account in your Boosthis dashboard and the same key starts working again. Nothing has been revoked.")
        if marker == "invite_key_unknown":
            return ("its project key was not recognised", "Check it is the current key, pasted whole, with no stray spaces, and that it belongs to this project.")
        return ("its project key wasn't accepted", "Check it is the current key, pasted whole, with no stray spaces, and that it belongs to this project.")
    return (
        f"the server refused the registration (HTTP {status})",
        "Retry later; if it keeps happening, check the project key in your "
        "Boosthis dashboard.",
    )


def _warn_stayed_inactive_once(
    status: int,
    body: dict[str, Any] | None = None,
    error_detail: str | None = None,
) -> None:
    """Emit the one-shot ``[boosthis] Boosthis stayed inactive because …`` line
    on the ``boosthis`` logger (whose default handler is stderr). Fires at most
    once per process; carries no key, token, URL or server prose."""
    global _warned_registration_rejected, _inactive_notice
    if _warned_registration_rejected:
        return
    _warned_registration_rejected = True
    why, fix = _registration_failure_hint(status, body, error_detail)
    _inactive_notice = f"Boosthis stayed inactive because {why}. {fix}"
    logging.getLogger("boosthis").warning("[boosthis] %s", _inactive_notice)


def get_inactive_notice() -> str | None:
    return _inactive_notice


def _warn_registration_rejected_once(err: urllib.error.HTTPError) -> None:
    """Key-rejection entry point (401/403). Reads the body once to spot the
    frozen ``invite_key_revoked`` marker, then routes through the one-shot
    emitter. Kept as the named seam for the key-rejection call site and its
    tests."""
    body: dict[str, Any] | None = None
    try:
        raw = err.read()
        parsed = json.loads(raw) if raw else {}
        if isinstance(parsed, dict):
            body = parsed
    except Exception:  # noqa: BLE001
        pass  # Non-JSON body — fall back to the generic "not accepted" wording.
    _warn_stayed_inactive_once(err.code, body)


def _reset_registration_warning_for_tests() -> None:
    """Test-only: reset the one-shot registration-warning guard."""
    global _warned_registration_rejected, _inactive_notice
    _warned_registration_rejected = False
    _inactive_notice = None


# ── Invalid install-id rejection (HTTP 400 ``invalid_install_id``) ───────────
# The server refuses a consent whose install id is not a UUID v4. This kit mints
# and persists a real ``uuid4()`` itself, so the rejection is a BACKSTOP for a
# hand-edited ``~/.boosthis/config.json`` (or a host that seeded a made-up
# "stable id" string). Before this, that case looked like "consent recorded, no
# errors" and the developer concluded Boosthis was broken.
#
# Three things happen, all local:
#   • ONE fixed, code-defined warning line per process (never the server's text,
#     never the install id, never any request/response value);
#   • a rejected STATE the local dashboard renders as a card;
#   • NO retry — re-sending the SAME bad id can never succeed, so lazy consent
#     retries are parked for the rest of the process. The hourly retry cadence
#     stays reserved for key rejection (401/403).
_warned_invalid_install_id = False
_registration_rejected_invalid_install_id = False


def _is_invalid_install_id_error(err: urllib.error.HTTPError) -> bool:
    """True when the 400 body carries the fixed ``invalid_install_id`` marker.

    Reads the body at most once and only ever compares it to a code-defined
    constant — no server text is retained, logged, or displayed. Any parse
    failure is treated as "not this case" (fail-safe)."""
    try:
        raw = err.read()
        parsed = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return False
    return isinstance(parsed, dict) and parsed.get("error") == "invalid_install_id"


def _note_invalid_install_id_rejection() -> None:
    """Latch the rejected state and emit the ONE-TIME, fixed warning."""
    global _warned_invalid_install_id, _registration_rejected_invalid_install_id
    _registration_rejected_invalid_install_id = True
    if _warned_invalid_install_id:
        return
    _warned_invalid_install_id = True
    logging.getLogger("boosthis").warning(
        "[boosthis] Registration rejected: this app's Boosthis install ID is "
        "not a valid UUID. Generate a real UUID (e.g. uuidgen / "
        "crypto.randomUUID()), persist it as the install ID, then restart."
    )


def registration_rejected_reason() -> str | None:
    """Coarse, code-defined registration-rejection marker for the local
    developer surface (the mounted dashboard's ``/api/context`` read).

    Returns ``"invalid_install_id"`` or ``None``. It is an enum this module
    defines — never a server- or developer-provided string — so a consumer can
    only ever render fixed, code-defined copy from it."""
    return (
        "invalid_install_id" if _registration_rejected_invalid_install_id else None
    )


def _clear_invalid_install_id_state() -> None:
    """Drop the rejected state + its one-shot warning latch. Called when a
    registration actually succeeds (e.g. after an identity rotation minted a
    fresh uuid4) and on erasure; also used by tests."""
    global _warned_invalid_install_id, _registration_rejected_invalid_install_id
    _warned_invalid_install_id = False
    _registration_rejected_invalid_install_id = False


# Epoch-ms of the last MEASUREMENT upload the server accepted, or None when
# this process has never had one accepted.
#
# Declared at module scope on purpose: the status page reads it precisely in
# the case where nothing has been uploaded yet, and a name that only comes into
# existence on the first successful upload would raise instead of answering
# "never".
_last_upload_at_ms: float | None = None
_last_upload_fail_at_ms: float | None = None
_last_upload_fail_reason: str | None = None
_dropped_uploads = 0
_last_upload_attempt_failed = False


def note_upload_rejected(reason: str) -> None:
    """Record one measurement batch that did not reach storage. Never raises."""
    global _last_upload_fail_at_ms, _last_upload_fail_reason
    global _dropped_uploads, _last_upload_attempt_failed
    try:
        if reason not in ("unauthorized", "rejected", "server-error", "unreachable"):
            return
        _last_upload_fail_at_ms = time.time() * 1000.0
        _last_upload_fail_reason = reason
        _dropped_uploads += 1
        _last_upload_attempt_failed = True
    except Exception:  # noqa: BLE001
        pass


def _note_upload_response(status: int) -> None:
    try:
        reason = "unauthorized" if status in (401, 403) else "server-error" if status >= 500 else "rejected"
        note_upload_rejected(reason)
    except Exception:  # noqa: BLE001
        pass


def _note_upload_exception(exc: Exception) -> None:
    try:
        from urllib.error import HTTPError
        if isinstance(exc, HTTPError):
            _note_upload_response(int(exc.code))
        else:
            note_upload_rejected("unreachable")
    except Exception:  # noqa: BLE001
        note_upload_rejected("unreachable")


def note_upload_accepted(body: Any = None) -> None:
    """Stamp "the server just accepted a measurement upload". Called ONLY where
    a MEASUREMENT upload (samples/candidates/snapshots/spans/crashes) returned
    2xx — never on consent/registration or erasure. Never raises: a clock read
    must not destabilize an upload path.

    A 2xx is not unqualified success: the server may have stored fewer rows than
    we sent. ``body`` is the reply body already in hand (never re-fetched, never
    awaited), so reading its honesty fields adds no request-path work and a
    reply without the fields reports nothing. Guarded independently of the
    clock stamp so neither can take the other down."""
    global _last_upload_at_ms, _last_upload_attempt_failed
    try:
        _last_upload_at_ms = time.time() * 1000.0
        _last_upload_attempt_failed = False
    except Exception:  # noqa: BLE001
        pass
    try:
        note_server_drops(body)
    except Exception:  # noqa: BLE001
        pass
def _install_checkin_config(endpoint: str, install_id: str) -> None:
    """(Re)install the server-authority kill-switch heartbeat for the current
    identity. ``get_token`` is lazy — it re-reads the on-disk config on every
    check-in so the delete/read token minted by consent (after enable runs) is
    picked up on the next knock, and ``forget()`` clearing the token
    immediately re-locks the heartbeat. This is the VAULT contract's on-launch
    enforcement edge: the first check runs a FORCED launch check-in that
    bypasses any cache-satisfied fast path."""

    def _get_token() -> str | None:
        cur = _read_config()
        if cur is None:
            return None
        return cur.delete_token or cur.read_token

    start_entitlement_checkin(
        EntitlementCheckinConfig(
            endpoint=endpoint,
            install_id=install_id,
            get_token=_get_token,
            kit_version=RUNTIME_VERSION,
            integrity=None,
        )
    )


# Lazy consent retries fire from EVERY transmit path (samples, spans,
# candidates, crashes, snapshots) until a delete token is held. Without a
# cool-down, an unregistered install hammers the server's consent endpoint on
# every flush — and its strict per-IP rate bucket (shared behind the platform
# proxy) then starves OTHER apps' genuine first registrations. Mirror the
# Node/Web kits' 1-hour retry cadence: after any failed attempt (transport
# error, 429, 4xx/5xx, or a tokenless 200), lazy retries wait an hour. An
# explicit enable_telemetry() call always bypasses the cool-down (user-driven).
_CONSENT_RETRY_COOLDOWN_S = 3600.0
_consent_next_attempt_at = 0.0

# ── SELF-HEALING REINSTALL RECOVERY (identity rotation) ─────────────────────
# A redeployed/reprovisioned process can keep a persisted ``install_id`` while
# losing its ``delete_token`` (a config restored from a backup that predates
# registration, an id seeded by the host/onboarding flow, or a partially wiped
# config). Its tokenless re-consent then gets the idempotent orphan response
# ("already_registered_no_token") forever: a tokenless 200 on every flush and
# NO credential, so the install sits silently inert. Because a FRESH
# registration with a valid invite key is already permitted (that is exactly
# what a brand-new install does), the kit recovers on its own: mint a fresh
# random install id, persist it in the kit's own config (so every later run
# uses the rotated identity), and re-register under it. Server capability is
# UNCHANGED — no new endpoint, no weakened proof rule; the old row's
# credentials stay dead and the row simply goes dormant.
#
# Guard rails (parity with the Node/RN/Web kits):
#   • fires ONLY on the orphan status, with an invite key in hand, and with NO
#     tokens (a Repair-window response carries tokens and takes the normal
#     adoption path — rotation can never race an owner's Repair);
#   • at most ONE rotation per process (no retry loops);
#   • the rotated id is persisted to ~/.boosthis/config.json, so relaunches
#     resume the post-rotation identity;
#   • forget() wipes the config (and this latch), leaving nothing behind.
_rotated_this_session = False


def _reset_rotation_for_tests() -> None:
    """Test-only: clear the once-per-process identity-rotation latch."""
    global _rotated_this_session
    _rotated_this_session = False


# --- Coverage freshness -----------------------------------------------------
# Registration happens at start-up. Most of what the coverage inventory has to
# say does not exist yet at start-up: a job library imported lazily is not in
# ``sys.modules`` until something needs it, no unit of work has finished, and
# no database module has been touched. An inventory sent once at boot would be
# systematically incomplete and — worse — would read on the dashboard as
# "nothing unwatched here" for the life of the process. That false clean bill
# of health is exactly what this feature exists to prevent.
#
# So the answer is re-sent when it CHANGES, and only then. Bounded hard: a
# handful of refreshes per process, no more often than the gap below, because a
# re-registration is a real round trip and a coverage update is worth far less
# than the customer's request latency.
_coverage_last_sent: str | None = None
_coverage_refreshes = 0
_coverage_checked_at = 0.0
_MAX_COVERAGE_REFRESHES = 6
_COVERAGE_REFRESH_GAP_S = 60.0


def _coverage_field() -> dict[str, Any]:
    """``{"coverage": {...}}``, or ``{}`` when there is nothing honest to say.

    Also latches the fingerprint of what we sent, so the refresh below can tell
    a changed answer from an unchanged one. Never raises: a failure here sends
    no coverage, which the server reads as "this install has not answered".
    """
    global _coverage_last_sent
    try:
        from boosthis.coverage_inventory import (
            coverage_fingerprint,
            coverage_inventory,
        )

        inventory = coverage_inventory()
        _coverage_last_sent = coverage_fingerprint(inventory)
        if not inventory.get("watched") and not inventory.get("unwatched"):
            return {}
        return {"coverage": inventory}
    except Exception:  # noqa: BLE001
        return {}


def _coverage_due() -> bool:
    """The cheap half of the refresh: may we look again yet?

    Marks the check as taken, so two callers in the same second cost one look.
    """
    global _coverage_checked_at
    if is_boosthis_disabled():
        return False
    if _coverage_refreshes >= _MAX_COVERAGE_REFRESHES:
        return False
    # ``None`` means the first consent has not happened yet — it will carry the
    # inventory itself, so there is nothing to refresh.
    if _coverage_last_sent is None:
        return False
    now = time.monotonic()
    if now - _coverage_checked_at < _COVERAGE_REFRESH_GAP_S:
        return False
    _coverage_checked_at = now
    return True


def _coverage_send_if_changed(cfg: "TelemetryConfig") -> None:
    """The expensive half: read the inventory and re-register only if it moved.

    Never raises — a coverage update is worth nothing next to the host's own
    work, so every failure here is silence.
    """
    global _coverage_refreshes
    try:
        from boosthis.coverage_inventory import (
            coverage_fingerprint,
            coverage_inventory,
        )

        fingerprint = coverage_fingerprint(coverage_inventory())
        if _coverage_last_sent is None or fingerprint == _coverage_last_sent:
            return
        _coverage_refreshes += 1
        _try_register_consent(cfg, force=True)
    except Exception:  # noqa: BLE001
        pass  # a coverage update is never worth disturbing the host


def _maybe_refresh_coverage(cfg: "TelemetryConfig") -> None:
    """Re-register when this app's coverage picture has actually changed."""
    try:
        if not _coverage_due():
            return
        _coverage_send_if_changed(cfg)
    except Exception:  # noqa: BLE001
        pass


def note_measured_work() -> None:
    """Coverage freshness on the ALWAYS-ON measured-work path.

    Called from ``tracker._emit`` — the one place every measured unit of work
    passes through, in every mode. It has to live here rather than on an upload
    because the upload paths are gated: in the default issues-only mode no
    sample and no snapshot ever ships, so an app that meets a job library at
    its first request, opens its database, or was refused an attach would never
    say so, and the project page would read "not reporting" for the life of the
    process — the false clean bill of health this whole feature exists to
    prevent.

    Cheap on the request thread: a counter and a clock compare. Only when the
    answer is actually due does the inventory get read, and the re-registration
    itself is handed to a daemon thread so the host's request is never waiting
    on our round trip. Bounded to a handful of refreshes per process, and only
    when the fingerprint moved. Never raises.
    """
    try:
        if not _coverage_due():
            return
        cfg = _read_config()
        if cfg is None or not cfg.enabled:
            return
        # Under pytest, run it inline: the tests assert on what was sent, and a
        # daemon thread would make that a race. Mirrors sample_uploader.
        if os.environ.get("PYTEST_CURRENT_TEST"):
            _coverage_send_if_changed(cfg)
            return
        threading.Thread(
            target=_coverage_send_if_changed, args=(cfg,), daemon=True
        ).start()
    except Exception:  # noqa: BLE001
        pass


def _try_register_consent(cfg: TelemetryConfig, *, force: bool = False) -> bool:
    """Best-effort consent registration. Returns True on success and
    persists any returned delete_token to disk. Existing delete_token is
    preserved if the server returns 200 (re-consent) without a new one.

    Also captures the server-issued ``readToken`` (the read-only credential the
    developer hands to their own AI) and latches the ``shareMeterWithAI``
    directive (the server emits it once the developer connects an AI from the
    web dashboard), then re-syncs the snapshot mirror so a private app starts
    mirroring with NO code change.
    """
    global _consent_next_attempt_at, _rotated_this_session
    if not force and time.monotonic() < _consent_next_attempt_at:
        return False
    # Any outcome that does NOT capture a delete token starts the cool-down;
    # it is cleared again on a token-bearing success below.
    _consent_next_attempt_at = time.monotonic() + _CONSENT_RETRY_COOLDOWN_S
    # Proof-of-control backfill: the server only mints/back-fills + returns a
    # read token for a LEGACY install when we prove control by presenting the
    # install's DELETE token in the X-Boosthis-Install-Token header (the
    # Authorization header carries the invite key). A distributable invite key +
    # a guessed install id is NOT sufficient. Sent ONLY when we already hold a
    # delete token; a fresh registration mints the read token unconditionally
    # and ignores this header. It is a Boosthis-server credential (not user
    # data), so it rides ``trusted_headers`` — added AFTER the PII guard, exactly
    # like the invite-key Authorization header.
    trusted_headers = (
        {"X-Boosthis-Install-Token": cfg.delete_token}
        if cfg.delete_token
        else None
    )
    try:
        # Use the internal response-capable path: it accepts ``auth_header``
        # (the public ``safe_transmit_with_response`` does not). The invite
        # key, when present, is a Boosthis-server credential, so it rides the
        # Authorization header on consent only — never on sample/candidate
        # ingest. With no invite key this behaves exactly like the public path.
        status, body = _safe_transmit_with_response_internal(
            f"{cfg.endpoint}/installs/consent",
            {
                "installId": cfg.install_id,
                "runtime": "py",
                "packageVersion": RUNTIME_VERSION,
                **(
                    {"serverKind": _declared_server_kind}
                    if _declared_server_kind
                    else {}
                ),
                # Optional developer-chosen display name (metadata). Only sent
                # when provided; the PII guard still screens the value.
                **({"appName": cfg.app_name} if cfg.app_name else {}),
                # WHERE THIS PROJECT RUNS. The hosting platform, its region and
                # whether this is production or a throwaway preview, each
                # recognised from what the platform itself publishes and each
                # sent as a word from a fixed list (see hosting.py). Read fresh
                # on every process, so a project that gets republished
                # somewhere else stops being described by where it used to run.
                #
                # Sent unconditionally, because every one of the three has a
                # real answer for the "nothing declared itself" case: an
                # ordinary server says so rather than going quiet, and only an
                # install from a kit built before this existed leaves the
                # record blank.
                "hosting": hosting_facts(),
                # WHETHER THIS PROCESS CAN KEEP BEING THIS INSTALL. Three
                # closed-vocabulary facts about where the install id is kept
                # (see identity_facts): which rung of the ladder this launch
                # settled on, and whether it picked up an id an earlier run had
                # written. Never a path, never an error string.
                #
                # Sent unconditionally, and it is the answer to the question
                # nothing else on the wire could answer: a service that arrives
                # as a brand-new install on every restart looks, from the
                # server, exactly like a service nobody is using. Saying "this
                # container has nowhere durable to write" turns a stream of
                # one-shot installs from a fault to chase into a stated
                # condition of where the app runs.
                "identity": identity_facts(),
                # WHAT WE ARE NOT WATCHING IN THIS APP. Green meters read as
                # full coverage, and this kit is the only thing that knows they
                # are not: that the app imported Dramatiq, which has no adapter
                # here, that it opened a database this package has never
                # watched, that a Streamlit rerun cannot produce an HTTP reply
                # to read cache headers off. Terms from watchable_surfaces and
                # counts — never a module, route or library name, and never a
                # percentage. Omitted entirely when there is nothing to say, so
                # the server reads silence as "has not answered", never as full
                # coverage. Re-sent when the answer CHANGES (see
                # ``_maybe_refresh_coverage``): most of it does not exist yet
                # at start-up.
                **_coverage_field(),
            },
            auth_header=f"Bearer {cfg.invite_key}" if cfg.invite_key else None,
            trusted_headers=trusted_headers,
            # Consent is the step that LEADS to activation, so it must be
            # allowed to send while the kit is merely LOCKED (never handshaken).
            # A KILLED install (revoked / unpaid / grace-expired) is still
            # silenced. Mirrors Node's consent ``allowWhenLocked``.
            allow_when_locked=True,
        )
    except urllib.error.HTTPError as e:
        # Registration was rejected (bad/revoked invite key). urllib raises on
        # 4xx, so this is where a 401/403 lands. Python has no locked-screen UI
        # (unlike RN), so surface a one-time plain-English hint, then behave
        # exactly as before (a rejected consent is still a failed consent).
        if e.code == 400 and _is_invalid_install_id_error(e):
            # The install id is not a UUID (a hand-edited config or a
            # host-seeded "stable id" string). Re-sending the SAME id can never
            # succeed, so park lazy retries for the rest of the process instead
            # of scheduling the hourly one — that cadence stays reserved for
            # key rejection. An explicit enable_telemetry() still forces a
            # fresh attempt (user-driven), and a restart re-reads the config.
            _note_invalid_install_id_rejection()
            mark_registration_refused()
            _consent_next_attempt_at = float("inf")
        elif e.code in (401, 403):
            mark_registration_refused()
            _warn_registration_rejected_once(e)
        else:
            # Any other HTTP status the server answered with (500, 429-with-body,
            # a 4xx that is neither the key nor the id) still means registration
            # did not go through. Say so once — a bare non-2xx that logs nothing
            # is the silent failure this warning exists to end.
            _warn_stayed_inactive_once(e.code)
            mark_registration_unreachable()
        return False
    except Exception as e:  # noqa: BLE001
        # No HTTP answer at all: the transport threw (DNS, TLS/certificate,
        # blocked egress, a proxy). This is THE case that costs hours — a broken
        # certificate store looks exactly like a healthy-but-quiet install. Emit
        # the one-shot line with status 0 and a FIXED, kit-owned classification
        # of the caught error as the detail — never the raw client/OS error
        # message, which can embed a URL or a credential-bearing URL.
        _warn_stayed_inactive_once(0, error_detail=_classify_transport_error(e))
        mark_registration_unreachable()
        return False
    if not (200 <= status < 300):
        # A response we could read but whose status is not a success (the
        # response-capable path returns rather than raising here). Registration
        # did not go through, so say so once before giving up.
        _warn_stayed_inactive_once(status, body if isinstance(body, dict) else None)
        mark_registration_unreachable()
        return False
    # The server accepted this install id, so any earlier "not a UUID"
    # rejection is stale (e.g. an identity rotation just minted a fresh
    # uuid4) — drop the state so the dashboard card disappears.
    _clear_invalid_install_id_state()
    mark_registration_confirmed()
    new_token = body.get("deleteToken")
    if isinstance(new_token, str) and new_token:
        # Always accept the server's current token (supports rotation),
        # not just on first issuance.
        cfg.delete_token = new_token
    read_token = body.get("readToken")
    if isinstance(read_token, str) and read_token:
        cfg.read_token = read_token
    # Server-authorized auto-share directive: latch (rising-edge) whenever the
    # server says an AI is connected. Never cleared here on a False/absent value
    # — it is a one-way "an AI is connected" signal; the developer stops sharing
    # via disable()/forget()/kill-switch.
    if body.get("shareMeterWithAI") is True:
        cfg.server_share = True
    # Dashboard "Full telemetry" directive — BOTH edges, unlike the rising-edge
    # share latch above: the server echoes the stored switch on every consent,
    # so ON grants the full meter picture and OFF withdraws ONLY the server
    # grant (the code-configured ``share_meter_with_ai`` opt-in still stands).
    # A non-boolean/absent value leaves the persisted state untouched.
    full_directive = body.get("fullTelemetry")
    if isinstance(full_directive, bool):
        cfg.server_full = full_directive
    # WHICH PROJECT this key belongs to. The name is developer-authored, so the
    # identity module re-sanitises it before any surface can render it. Replies
    # from older servers omit both fields and must leave the last answer alone.
    if body.get("projectName") is not None or body.get("projectCode") is not None:
        try:
            set_kit_project(body.get("projectName"), body.get("projectCode"))
            cfg.project_identity_json = serialize_kit_project(get_kit_project())
        except Exception:  # noqa: BLE001
            # Persistence is display-only and best-effort; consent must succeed
            # even if this one local fact cannot be stored.
            pass
    # WHAT THE DEVELOPER SAID MUST STAY TRUE. Their own words plus a closed
    # standing token — no sentence of ours travels, and no verdict either (see
    # project_identity). Screened again on the way in, rendered as text.
    #
    # UNCONDITIONAL, unlike the identity above. The reply is this run's whole
    # answer about the promises: an empty list means the project holds none,
    # and nothing at all means Boosthis could not read them. Applying only the
    # first would keep a deleted promise on the page for the life of the
    # process, and the persisted copy would put it back on the next one.
    try:
        set_kit_promises(body.get("promises"))
        cfg.project_promises_json = serialize_kit_promises()
    except Exception:  # noqa: BLE001
        # Same rule as the identity above: a page that cannot be given the
        # promises shows none, and consent still succeeds.
        pass
    # SELF-HEALING: the server says this install id is registered but we hold no
    # proof (orphaned identity — a restored/seeded config lost the tokens). With
    # an invite key in hand and a response that carried NO tokens (a Repair-
    # window response carries tokens and was adopted above — never rotate over a
    # Repair), mint a FRESH identity, persist it, and re-register under it. Once
    # per process, so a persistently-orphaning server can never cause a loop.
    orphan = body.get("status") == "already_registered_no_token"
    if (
        orphan
        and not cfg.delete_token
        and not cfg.read_token
        and cfg.invite_key
        and not _rotated_this_session
    ):
        _rotated_this_session = True
        # Record WHICH id this replaced before overwriting it. Without that the
        # rotation is unattributable, and a host that keeps passing the original
        # id (an argument, or BOOSTHIS_INSTALL_ID) would be handed straight back
        # to the orphaned identity on every restart. With it, the resolver can
        # resume the rotation — but only for the id it actually replaced.
        cfg.baked_install_id = cfg.install_id
        cfg.install_id = str(uuid.uuid4())
        # Persist the rotated identity FIRST so a crash mid-recovery still
        # leaves the next run knocking as the new (registrable) install.
        _write_config(cfg)
        # Repoint the kill-switch heartbeat at the rotated identity — otherwise
        # it knocks with the dead id plus the new token and the activation lock
        # never releases. Best-effort; consent must never throw into the host.
        try:
            _install_checkin_config(cfg.endpoint, cfg.install_id)
        except Exception:  # noqa: BLE001
            pass
        # Re-register under the fresh identity — a NORMAL fresh registration
        # that mints fresh credentials via the standard path.
        return _try_register_consent(cfg, force=True)
    if orphan and not cfg.delete_token and not cfg.read_token:
        # Rotation has already happened (or cannot — no invite key), and the
        # answer is STILL the orphan marker. This is NOT a registration: the
        # response rode a 200 but carried no credential, so persisting it as one
        # leaves an install that looks connected, uploads nothing, and never
        # explains itself — the exact 200-that-reads-as-success dead end. Say it
        # once and stop; never loop.
        _warn_stayed_inactive_once(status, body)
        return False
    _write_config(cfg)
    # Keep the snapshot mirror consistent with the (possibly just-latched) share
    # policy — fires one immediate flush only on the off→on rising edge.
    _sync_snapshot_submitter(cfg)
    # Consent just adopted the server-issued delete/read token, so the
    # kill-switch heartbeat's lazy ``get_token`` now returns a credential. Fire a
    # FORCED entitlement check (bypassing the throttle) so a fresh install (or a
    # rotation that just re-minted a token) proves activation immediately rather
    # than waiting for the next heartbeat tick. Non-blocking + self-guarded.
    force_entitlement_check(True)
    if cfg.delete_token:
        # Registration credential in hand — lazy retries may fire freely again
        # (they short-circuit on the held token anyway).
        _consent_next_attempt_at = 0.0
    return True


def disable_telemetry() -> None:
    """Stop sending the optional full performance samples.

    Issue + fix reporting stays ON for registered (invited/public) apps:
    that channel is intentionally NOT user-toggleable, so the candidate
    auto-submitter is left registered. Only the emergency kill-switch
    (``BOOSTHIS_DISABLED``) or erasure (``forget()``) stops it. Keeps the
    install_id and delete_token so re-enabling resumes the same identity.
    """
    cfg = _read_config()
    if cfg is None:
        return
    cfg.enabled = False
    _write_config(cfg)
    # The snapshot mirror is screen-bearing OPTIONAL data (like the RN/Node full
    # sampler), so disabling stops it: _effective_share now returns False, so
    # this clears the submitter and blocks any queued flush. The always-on issue
    # + fix channel is untouched above.
    _sync_snapshot_submitter(cfg)


def forget() -> bool:
    """GDPR right-to-erasure. Calls ``POST /installs/forget`` with the
    stored delete_token (required by the server) then deletes the local
    config file and the candidate-rule store. Returns ``True`` if local
    config existed.
    """
    # Local state is wiped unconditionally — even if the server call
    # below fails, candidates + submitter registration are gone. Clearing the
    # snapshot submitter also blocks any queued flush, so nothing screen-bearing
    # can leak after erasure.
    global _rotated_this_session
    # Erasure deletes the config file, and with it any rotated identity, so the
    # once-per-process rotation latch resets too: a later enable_telemetry()
    # starts a brand-new identity lifecycle that may self-heal on its own.
    _rotated_this_session = False
    # Same for the "install id is not a UUID" rejection: the offending config is
    # about to be deleted, so the state (and its dashboard card) must go too.
    _clear_invalid_install_id_state()
    _reset_kit_project()
    set_candidate_submitter(None)
    set_resolution_submitter(None)
    snapshot_mirror.set_snapshot_submitter(None)
    # Stop the server-authority kill-switch heartbeat and erase the persisted
    # entitlement cache — erasure must leave nothing Boosthis-shaped behind, and
    # a copy without a proven handshake must not run (the ACTIVATION LOCK
    # re-engages exactly as on a never-connected install). Both are best-effort
    # and never raise. Mirrors Node's forget() wiring.
    stop_entitlement_checkin()
    clear_entitlement_cache()
    # Tear down the span mirror too: unwire the submitter (making enqueue_span
    # inert) and drop any buffered spans, so nothing span-shaped can leak after
    # erasure.
    span_emitter.set_span_submitter(None)
    span_emitter.clear_buffered_spans()
    # Tear down the per-request sample upload too: unwire the submitter (making
    # enqueue_sample inert) and drop anything buffered, so no measurement can
    # leak after erasure.
    sample_uploader.set_sample_submitter(None)
    sample_uploader.clear_buffered_samples()
    # Tear down the job-run reporter too: unwire the submitter (making
    # report_job_run inert) and drop anything buffered.
    job_reporter.set_job_run_submitter(None)
    job_reporter.clear_buffered_job_runs()
    # And every rhythm this app declared: erasure must leave Boosthis holding
    # nothing, including "watch this job".
    job_reporter.clear_job_rhythm_declarations()
    # And every promise its code declared, for the same reason: a statement
    # waiting to be delivered is something Boosthis is still holding.
    promise_declaration.clear_promise_declarations()
    # Stop crash reporting and remove every trace: clears the submitter, restores
    # the host's ORIGINAL excepthook/threading.excepthook, drops in-memory pending
    # crashes, and deletes ~/.boosthis/crash-pending.json.
    crash_reporter.set_crash_submitter(None)
    crash_reporter.uninstall_crash_handlers()
    clear_all_candidates()
    live_detectors.clear_detectors()  # wipe retry-storm / idle-burn state
    event_loop_lag.clear_event_loop_lag()  # disarm + wipe loop-lag samples
    runtime_vitals.clear_vitals()  # remove gc hook + wipe memory/reliability state
    meter_axes.clear_meter_axes()  # stop sched heartbeat + wipe network/idle/etc.
    job_adapters.unpatch_job_systems()
    extra_meters.clear_extra_meters()  # unhook log/pool/popen + wipe batch state
    extra_meters.erase_extra_meter_files()  # + delete the worker-boot history/lock
    os_meters.clear_os_meters()  # wipe the 2026-08 batch (no hooks, no files)
    build_identity.clear_build_identity()  # drop the frozen build-identity state
    host_surface.clear()  # forget which host we mounted on, and its wiring record
    cfg = _read_config()
    if cfg is None:
        return False
    # Kill-switch honors silence even on erasure: when BOOSTHIS_DISABLED is
    # set the runtime must not touch the network at all. We still wipe the
    # on-disk config so the user's intent is recorded locally.
    if is_boosthis_disabled() and cfg.delete_token:
        _erase_stored_config()
        return True
    if cfg.delete_token:
        # GDPR right-to-erasure: bypass safe_transmit because the PII
        # guard would (correctly, in general) reject the server-issued
        # `deleteToken` field — its tokenized form contains "token".
        # The payload shape is fixed and contains only the install_id
        # + the token we are returning, no user data.
        _post_forget_raw(
            f"{cfg.endpoint}/installs/forget",
            {"installId": cfg.install_id, "deleteToken": cfg.delete_token},
        )
    _erase_stored_config()
    return True


def transmit_samples(samples: list[dict[str, Any]]) -> int:
    """Send a batch of samples if telemetry is enabled. Returns the number
    of samples successfully accepted, or 0 if telemetry is disabled, no
    delete_token has been issued yet, or the network call fails.

    Each sample dict must contain at minimum ``routeLabel``, ``durationMs``,
    and ``rating``. A sample whose route label trips the PII guard is dropped
    from the batch; the rest still go.
    """
    cfg = _read_config()
    if cfg is None or not cfg.enabled or not samples:
        return 0
    # If we never managed to capture a delete_token (e.g. the network was
    # down at enable() time), retry consent now. Otherwise the server will
    # reject our batch with 401.
    if not cfg.delete_token:
        _try_register_consent(cfg)
        cfg = _read_config() or cfg
    # By now this app has served real traffic, so the surfaces that only appear
    # once it runs — a lazily imported job library, an opened database — finally
    # have an honest answer. Re-register if, and only if, that answer moved.
    _maybe_refresh_coverage(cfg)
    if not cfg.delete_token:
        return 0
    # Apply route-label hardening + closed metadata schema before assertNoPII,
    # mirroring the RN transmit path:
    #
    # 1. routeLabel is normalized by the shared part-name rule — samples with
    #    over-long names, whitespace, UUID, long numeric ID,
    #    email, JWT, or phone labels are silently dropped.
    #
    # 2. metadata is normalised to ONLY the two privacy-safe enum buckets
    #    (startType + deviceTier). Any host-provided key outside those two is
    #    omitted; values outside the allowed enum sets are coerced to "unknown"
    #    so arbitrary host strings (which may be PII) never leave the process.
    #
    # 3. The full sample dict is rebuilt from explicit allowed fields only —
    #    unknown top-level keys are dropped (closed-schema policy).
    _ALLOWED_START_TYPES = frozenset({"cold", "warm", "hot", "unknown"})
    _ALLOWED_DEVICE_TIERS = frozenset({"low", "mid", "high", "unknown"})

    def _norm_meta(raw_meta: Any) -> dict[str, str]:
        if not isinstance(raw_meta, dict):
            raw_meta = {}
        start = raw_meta.get("startType")
        tier = raw_meta.get("deviceTier")
        return {
            "startType": start if isinstance(start, str) and start in _ALLOWED_START_TYPES else "unknown",
            "deviceTier": tier if isinstance(tier, str) and tier in _ALLOWED_DEVICE_TIERS else "unknown",
        }

    clean_samples = []
    for s in samples:
        label = route_inventory.normalize_part_name(s.get("routeLabel", ""))
        if label is None:
            continue
        cleaned: dict[str, Any] = {
            "routeLabel": label,
            "durationMs": s.get("durationMs"),
            "rating": s.get("rating"),
            "metadata": _norm_meta(s.get("metadata")),
        }
        if "ruleId" in s:
            cleaned["ruleId"] = s["ruleId"]
        clean_samples.append(cleaned)
    if not clean_samples:
        return 0
    payload = {
        "installId": cfg.install_id,
        "packageVersion": RUNTIME_VERSION,
        "samples": clean_samples,
    }
    # assert_no_pii raises if the payload is dirty — let it propagate so
    # callers know they have a problem rather than silently swallowing it.
    assert_no_pii(payload)
    try:
        status, body = _safe_transmit_with_response_internal(
            f"{cfg.endpoint}/samples",
            payload,
            auth_header=f"Bearer {cfg.delete_token}",
        )
    except Exception as exc:  # noqa: BLE001
        _note_upload_exception(exc)
        return 0
    if status == LOCKED_NOT_SENT:
        # The kill-switch or the ACTIVATION LOCK stopped this inside the
        # transport; no socket was opened, so there is nothing to record and
        # nothing to count. Say so, and the uploader keeps the batch for a
        # flush that can actually make it. Reporting acceptance here is what
        # made a whole project's measurements disappear quietly.
        return NOT_SENT
    if 200 <= status < 300:
        note_upload_accepted(body)
        return len(clean_samples)
    _note_upload_response(status)
    return 0


__all__ = [
    "DEFAULT_ENDPOINT",
    "CONFIG_PATH",
    "TelemetryConfig",
    "get_config",
    "is_enabled",
    "enable_telemetry",
    "disable_telemetry",
    "forget",
    "transmit_samples",
]


def get_last_upload_at() -> float | None:
    """Epoch-ms of the last MEASUREMENT upload the server accepted, or ``None``
    when this process has never had one accepted. Read by the standalone status
    page. Never raises."""
    return _last_upload_at_ms


def get_last_upload_failure() -> tuple[float, str] | None:
    try:
        if _last_upload_fail_at_ms is None or _last_upload_fail_reason is None:
            return None
        return (_last_upload_fail_at_ms, _last_upload_fail_reason)
    except Exception:  # noqa: BLE001
        return None


def get_dropped_upload_count() -> int:
    try:
        return _dropped_uploads
    except Exception:  # noqa: BLE001
        return 0


def is_last_upload_attempt_failed() -> bool:
    try:
        return _last_upload_attempt_failed
    except Exception:  # noqa: BLE001
        return False


def _reset_last_upload_for_tests() -> None:
    """@internal Test hook — clear the in-process last-upload stamp."""
    global _last_upload_at_ms, _last_upload_fail_at_ms, _last_upload_fail_reason
    global _dropped_uploads, _last_upload_attempt_failed
    _last_upload_at_ms = None
    _last_upload_fail_at_ms = None
    _last_upload_fail_reason = None
    _dropped_uploads = 0
    _last_upload_attempt_failed = False

# Declared by the host (``worker.py``), so it is unset in every ordinary web
# process. It needs a module-level value regardless: without one the read in the
# consent payload raises NameError, which the transport catch reports as "could
# not reach the Boosthis API" — a wrong diagnosis for a kit that never left.
_declared_server_kind: str | None = None

# Same shape of trap, same file: both of these are only ever bound inside
# ``enable_telemetry``'s ``global`` statement, so every read before the first
# enable — the panel, the project-key override notice — raised NameError.
# The starting value has to be a real ResolvedProjectKey, not None: every reader
# goes straight for .display/.source, and the panel's catch-all turns an
# AttributeError into "no project key configured" AND a null install id.
_active_project_key = ResolvedProjectKey(
    None, ProjectKeySource.NONE, False, None, False, True
)
_warned_project_key_override = False


def _set_declared_server_kind(kind: str | None) -> None:
    global _declared_server_kind
    _declared_server_kind = kind if isinstance(kind, str) and kind else None

def _probe_state_dir(d: Path) -> bool:
    """Can this directory hold credentials, and can we actually write in it?

    Answered by DOING it, never by inspecting a mode: a directory can look
    perfect and still be read-only, and an unwritable rung silently chosen is
    exactly how an install id stops surviving.
    """
    try:
        dfd = _pinned_state_dir_fd(d, create=True)
    except (OSError, ValueError):
        return False
    if dfd is None:
        return _probe_state_dir_by_name(d)
    try:
        # O_EXCL, so a probe planted by somebody else is a refusal rather than
        # something we write into. Everything here is relative to the pinned
        # descriptor, so the directory cannot be swapped underneath it.
        fd = os.open(
            _WRITE_PROBE_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
            0o600,
            dir_fd=dfd,
        )
        os.close(fd)
        os.unlink(_WRITE_PROBE_NAME, dir_fd=dfd)
        return True
    except FileExistsError:
        # Our own probe, left behind by a run that was killed between the
        # create and the unlink. Clear it and answer the question once.
        try:
            os.unlink(_WRITE_PROBE_NAME, dir_fd=dfd)
            fd = os.open(
                _WRITE_PROBE_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                0o600,
                dir_fd=dfd,
            )
            os.close(fd)
            os.unlink(_WRITE_PROBE_NAME, dir_fd=dfd)
            return True
        except (OSError, ValueError):
            return False
    except (OSError, ValueError):
        return False
    finally:
        os.close(dfd)

def _probe_state_dir_by_name(d: Path) -> bool:
    """The same probe where no descriptor can be pinned (Windows).

    Weaker by the platform's own limits, and deliberately not silent about it:
    a link anywhere in the path is refused, and the mode repair is re-read
    rather than trusted.
    """
    try:
        _refuse_linked_components(d)
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        # Re-read the repair rather than trusting the call: on some hosts chmod
        # returns without error and changes nothing.
        if (os.stat(d).st_mode & 0o077) != 0:
            return False
        probe = d / _WRITE_PROBE_NAME
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        try:
            probe.unlink()
        except OSError:
            pass
        return True
    except (OSError, ValueError):
        return False

def _state_dir_candidates() -> list[tuple[Path, Path, str]]:
    """The rungs of the state ladder, best first, as (directory, file, word).

    Built in one place because two callers need the same list: the resolver that
    picks a rung, and the notice that has to say WHY none of them worked.
    """
    env_dir = os.environ.get(IDENTITY_STATE_DIR_ENV) or None
    rungs: list[tuple[Path, Path, str]] = []
    if env_dir:
        rungs.append((Path(env_dir), Path(env_dir) / "config.json", "configured"))
    # The home rung is whatever CONFIG_DIR/CONFIG_PATH name, so a host that
    # redirects them (and every test that does) keeps its own answer.
    rungs.append((CONFIG_DIR, CONFIG_PATH, "home"))
    try:
        scratch = Path(tempfile.gettempdir()) / f"boosthis-state-{_scratch_scope()}"
        rungs.append((scratch, scratch / "config.json", "scratch"))
    except Exception:  # noqa: BLE001 — no temp directory is a real possibility.
        pass
    return rungs
def _resolve_store() -> tuple[Path | None, str]:
    """(config path, which rung) for this process. ``None`` means memory-only."""
    global _store_choice
    env_dir = os.environ.get(IDENTITY_STATE_DIR_ENV) or None
    stamp = (env_dir, str(CONFIG_DIR), str(CONFIG_PATH))
    if _store_choice is not None and _store_choice[0] == stamp:
        return _store_choice[1], _store_choice[2]

    chosen: Path | None = None
    word = "memory"
    rungs = _state_dir_candidates()
    for directory, path, rung in rungs:
        if _probe_state_dir(directory):
            chosen, word = path, rung
            break
    _store_choice = (stamp, chosen, word)
    return chosen, word

def _erase_stored_config() -> None:
    """Forget this install everywhere it could have been kept.

    Erasure must not depend on which rung the ladder picked: a config written to
    scratch (or held in memory because nothing was writable) has to be erasable
    by the same call that erases one in the home directory.
    """
    global _memory_config
    _memory_config = None
    path, _rung = _resolve_store()
    for candidate in {path, CONFIG_PATH}:
        if candidate is None:
            continue
        try:
            candidate.unlink()
        except (FileNotFoundError, OSError):
            pass

def _scratch_scope() -> str:
    """A short tag that is the SAME for every restart of this service and
    DIFFERENT for another service sharing the machine.

    The home directory is naturally one developer's; the system temp directory
    is not. An unnamespaced ``/tmp/boosthis-state/config.json`` would be found
    by every Python service on the host, and the second one to start would read
    back the first one's install id and its credentials — two services merged
    into one install, under a project key neither of them was given. That is a
    far worse failure than the one this ladder exists to fix, so the rung is
    scoped before it is ever used.

    Scoped by what identifies the SERVICE and not the run: the entry point, the
    working directory it was started from, and the user id. All three survive a
    restart; none of them is a value we would ever transmit — only the digest
    is used, and only as a directory name.

    The working directory cannot simply be dropped, tempting though it is: the
    entry point of two different services on one host is routinely the SAME file
    (``.../bin/uvicorn``, ``.../bin/gunicorn``), and with the directory gone they
    would hash alike and the second to start would read back the first one's id
    AND its credentials. So the directory stays, and a host that restarts a
    service from somewhere else gets a lever instead: ``BOOSTHIS_APP_SCOPE``
    (the same label ``app_scope`` already honours) replaces the automatic inputs
    outright. The definitive answer for a host that moves or discards its state
    is one rung up — ``BOOSTHIS_INSTALL_ID`` — which does not consult this scope
    at all.
    """
    try:
        declared = (os.environ.get("BOOSTHIS_APP_SCOPE") or "").strip()
    except Exception:  # noqa: BLE001
        declared = ""
    if declared:
        return hashlib.sha256(declared.encode("utf-8", "replace")).hexdigest()[:16]
    try:
        entry = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    except Exception:  # noqa: BLE001
        entry = ""
    try:
        # Realpath so /srv/app and a symlink to it are one service, not two.
        cwd = os.path.realpath(os.getcwd())
    except OSError:
        cwd = ""
    try:
        uid = str(os.getuid())
    except AttributeError:  # non-POSIX
        uid = ""
    raw = "\u001f".join((entry, cwd, uid))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]

def reset_identity_store_for_tests() -> None:
    """Forget the probed rung and any memory-only config."""
    global _store_choice, _memory_config, _identity_restored
    global _store_degraded_to_memory, _identity_notice_said, _identity_pinned
    _store_choice = None
    _memory_config = None
    _identity_restored = False
    _store_degraded_to_memory = False
    _identity_notice_said = False
    _identity_pinned = False
    _refused_install_id_sources.clear()

# The three closed causes an identity loss can have. Closed because this word
# is read by a developer and asserted by our tests: an OS error string would
# vary by platform and could not be matched. The Go kit names the same three.
IDENTITY_LOSS_NO_DIR = "no writable state directory on this host"
IDENTITY_LOSS_NOT_OWNER_ONLY = "the state directory cannot be made owner-only"
IDENTITY_LOSS_NOT_KEPT = "the saved identity could not be kept on disk"


def _identity_loss_cause() -> str:
    """Which of the three closed causes applies to this process."""
    if _store_degraded_to_memory:
        return IDENTITY_LOSS_NOT_KEPT
    for directory, _path, _rung in _state_dir_candidates():
        try:
            mode = os.stat(directory).st_mode
        except OSError:
            continue
        # It is there, and the reason it was refused is the one the permissions
        # doctrine exists for. Saying "nowhere writable" here would send the
        # developer looking for a full disk.
        if (mode & 0o077) != 0:
            return IDENTITY_LOSS_NOT_OWNER_ONLY
    return IDENTITY_LOSS_NO_DIR
def identity_store_word() -> str:
    """Which rung this process settled on — one of the closed store words.

    Reports the EFFECTIVE store, not the probed one: once a real write has
    failed there is nowhere durable, whatever the probe predicted.
    """
    if _store_degraded_to_memory:
        return "memory"
    return _resolve_store()[1]

def identity_facts() -> dict[str, Any]:
    """What this install can say about keeping its own identity.

    Three closed-vocabulary facts, kept byte-compatible with the server's
    ``artifacts/api-server/src/lib/identityPersistence.ts``. It reports WHERE it
    put the file and whether it found one — never a path, never an error string,
    never a promise that a restart will work.
    """
    return {
        # Python mints its own id (unlike go/swift/kotlin/flutter, where the
        # host supplies one), so this kit's answer is always "store".
        "source": "store",
        "store": identity_store_word(),
        "restored": _identity_restored,
    }

def _can_pin_directories() -> bool:
    """Can this platform hold a directory OPEN and work relative to it?

    Asked at every call rather than cached, because a test that takes
    O_NOFOLLOW away is asking to be treated as the platform that has none.
    """
    supports = getattr(os, "supports_dir_fd", set())
    return bool(
        _O_NOFOLLOW
        and _O_DIRECTORY
        and os.open in supports
        and os.mkdir in supports
        and os.unlink in supports
    )

def _refuse_linked_components(d: Path) -> None:
    """The same refusal for a platform that cannot pin a descriptor.

    It cannot close the window between the look and the use — nothing can,
    without descriptors — but it still refuses the shape of the attack: a link
    anywhere in the path of a directory we are about to put credentials in.
    """
    walked = Path(os.path.abspath(str(d)))
    seen = [walked, *walked.parents]
    for part in seen:
        try:
            info = os.lstat(part)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise OSError(errno.ELOOP, "a link stands in the state path", str(d))

def _ensure_state_dir(d: Path) -> None:
    """Make sure the state directory exists, is ours, and is owner-only.

    Raises OSError when it cannot be — the caller degrades to memory, which is
    a reported condition rather than a silent write into somewhere unsafe.
    """
    dfd = _pinned_state_dir_fd(d, create=True)
    if dfd is None:
        _refuse_linked_components(d)
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Harden a directory an older version of the kit created at the process
        # umask. Best-effort: the file's own mode is set on the descriptor.
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return
    # The pinned walk has already repaired the mode and refused anything that
    # is not our own owner-only directory.
    os.close(dfd)

def _ancestor_is_tamper_safe(info: os.stat_result, euid: int | None) -> bool:
    """Can a directory we did NOT create be trusted to still be itself later?

    Only two things make an ancestor unsafe: somebody else owning it, and
    anybody being able to write in it. The sticky bit is the exception the
    system itself relies on — /tmp is world-writable by design, and the bit is
    exactly the promise that one user cannot rename or remove another's entry.
    """
    if euid is not None and info.st_uid not in (0, euid):
        return False
    if (info.st_mode & 0o022) and not (info.st_mode & stat.S_ISVTX):
        return False
    return True

def _pinned_state_dir_fd(d: Path, *, create: bool) -> int | None:
    """Open the state directory as a DESCRIPTOR, not a name.

    A check on a path is worth nothing to the operation that follows it: the
    directory can be replaced with a symlink in between, and every later call
    made by name — mkdir, chmod, stat, and the open of config.json itself —
    follows it into somewhere an attacker chose. The credentials in that file
    (delete token, read token, project key) would be written straight through.

    So the path is walked once, and what comes back is a descriptor pinned to
    the directory that was actually checked. Everything afterwards happens
    relative to it, and nothing re-resolves the name.

    The walk treats two kinds of component differently, because they carry
    different promises:

    * Ancestors we did not create are resolved the way every other program on
      the machine resolves them — /tmp being a symlink is a system convention,
      not an attack — but each one must be owned by us or by root and must not
      be writable by anybody else unless it is sticky. An attacker who cannot
      write in the parent cannot swap the child.
    * The directory we create and keep credentials in is opened with
      O_NOFOLLOW, must be ours, and must be owner-only. A link, somebody else's
      directory, or a loosened mode is refused outright.

    Returns None where the platform cannot pin a directory at all (Windows);
    the caller then falls back to the by-name checks. Raises OSError when the
    directory itself is refused — that is a real answer, and every caller
    already treats it as "nowhere durable here".
    """
    if not _can_pin_directories():
        return None
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    # abspath normalises "." and ".." WITHOUT resolving symlinks, which is what
    # we want: resolving here would hand the walk a path an attacker chose.
    parts = [c for c in os.path.abspath(str(d)).split(os.sep) if c]
    fd = os.open(os.sep, os.O_RDONLY | _O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            mine = False
            try:
                nxt = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                # Freshly ours, so it is opened under the strict rule even when
                # it is only an intermediate directory.
                nxt = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
                mine = True
            except OSError as err:
                if last or err.errno not in (errno.ELOOP, errno.EMLINK):
                    raise
                # An ancestor that is a link — /tmp on macOS is one. Follow it
                # the way the rest of the system does, then judge what it is.
                nxt = os.open(part, os.O_RDONLY | _O_DIRECTORY, dir_fd=fd)
            try:
                if not last and not mine:
                    if not _ancestor_is_tamper_safe(os.fstat(nxt), euid):
                        raise OSError(
                            errno.EPERM,
                            "a directory above the state directory is not safe "
                            "from tampering",
                            str(d),
                        )
            except BaseException:
                os.close(nxt)
                raise
            os.close(fd)
            fd = nxt
        # The directory the credentials go in: repair the mode, then judge what
        # we are actually holding rather than what the repair claimed.
        try:
            os.fchmod(fd, 0o700)
        except OSError:
            pass
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(errno.ENOTDIR, "state directory is not a directory", str(d))
        if (info.st_mode & 0o077) != 0:
            raise OSError(errno.EPERM, "state directory is not owner-only", str(d))
        if euid is not None and info.st_uid != euid:
            raise OSError(errno.EPERM, "state directory belongs to someone else", str(d))
        return fd
    except BaseException:
        os.close(fd)
        raise

def _replace_state_file(path: Path, content: bytes) -> None:
    """Put the new contents in place in ONE step, or leave the old ones alone.

    Writing over the config where it lies has a window in which the file holds
    neither the old identity nor the new one. A crash, a full disk, a killed
    container or a short write between the truncate and the final byte leaves a
    document nothing can read — and an install whose id cannot be read is a
    fresh install, with fresh credentials, on a fresh page.

    So the bytes go to a file of our own beside it, created with O_EXCL so it
    can be nothing that was already there, and the directory ENTRY is then moved
    over the old one. A rename is atomic: a reader sees the whole of one version
    or the whole of the other, and a crash at any point up to it leaves the
    previous identity exactly as it was.

    The move also cannot write through a planted link — it replaces the entry
    rather than following it — but the caller still judges that entry first and
    refuses one that is not ours, because a link there means the directory is
    not ours to keep credentials in.
    """
    directory = path.parent
    tmp_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    dfd = _pinned_state_dir_fd(directory, create=False)
    dir_fd_ops = getattr(os, "supports_dir_fd", set())
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
        fd = (
            os.open(tmp_name, flags, 0o600, dir_fd=dfd)
            if dfd is not None
            else os.open(str(directory / tmp_name), flags, 0o600)
        )
        try:
            _write_all(fd, content)
            # The file is created 0o600, and replacing the entry carries that
            # mode in — which is also how a config.json left at 0o644 by an
            # older kit gets its permissions repaired.
            os.fchmod(fd, 0o600)
            try:
                os.fsync(fd)
            except OSError:
                # Durability is best effort — some filesystems and sandboxes
                # refuse it. The atomic move is what protects the old identity.
                pass
        finally:
            os.close(fd)
        if dfd is not None and os.replace in dir_fd_ops:
            os.replace(tmp_name, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        else:
            os.replace(str(directory / tmp_name), str(path))
        if dfd is not None:
            try:
                os.fsync(dfd)
            except OSError:
                pass
    except BaseException:
        # Nothing of ours is left lying about, and the file that still holds the
        # identity has not been touched.
        try:
            if dfd is not None:
                os.unlink(tmp_name, dir_fd=dfd)
            else:
                os.unlink(str(directory / tmp_name))
        except OSError:
            pass
        raise
    finally:
        if dfd is not None:
            os.close(dfd)

def _stored_identity() -> tuple[str | None, str | None]:
    """``(install_id, baked_install_id)`` as they sit in the store right now.

    Deliberately free of ``_read_config``'s side effects — it latches
    "restored" and re-applies a stored project identity — so the scope resolver
    can ask the same question without pretending a registration happened.
    """
    path, _rung = _resolve_store()
    raw: Any = None
    if path is not None:
        try:
            fd = _open_state_file(path, os.O_RDONLY)
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                raw = json.loads(fh.read())
        except (json.JSONDecodeError, OSError, ValueError):
            raw = None
    if raw is None:
        raw = _memory_config
    if not isinstance(raw, dict):
        return None, None

    def _stored(key: str) -> str | None:
        # NOT UUID-validated, deliberately. A hand-edited id already has an
        # answer in this kit: it is sent, the server rejects it by name, and the
        # developer is told to fix that value. Quietly minting a replacement
        # here would turn that loud, recoverable refusal into exactly the thing
        # this resolver exists to prevent — a second install nobody asked for.
        # Validation belongs on the rungs ABOVE the store (an argument, an
        # environment variable), where falling through costs nothing.
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    return _stored("install_id"), _stored("baked_install_id")

def env_install_id() -> str | None:
    """The install id this host pinned in the environment, or ``None``."""
    for name in (INSTALL_ID_ENV, LEGACY_INSTALL_ID_ENV):
        try:
            raw = os.environ.get(name)
        except Exception:  # noqa: BLE001
            continue
        if raw is None or not raw.strip():
            continue
        cleaned = _clean_install_id(raw, name)
        if cleaned is not None:
            return cleaned
    return None

def _adopt_rotation(chosen: str) -> str:
    """Resume a rotation that replaced exactly ``chosen``, else ``chosen``.

    The reinstall self-heal mints a fresh id when the server reports this one as
    an orphan, and the replacement is the identity that owns the data from then
    on. It is adopted only when the store says it replaced THIS id, so a
    persisted rotation can never override a different id the developer named.
    """
    stored, baked = _stored_identity()
    if stored is not None and baked == chosen and stored != chosen:
        return stored
    return chosen

def resolve_install_id(
    explicit: str | None = None,
    existing: TelemetryConfig | None = None,
) -> str:
    """Which install this is: explicit > environment > persisted > freshly minted.

    ``existing`` is the already-read config, passed in only to save a second
    read of the store; leaving it ``None`` is equivalent.
    """
    cleaned = _clean_install_id(explicit, "the install_id argument")
    if cleaned is not None:
        return _adopt_rotation(cleaned)
    from_env = env_install_id()
    if from_env is not None:
        return _adopt_rotation(from_env)
    # The store's own value is taken as it stands — see _stored_identity for why
    # a hand-edited id is carried through rather than quietly replaced.
    if existing is not None:
        stored = existing.install_id if isinstance(existing.install_id, str) else ""
        stored = stored.strip()
        if stored:
            return _adopt_rotation(stored)
    else:
        stored_id, _baked = _stored_identity()
        if stored_id is not None:
            return _adopt_rotation(stored_id)
    return str(uuid.uuid4())

def known_install_id() -> str | None:
    """Which install this is, WITHOUT minting one: env > persisted.

    The one answer both halves of the kit read. Returns ``None`` when this copy
    has never registered and nothing was configured — a real answer, and not the
    same as "here is a brand new install".
    """
    from_env = env_install_id()
    if from_env is not None:
        return _adopt_rotation(from_env)
    stored, _baked = _stored_identity()
    return stored

def _announce_identity_at_risk(pinned: bool | None = None) -> None:
    """Say, once, that this install's identity will not survive a restart.

    Before the consequence arrives, not inferred afterwards from a duplicate row
    on the dashboard. Ungated (it is not telemetry, and a kit that cannot keep
    its identity usually cannot register either) and best-effort — evidence must
    never block the host.

    Silent when an id was pinned by argument or environment: the store does not
    decide who we are in that case, and the warning would be a lie.
    """
    global _identity_notice_said, _identity_pinned
    if pinned is not None:
        _identity_pinned = _identity_pinned or pinned
    if _identity_notice_said or _identity_pinned:
        return
    try:
        if identity_store_word() != "memory":
            return
        _identity_notice_said = True
        say_after_startup_line(
            "[boosthis] Boosthis cannot keep this install's identity between restarts ("
            + _identity_loss_cause()
            + "). Every restart registers as a new install. Set BOOSTHIS_INSTALL_ID to a fixed UUID to pin it."
        )
    except BaseException:  # noqa: BLE001 — evidence must never block the host
        pass

def _clean_install_id(raw: object, source: str | None = None) -> str | None:
    """A UUID-shaped id, or ``None``. Refuses (loudly) anything else."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if INSTALL_ID_SHAPE.match(value) is None:
        if source is not None:
            _refuse_install_id_once(value, source)
        return None
    return value
