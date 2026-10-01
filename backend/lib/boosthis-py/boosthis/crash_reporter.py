"""Boosthis: privacy-safe crash reporter (Python sibling of ``crashReporter.ts``).

Captures REAL uncaught crashes from the host process and turns each one into a
tiny, scrubbed fingerprint that is safe to send off-device. This is NOT a perf
checklist rule — it is a separate always-on channel (for registered apps) that
mirrors the candidate/snapshot model: install it once from ``enable_telemetry``
and the runtime reports on its own. Nothing here runs as an import side-effect;
:func:`install_crash_handlers` must be called explicitly.

Two capture sources, both CHAINED so Boosthis can never swallow a host crash or
clobber host behavior (mirrors RN's ``ErrorUtils`` chain guarantee):

  1. ``sys.excepthook`` — uncaught exceptions on the main thread. We store the
     previous hook and ALWAYS call it in a ``finally`` so the host's own
     handling (default traceback print, an installed reporter, …) still runs.
  2. ``threading.excepthook`` — uncaught exceptions on worker threads. Same
     store-previous / always-call-in-finally contract.

The asyncio loop exception handler is DEFERRED on purpose (no
``loop.set_exception_handler`` here): a drop-in kit must not replace a host's
own loop handler, and uncaught exceptions still surface through the two hooks
above.

PRIVACY: the DEFAULT payload carries only the exception type, a hashed
signature, a redacted top frame, and a bucketed count — never source, values,
message text, or PII. The signature is CODE-DERIVED only: the redacted top
traceback frame (function + file BASENAME + line) or, when there is no frame, a
constant ``errorName|kind|<no-frame>`` marker — NEVER the raw exception message
or args. OPT-IN detailed mode additionally carries a PII-scrubbed first message
line (``summary``) and sanitized stack ``frames``; ``summary`` and any frame
that trips the PII guard are dropped individually so the rest still reports. The
wire field names deliberately avoid the PII denylist (no ``message``/``text``/
``body``).

Crash-safety: every hook body is wrapped in ``try/except`` so a bug in the
reporter itself can never crash the host, and the capture/flush path persists
pending crashes SYNCHRONOUSLY to ``~/.boosthis/crash-pending.json`` so a FATAL
crash (which may kill the process before the network flush completes) is still
reported on the next launch.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from typing import Any, Callable

from boosthis.pii import check_no_pii
from boosthis.runtime_flags import is_boosthis_disabled

# Local pending-crash store lives next to the telemetry config so ``forget()``
# can wipe both. Resolved lazily (below) from ``telemetry.CONFIG_DIR`` so tests
# that redirect the config dir are honored and there is no import cycle.
STORAGE_FILENAME = "crash-pending.json"

# Bound the in-memory + persisted crash set so a pathological app that raises a
# unique exception class on every frame can never grow memory/storage without
# limit. Distinct crash SIGNATURES (not crash frequency) are what counts.
MAX_CRASHES = 200
# Server caps a batch at 50 reports (CrashBatch.maxItems).
MAX_BATCH = 50
# Server caps a single signature's occurrences at 100000.
MAX_OCCURRENCES = 100_000
# Server caps detailed frames at 20 (CrashReport.frames.maxItems).
MAX_FRAMES = 20

# Closed enum shared with the OpenAPI CrashReport schema. Python only ever emits
# "uncaught" (excepthook / threading.excepthook) and "render" (the caught
# near-miss path :func:`report_render_error`). "unhandledRejection" has no direct
# Python analog, so it is never produced here — it is listed only so restored
# rows written by another runtime version validate.
CRASH_KINDS = ("uncaught", "unhandledRejection", "render")

# Network submitter wired by the telemetry client (-> ``_submit_crashes``).
# Registered on ``enable_telemetry``, cleared on ``forget()``. Takes the batch
# and returns the number of reports the server accepted.
CrashSubmitter = Callable[[list[dict[str, Any]]], int]

# ─── Module state ─────────────────────────────────────────────────────────────

_lock = threading.RLock()
_pending: dict[str, dict[str, Any]] = {}
_submitter: CrashSubmitter | None = None
_active = False
_detailed = False
_restored_once = False
_flushing = False
_prev_excepthook: Any = None
_prev_threading_excepthook: Any = None
_hooks_installed = False
# Count of crashes captured this session (uncaught / render). Read by the
# crashFree meter axis; zeroed on forget()/reset. A plain counter derived from
# the existing crash hook — no new collection.
_crash_total = 0


def _pending_path():
    """Path to the on-disk pending-crash store.

    Resolved from ``telemetry.CONFIG_DIR`` at call time (lazy import to avoid an
    import cycle — telemetry imports this module) so a test that monkeypatches
    ``telemetry.CONFIG_DIR`` redirects the store too.
    """
    from boosthis import telemetry

    return telemetry.CONFIG_DIR / STORAGE_FILENAME


def set_crash_submitter(submitter: CrashSubmitter | None) -> None:
    """Register the crash auto-submitter. Pass ``None`` to clear (on opt-out)."""
    global _submitter
    with _lock:
        _submitter = submitter


# ─── Redaction helpers ────────────────────────────────────────────────────────


def _hash(value: str) -> str:
    """Cheap stable djb2 hash -> base36. Input is already redacted/non-reversible
    content; the output only GROUPS identical crash classes and carries no
    recoverable user data."""
    h = 5381
    for ch in value:
        h = ((h * 33) ^ ord(ch)) & 0xFFFFFFFF
    # base36 of the 32-bit unsigned hash.
    if h == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    n = h
    while n > 0:
        out.append(digits[n % 36])
        n //= 36
    return "".join(reversed(out))


def bucket_count(n: int) -> str:
    """Coarse, privacy-safe occurrence bucket. Always <= 12 chars (server cap)."""
    if n <= 1:
        return "1"
    if n <= 5:
        return "2-5"
    if n <= 20:
        return "6-20"
    if n <= 100:
        return "21-100"
    return "100+"


def sanitize_error_name(name: str) -> str:
    """Keep only identifier characters so an exception NAME can never carry an
    email/path/URL/value. Class names are code-defined; anything else collapses
    to ``Error``."""
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "_$")[:80]
    return cleaned if cleaned else "Error"


def file_basename(raw: str) -> str:
    """Reduce a file path/URL to its bare basename, dropping directories, URL
    scheme/host, query strings, and fragments."""
    s = raw
    for sep in ("?", "#"):
        idx = s.find(sep)
        if idx >= 0:
            s = s[:idx]
    # Normalize both separators, take the last segment.
    s = s.replace("\\", "/")
    s = s.rsplit("/", 1)[-1]
    s = s.strip()[:120]
    return s if s else "<unknown>"


def _clamp_int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    return min(n, 100_000_000)


def parse_stack(tb: Any) -> list[dict[str, Any]]:
    """Parse a traceback into redacted frames, TOP frame first (the deepest /
    raise site, mirroring RN's ``stack[0]``). File paths are reduced to
    basenames here so no absolute path ever survives parsing."""
    if tb is None:
        return []
    frames: list[dict[str, Any]] = []
    try:
        # extract_tb yields outermost -> innermost; reverse so index 0 is the
        # deepest frame (where the exception was raised), matching RN.
        summaries = list(traceback.extract_tb(tb))
    except Exception:  # noqa: BLE001
        return []
    for fs in reversed(summaries):
        func = fs.name or "<anonymous>"
        func = "".join(
            ch for ch in func if ch.isalnum() or ch in "_$.<> "
        ).strip()[:120]
        if not func:
            func = "<anonymous>"
        line_no = _clamp_int(fs.lineno)
        frame: dict[str, Any] = {
            "func": func,
            "file": file_basename(fs.filename or ""),
        }
        if line_no is not None:
            frame["line"] = line_no
        frames.append(frame)
        if len(frames) >= MAX_FRAMES:
            break
    return frames


def _format_redacted_frame(top: dict[str, Any] | None) -> str:
    if not top:
        return "<unknown>"
    line = top.get("line")
    suffix = f":{line}" if line is not None else ""
    return f"{top['func']} ({top['file']}{suffix})"[:160]


def sanitize_summary(message: str) -> str | None:
    """First line of the exception message, scrubbed: dropped entirely if it
    trips the PII guard (email/JWT/bearer/IP/phone). Returns ``None`` when there
    is nothing safe to keep."""
    if not message:
        return None
    first = message.splitlines()[0].strip()[:300] if message.splitlines() else ""
    if not first:
        return None
    if check_no_pii(first) is not None:
        return None
    return first


def redact_error(exc: Any, kind: str, tb: Any = None) -> dict[str, Any]:
    """Turn a raw exception into the closed, PII-safe crash shape. Detailed
    fields are added only when :data:`_detailed` is on and they pass the PII
    guard. NEVER derives the signature from the raw message/args."""
    if isinstance(exc, BaseException):
        name = type(exc).__name__
        message = str(exc)
        raw_tb = tb if tb is not None else exc.__traceback__
    else:
        name = "Error"
        message = exc if isinstance(exc, str) else str(exc)
        raw_tb = tb
    error_name = sanitize_error_name(name)
    frames = parse_stack(raw_tb)
    top = frames[0] if frames else None
    redacted_frame = _format_redacted_frame(top)
    # The signature is ALWAYS sent (even in default mode), so its basis must
    # never contain user values. A redacted top frame is code-defined (function
    # + file basename + line) and safe; when there is NO frame (stackless throw)
    # we fall back to ONLY code-defined tokens — the sanitized error name, the
    # crash kind, and a constant marker — and NEVER the raw message, which could
    # carry an email/token/user value that would otherwise leave as a hash.
    basis = redacted_frame if top else f"{error_name}|{kind}|<no-frame>"
    signature = f"{error_name}:{_hash(basis)}"[:120]
    out: dict[str, Any] = {
        "signature": signature,
        "errorName": error_name,
        "kind": kind,
        "redactedFrame": redacted_frame,
    }
    if _detailed:
        summary = sanitize_summary(message)
        if summary:
            out["summary"] = summary
        safe_frames = []
        for f in frames[:MAX_FRAMES]:
            if check_no_pii(f) is not None:
                continue
            fr: dict[str, Any] = {"func": f["func"], "file": f["file"]}
            if f.get("line") is not None:
                fr["line"] = f["line"]
            safe_frames.append(fr)
        if safe_frames:
            out["frames"] = safe_frames
    return out


# ─── Capture + flush ──────────────────────────────────────────────────────────


def capture(exc: Any, kind: str, tb: Any = None) -> None:
    """Record one crash. Never throws (fully guarded). No-op until
    :func:`install_crash_handlers` has run, so the reporter has zero effect on
    apps that never enabled telemetry, and no-op under ``BOOSTHIS_DISABLED``."""
    global _crash_total
    try:
        if not _active or is_boosthis_disabled():
            return
        _crash_total += 1
        r = redact_error(exc, kind, tb)
        now = int(_now_ms())
        # WHICH ACTION this crash happened in. Read from the kit's own
        # context-local scope — the same trace/span pair the spans are
        # correlated by — so it can only ever name work this context is
        # genuinely inside. A crash on a worker thread, in a CLI run, or
        # anywhere outside a measured request returns None and is filed as
        # "no action recorded": the in-flight request is NOT consulted,
        # because the nearest request is not evidence of causality.
        action = None
        try:
            from boosthis.span_scope import current_action

            action = current_action()
        except Exception:  # noqa: BLE001
            action = None  # instrumentation must never disturb the host app.
        with _lock:
            existing = _pending.get(r["signature"])
            if existing is not None:
                existing["sessionTotal"] = min(
                    existing["sessionTotal"] + 1, MAX_OCCURRENCES
                )
                existing["unsent"] = min(existing["unsent"] + 1, MAX_OCCURRENCES)
                existing["kind"] = r["kind"]
                existing["errorName"] = r["errorName"]
                existing["redactedFrame"] = r["redactedFrame"]
                existing["lastSeen"] = now
                if "summary" in r:
                    existing["summary"] = r["summary"]
                if "frames" in r:
                    existing["frames"] = r["frames"]
                # A later occurrence that DOES know its action names it; one
                # that does not leaves the last known action alone rather
                # than wiping it. Same rule the server applies when it merges
                # repeat reports of one signature.
                if action is not None:
                    existing["traceId"] = action.trace_id
                    existing["spanId"] = action.span_id
            else:
                _pending[r["signature"]] = {
                    "signature": r["signature"],
                    "errorName": r["errorName"],
                    "kind": r["kind"],
                    "redactedFrame": r["redactedFrame"],
                    "summary": r.get("summary"),
                    "frames": r.get("frames"),
                    "sessionTotal": 1,
                    "unsent": 1,
                    "lastSeen": now,
                    "traceId": action.trace_id if action is not None else None,
                    "spanId": action.span_id if action is not None else None,
                }
                _evict_if_needed()
        # Persist SYNCHRONOUSLY so a FATAL crash that kills the process before
        # the network flush completes is still reported on the next launch.
        _persist()
        _schedule_flush()
    except Exception:  # noqa: BLE001
        # The reporter must NEVER crash the host.
        pass


def report_render_error(exc: Any) -> None:
    """Public entry for a caught render/near-miss throw (tagged ``render``).

    This is the ONLY path that emits ``kind: "render"`` — a caught exception the
    host hands us on purpose, not an uncaught crash. Silent no-op when telemetry
    was never enabled."""
    capture(exc, "render")


def _now_ms() -> float:
    import time

    return time.time() * 1000


def _evict_if_needed() -> None:
    """Keep the crash set bounded: evict the least-recently-seen entries that
    have nothing left to send; if everything still has unsent work, drop the
    oldest regardless to honor the cap. Caller holds ``_lock``."""
    if len(_pending) <= MAX_CRASHES:
        return
    ordered = sorted(_pending.values(), key=lambda e: e["lastSeen"])
    for e in ordered:
        if len(_pending) <= MAX_CRASHES:
            break
        if e["unsent"] <= 0:
            _pending.pop(e["signature"], None)
    idx = 0
    while len(_pending) > MAX_CRASHES and idx < len(ordered):
        _pending.pop(ordered[idx]["signature"], None)
        idx += 1


def build_batch() -> list[dict[str, Any]]:
    """Assemble the outbound batch (CrashReport wire shape, camelCase keys).
    Only entries with unsent occurrences are included, capped at
    :data:`MAX_BATCH`."""
    batch: list[dict[str, Any]] = []
    with _lock:
        for e in _pending.values():
            if e["unsent"] <= 0:
                continue
            item: dict[str, Any] = {
                "signature": e["signature"],
                "errorName": e["errorName"],
                "kind": e["kind"],
                "redactedFrame": e["redactedFrame"],
                "countBucket": bucket_count(e["sessionTotal"]),
                "occurrences": min(e["unsent"], MAX_OCCURRENCES),
            }
            if e.get("summary") is not None:
                item["summary"] = e["summary"]
            if e.get("frames") is not None:
                item["frames"] = e["frames"]
            # WHICH ACTION this crash happened in, when the kit held one.
            # Added only when present: a crash that could not be placed sends
            # exactly the keys it always did, and the server reads that as
            # "no action recorded" rather than inventing one.
            if e.get("traceId") is not None:
                item["traceId"] = e["traceId"]
            if e.get("spanId") is not None:
                item["spanId"] = e["spanId"]
            batch.append(item)
            if len(batch) >= MAX_BATCH:
                break
    return batch


def _apply_sent(batch: list[dict[str, Any]]) -> None:
    """Subtract only what was sent; crashes that arrived while the request was in
    flight remain queued for the next flush. Caller need not hold ``_lock``."""
    with _lock:
        for sent in batch:
            cur = _pending.get(sent["signature"])
            if cur is None:
                continue
            cur["unsent"] = max(0, cur["unsent"] - sent["occurrences"])


def flush() -> None:
    """Flush pending crashes through the registered submitter. Always-on for
    registered apps (not gated by enable/disable) — only ``BOOSTHIS_DISABLED``,
    ``forget()``, or a missing submitter stop it. Never throws."""
    global _flushing
    with _lock:
        if _flushing:
            return
        submitter = _submitter
        if submitter is None or is_boosthis_disabled() or not _pending:
            return
        _flushing = True
    try:
        batch = build_batch()
        if not batch:
            return
        accepted = 0
        try:
            accepted = int(submitter(batch))
        except Exception:  # noqa: BLE001
            accepted = 0
        if accepted > 0:
            _apply_sent(batch)
            _persist()
    finally:
        with _lock:
            _flushing = False


def _schedule_flush() -> None:
    """Spawn a one-shot daemon thread to flush (the snapshot_mirror idiom).
    Skipped under the kill-switch or in tests (where flush is called directly)."""
    if is_boosthis_disabled() or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    with _lock:
        if _submitter is None or not _pending:
            return
    try:
        threading.Thread(target=flush, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


# ─── Persistence ──────────────────────────────────────────────────────────────


def _persist() -> None:
    """Write the pending crash set to disk. Best-effort — never throws."""
    try:
        with _lock:
            entries = list(_pending.values())[:MAX_CRASHES]
        path = _pending_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Atomic publish — see _write_config in telemetry.py. This file is
        # written from a dying process, and every worker of the app writes it,
        # so a torn file here is not a theoretical risk: it would read back as
        # "no crashes at all".
        tmp_path = f"{path}.tmp-{os.getpid()}"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(entries).encode("utf-8"))
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        try:
            os.replace(tmp_path, path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:  # noqa: BLE001
        # A failed write just means a fatal crash before the next successful
        # flush may go unreported. Never throws.
        pass


def _restore() -> None:
    """Load crashes persisted from a previous (possibly fatal) session. Runs at
    most once per process. Corrupt/absent store starts clean."""
    global _restored_once
    if _restored_once:
        return
    _restored_once = True
    try:
        path = _pending_path()
        if not path.exists():
            return
        parsed = json.loads(path.read_text())
        if not isinstance(parsed, list):
            return
        with _lock:
            for c in parsed:
                if not isinstance(c, dict):
                    continue
                if (
                    isinstance(c.get("signature"), str)
                    and isinstance(c.get("errorName"), str)
                    and isinstance(c.get("redactedFrame"), str)
                    and c.get("kind") in CRASH_KINDS
                    and isinstance(c.get("sessionTotal"), int)
                    and isinstance(c.get("unsent"), int)
                ):
                    if c["signature"] in _pending:
                        continue
                    _pending[c["signature"]] = {
                        "signature": c["signature"],
                        "errorName": c["errorName"],
                        "kind": c["kind"],
                        "redactedFrame": c["redactedFrame"],
                        "summary": c.get("summary")
                        if isinstance(c.get("summary"), str)
                        else None,
                        "frames": c.get("frames")
                        if isinstance(c.get("frames"), list)
                        else None,
                        # The action the previous process placed this crash
                        # in, carried back with it — a restore that dropped
                        # these would turn a crash that knew its action into
                        # one that never did.
                        "traceId": c.get("traceId")
                        if isinstance(c.get("traceId"), str)
                        else None,
                        "spanId": c.get("spanId")
                        if isinstance(c.get("spanId"), str)
                        else None,
                        "sessionTotal": c["sessionTotal"],
                        "unsent": c["unsent"],
                        "lastSeen": c.get("lastSeen")
                        if isinstance(c.get("lastSeen"), int)
                        else int(_now_ms()),
                    }
            _evict_if_needed()
    except Exception:  # noqa: BLE001
        pass


# ─── Install / uninstall ──────────────────────────────────────────────────────


def _excepthook_wrapper(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
    """Chained ``sys.excepthook``: capture (fully guarded) then ALWAYS hand off
    to the host's previous hook in ``finally`` so the host's own crash flow is
    untouched."""
    try:
        capture(exc_value, "uncaught", exc_tb)
    except Exception:  # noqa: BLE001
        pass
    finally:
        prev = _prev_excepthook
        if callable(prev) and prev is not _excepthook_wrapper:
            prev(exc_type, exc_value, exc_tb)


def _threading_excepthook_wrapper(args: Any) -> None:
    """Chained ``threading.excepthook``: same store-previous / always-call
    contract as the main-thread hook."""
    try:
        capture(
            getattr(args, "exc_value", None),
            "uncaught",
            getattr(args, "exc_traceback", None),
        )
    except Exception:  # noqa: BLE001
        pass
    finally:
        prev = _prev_threading_excepthook
        if callable(prev) and prev is not _threading_excepthook_wrapper:
            prev(args)


def install_crash_handlers(detailed: bool = False) -> None:
    """Install the chained crash handlers. Called once from ``enable_telemetry``.

    Idempotent: calling again only updates ``detailed`` and never re-chains the
    hooks (which would otherwise double-report or lose the host's original
    handler). Restores any crashes persisted from a previous session and
    schedules a flush now that a submitter is registered. Fully guarded — a bug
    here can never crash the host."""
    global _active, _detailed, _prev_excepthook, _prev_threading_excepthook
    global _hooks_installed
    try:
        _detailed = detailed is True
        if _active:
            return
        _active = True

        if not _hooks_installed:
            # Store the host's current hooks and ALWAYS call them from our
            # wrappers so we never swallow a crash or clobber host behavior.
            _prev_excepthook = sys.excepthook
            sys.excepthook = _excepthook_wrapper
            _prev_threading_excepthook = threading.excepthook
            threading.excepthook = _threading_excepthook_wrapper
            _hooks_installed = True

        _restore()
        _schedule_flush()
    except Exception:  # noqa: BLE001
        pass


def uninstall_crash_handlers() -> None:
    """Restore the host's ORIGINAL hooks, clear all pending crash state, and
    delete the persisted store. Called by ``telemetry.forget()`` so nothing
    Boosthis-shaped is left on the host."""
    global _active, _detailed, _prev_excepthook, _prev_threading_excepthook
    global _hooks_installed
    _active = False
    _detailed = False
    try:
        if _hooks_installed:
            if callable(_prev_excepthook):
                sys.excepthook = _prev_excepthook
            if callable(_prev_threading_excepthook):
                threading.excepthook = _prev_threading_excepthook
        _prev_excepthook = None
        _prev_threading_excepthook = None
        _hooks_installed = False
    except Exception:  # noqa: BLE001
        pass
    global _crash_total
    _crash_total = 0
    with _lock:
        _pending.clear()
    try:
        _pending_path().unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass


# ─── Test/debug helpers ───────────────────────────────────────────────────────


def is_active() -> bool:
    return _active


def is_detailed() -> bool:
    return _detailed


def pending_size() -> int:
    with _lock:
        return len(_pending)


def get_pending() -> list[dict[str, Any]]:
    with _lock:
        return [dict(e) for e in _pending.values()]


def _reset_for_tests() -> None:
    """Clear all module state between hermetic test runs (restoring hooks)."""
    global _submitter, _active, _detailed, _restored_once, _flushing
    global _prev_excepthook, _prev_threading_excepthook, _hooks_installed
    global _crash_total
    if _hooks_installed:
        try:
            if callable(_prev_excepthook):
                sys.excepthook = _prev_excepthook
            if callable(_prev_threading_excepthook):
                threading.excepthook = _prev_threading_excepthook
        except Exception:  # noqa: BLE001
            pass
    with _lock:
        _pending.clear()
    _submitter = None
    _active = False
    _detailed = False
    _restored_once = False
    _flushing = False
    _prev_excepthook = None
    _prev_threading_excepthook = None
    _hooks_installed = False
    _crash_total = 0


def crash_count() -> int:
    """Crashes captured since telemetry started (crashFree axis input)."""
    return _crash_total


def _set_crash_count_for_tests(n: int) -> None:
    global _crash_total
    _crash_total = n


def _set_active_for_tests(value: bool) -> None:
    global _active
    _active = value


def _set_detailed_for_tests(value: bool) -> None:
    global _detailed
    _detailed = value


__all__ = [
    "CrashSubmitter",
    "set_crash_submitter",
    "install_crash_handlers",
    "uninstall_crash_handlers",
    "report_render_error",
    "capture",
    "flush",
    "build_batch",
    "redact_error",
    "crash_count",
]
