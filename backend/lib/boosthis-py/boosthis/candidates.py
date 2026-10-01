"""Candidate rules — Python mirror of ``candidateRules.ts``.

A "candidate rule" is a recurring perf pattern Boosthis has observed but
which does not map to any rule in the shipped checklist. When the same
privacy-safe signature is seen ``MIN_OCCURRENCES_TO_SURFACE`` times
locally and telemetry is on, the runtime auto-submits the signature to
the Boosthis server — no user action required, no screen names, no
values, no code, no traces. Just ``<kind>:<sev>:<countBucket>``.

If telemetry is off (default), the candidate stays on disk only and
nothing leaves the process.

This module also reports the other half of the story: FIX OUTCOMES. When a
kind that was firing at a worse severity is later seen only at a better one,
somebody fixed it, and the kit says so — the rule kind plus the bucketed
before -> after rating, nothing else. Both halves follow
``docs/kit-problem-reporting-contract.md``, and every kind reported comes from
the shared vocabulary in ``boosthis.problem_kinds``.
"""

from __future__ import annotations

import errno
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from time import monotonic, sleep, time
from typing import Any, Iterable

from boosthis.problem_kinds import is_problem_kind
from boosthis.runtime_flags import is_boosthis_disabled

CANDIDATES_PATH = Path.home() / ".boosthis" / "candidates.json"
#: Per-kind worst severity ever observed — the only thing needed to notice that
#: a problem later got better. A handful of kinds mapped to a bucket; no user
#: data, no route names, no values.
BASELINES_PATH = Path.home() / ".boosthis" / "rule-baselines.json"
MAX_CANDIDATES = 100
MIN_OCCURRENCES_TO_SURFACE = 2

# Severity = where the p95 lands. Same buckets as JS runtime.
_SEV_LOW_MAX_MS = 100
_SEV_MED_MAX_MS = 500


def _bucket_severity(p95_ms: float) -> str:
    if p95_ms <= _SEV_LOW_MAX_MS:
        return "low"
    if p95_ms <= _SEV_MED_MAX_MS:
        return "med"
    return "high"


def _bucket_count(count: int) -> str:
    if count < 3:
        return "<3"
    if count < 10:
        return "<10"
    if count < 50:
        return "<50"
    return "50+"


# Severity ordering + the rating words the fix-outcome channel speaks. Same
# vocabulary as the JS runtimes so a fix proven in Python and the same fix
# proven in Node are one row, not two.
_SEV_RANK: dict[str, int] = {"low": 0, "med": 1, "high": 2}


def _severity_to_rating(sev: str) -> str:
    if sev == "high":
        return "poor"
    if sev == "med":
        return "needs-work"
    return "good"


@dataclass
class Finding:
    """Detector output — caller-supplied. ``name`` is intentionally
    dropped from the signature; only ``kind`` + ``p95`` + ``count``
    influence what's submitted."""

    kind: str
    name: str
    p95: float
    count: int
    hint: str = ""


@dataclass
class CandidateRule:
    id: str
    signature: str
    kind: str
    severity_bucket: str
    count_bucket: str
    occurrences: int
    first_seen_at: float
    last_seen_at: float
    example_hint: str = ""
    status: str = "new"  # "new" | "promoted-local" | "submitted"

    def to_dict(self) -> dict:
        return asdict(self)


def signature_for(f: Finding) -> str:
    """Privacy-safe fingerprint. Mirrors `signatureFor` in JS."""
    return f"{f.kind}:{_bucket_severity(f.p95)}:{_bucket_count(f.count)}"


def _safe_hash_id(input_str: str) -> str:
    h = 5381
    for ch in input_str:
        h = ((h * 33) ^ ord(ch)) & 0xFFFFFFFF
    return "cr_" + format(h, "x")


def _read_all() -> list[CandidateRule]:
    if not CANDIDATES_PATH.exists():
        return []
    try:
        raw = json.loads(CANDIDATES_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[CandidateRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                CandidateRule(
                    id=str(item["id"]),
                    signature=str(item["signature"]),
                    kind=str(item["kind"]),
                    severity_bucket=str(item["severity_bucket"]),
                    count_bucket=str(item["count_bucket"]),
                    occurrences=int(item["occurrences"]),
                    first_seen_at=float(item["first_seen_at"]),
                    last_seen_at=float(item["last_seen_at"]),
                    example_hint=str(item.get("example_hint", "")),
                    status=str(item.get("status", "new")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _write_all(items: list[CandidateRule]) -> None:
    try:
        CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
        trimmed = items[:MAX_CANDIDATES]
        CANDIDATES_PATH.write_text(
            json.dumps([c.to_dict() for c in trimmed], indent=2) + "\n"
        )
    except OSError:
        # Best-effort, never raises out to the caller.
        pass


# Network submitter, registered by ``boosthis.telemetry`` when the user
# opts in. When telemetry is off, no submitter is registered and
# nothing leaves the process.
_active_submitter = None  # type: ignore[var-annotated]


def set_candidate_submitter(submitter) -> None:  # type: ignore[no-untyped-def]
    """Register an auto-submitter. Pass ``None`` to clear (opt-out)."""
    global _active_submitter
    _active_submitter = submitter


# Network submitter for FIX OUTCOMES — "we said this was wrong, and it got
# better". Wired by ``boosthis.telemetry`` exactly like the candidate submitter,
# and carrying only the rule kind plus the bucketed before -> after rating.
# Never the code, the diff, a route name or a raw value.
_active_resolution_submitter = None  # type: ignore[var-annotated]


def set_resolution_submitter(submitter) -> None:  # type: ignore[no-untyped-def]
    """Register the fix-outcome auto-submitter. Pass ``None`` to clear."""
    global _active_resolution_submitter
    _active_resolution_submitter = submitter


def _read_baselines() -> dict[str, str]:
    """Worst severity ever seen, per kind. Safe — ``{}`` on any error."""
    try:
        raw = json.loads(BASELINES_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in raw.items()
        if isinstance(v, str) and v in _SEV_RANK
    }


def _write_baselines(baselines: dict[str, str]) -> None:
    try:
        BASELINES_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINES_PATH.write_text(json.dumps(baselines, indent=2) + "\n")
    except OSError:
        # Best-effort, same policy as the candidate store.
        pass


def list_candidate_rules() -> list[CandidateRule]:
    return _read_all()


def list_surfaceable(all_items: Iterable[CandidateRule]) -> list[CandidateRule]:
    return [c for c in all_items if c.occurrences >= MIN_OCCURRENCES_TO_SURFACE]


# Polling-safe dedupe — same idea as the JS runtime. If two consecutive
# ingest calls describe the same set of findings (same kind+name+p95+
# count), they're treated as a single observation.
_last_ingest_key: str | None = None


def _reset_ingest_dedupe_for_tests() -> None:
    global _last_ingest_key
    _last_ingest_key = None


def _fingerprint(findings: list[Finding]) -> str:
    parts = sorted(
        f"{f.kind}|{f.name}|{round(f.p95)}|{f.count}" for f in findings
    )
    return "\n".join(parts)


def ingest_findings(findings: list[Finding]) -> list[CandidateRule]:
    """Ingest the latest detector findings: increment counts for known
    signatures, create new candidates for unseen ones, persist. If
    telemetry is on (submitter registered), auto-submit any signature
    that just crossed the local threshold.

    Honors the kill-switch: when ``BOOSTHIS_DISABLED=1`` we don't even
    read or write the candidate store.
    """
    if is_boosthis_disabled():
        return list_surfaceable(_read_all())

    # Only the shared vocabulary travels. A kind nobody else spells the same way
    # could never group with the same problem found by another language, so it
    # is dropped here rather than accumulated and uploaded. Dropping (not
    # raising) is deliberate: bookkeeping must never break the host. The build
    # guard in scripts/src/__tests__/kitProblemReporting.test.ts is what stops an
    # off-list detector going unnoticed. See
    # docs/kit-problem-reporting-contract.md.
    findings = [f for f in findings if is_problem_kind(f.kind)]
    if not findings:
        return list_surfaceable(_read_all())

    global _last_ingest_key
    key = _fingerprint(findings)
    if key == _last_ingest_key:
        return list_surfaceable(_read_all())
    _last_ingest_key = key

    now = time()
    existing = _read_all()
    by_id: dict[str, CandidateRule] = {c.id: c for c in existing}

    for f in findings:
        sig = signature_for(f)
        cid = _safe_hash_id(sig)
        prev = by_id.get(cid)
        if prev:
            prev.occurrences += 1
            prev.last_seen_at = now
        else:
            by_id[cid] = CandidateRule(
                id=cid,
                signature=sig,
                kind=f.kind,
                severity_bucket=_bucket_severity(f.p95),
                count_bucket=_bucket_count(f.count),
                occurrences=1,
                first_seen_at=now,
                last_seen_at=now,
                example_hint=f.hint,
                status="new",
            )

    merged = sorted(by_id.values(), key=lambda c: c.last_seen_at, reverse=True)
    _write_all(merged)

    # ── Fix-outcome detection ────────────────────────────────────────
    # A kind observed at a BETTER severity than the worst ever recorded for it
    # means somebody fixed something. Report the kind plus the bucketed
    # before -> after rating, and the circumstances the problem held before the
    # fix, so the matcher can weight a proven fix toward similar projects.
    # Mirrors ``candidateRules.ts``.
    baselines = _read_baselines()
    baselines_changed = False
    current_worst: dict[str, str] = {}
    current_count: dict[str, int] = {}
    for f in findings:
        sev = _bucket_severity(f.p95)
        prev_sev = current_worst.get(f.kind)
        if prev_sev is None or _SEV_RANK[sev] > _SEV_RANK[prev_sev]:
            current_worst[f.kind] = sev
        current_count[f.kind] = max(current_count.get(f.kind, 0), f.count)
    resolved: list[dict[str, Any]] = []
    for kind, sev in current_worst.items():
        worst = baselines.get(kind)
        if worst and _SEV_RANK[sev] < _SEV_RANK[worst]:
            # Improvement — the baseline is NOT lowered until the server has
            # taken it, so a failed upload retries instead of losing the fix.
            resolved.append(
                {
                    "ruleId": kind,
                    "kind": kind,
                    "beforeRating": _severity_to_rating(worst),
                    "afterRating": _severity_to_rating(sev),
                    "occurrences": 1,
                    "severityBucket": worst,
                    "countBucket": _bucket_count(current_count.get(kind, 0)),
                }
            )
        elif not worst or _SEV_RANK[sev] > _SEV_RANK[worst]:
            # New or worsened — record/raise the worst-ever baseline now.
            baselines[kind] = sev
            baselines_changed = True
    if resolved and _active_resolution_submitter is not None:
        try:
            accepted = _active_resolution_submitter(resolved)
        except Exception:  # noqa: BLE001
            # Best-effort. Baseline untouched so the improvement retries.
            accepted = 0
        if isinstance(accepted, int) and accepted > 0:
            for r in resolved[:accepted]:
                baselines[str(r["ruleId"])] = current_worst[str(r["ruleId"])]
                baselines_changed = True
    if baselines_changed:
        _write_baselines(baselines)

    # Auto-submit any surfaceable candidate still marked `new`.
    if _active_submitter is not None:
        to_submit = [c for c in list_surfaceable(merged) if c.status == "new"]
        if to_submit:
            payload = [
                {
                    "signature": c.signature,
                    "kind": c.kind,
                    "severityBucket": c.severity_bucket,
                    "countBucket": c.count_bucket,
                    "occurrences": c.occurrences,
                }
                for c in to_submit
            ]
            try:
                accepted = _active_submitter(payload)
            except Exception:  # noqa: BLE001
                accepted = 0
            if isinstance(accepted, int) and accepted > 0:
                submitted_ids = {c.id for c in to_submit[:accepted]}
                for c in merged:
                    if c.id in submitted_ids:
                        c.status = "submitted"
                _write_all(merged)

    return list_surfaceable(merged)


def promote_candidate_local(candidate_id: str) -> None:
    all_items = _read_all()
    for c in all_items:
        if c.id == candidate_id:
            c.status = "promoted-local"
    _write_all(all_items)


def clear_all_candidates() -> None:
    """Wipe the candidate store, the severity baselines AND the remembered
    findings, so erasure leaves nothing Boosthis-shaped behind (mirrors
    ``clearAllCandidates`` in JS)."""
    global _recurrence_mirror
    for path in (CANDIDATES_PATH, BASELINES_PATH, recurrence_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    # Forgetting is not "we looked and there is nothing": the next reading
    # says nothing at all until this process has looked again.
    _recurrence_mirror = None


# ── What this app keeps doing, across restarts ──────────────────────────────
#
# Python mirror of the recurrence half of ``candidateRules.ts``. A finding
# computed in the process and forgotten when the process ends arrives fresh on
# every deploy, and nothing can tell a one-off from the fourth release running.
# The measurement is already taken; what was missing was the memory of having
# taken it before.
#
# WHAT IS REMEMBERED: the finding's KIND and counts. Never a prompt, a host, a
# path, a model or the release string — the release is folded into a local hash
# and a local ordinal that never leave the process. What leaves is how many
# separate runs, how many separate releases, and how long ago.

#: Beside the candidate list, under the same directory, wiped by the same
#: erase path — but SCOPED TO THIS APPLICATION.
#:
#: Several Boosthis-instrumented Python services routinely share one machine
#: account, and a machine-wide file would blend their histories: two apps
#: would count each other's runs and releases, and each would report the
#: other's AI anti-patterns as its own. The scope key is the one every meter
#: that keeps a file already uses (``app_scope``) — an opaque digest, so no
#: path, user or install id is written in readable form. The Node kit's store
#: is app-scoped by the same rule.
def recurrence_path() -> Path:
    """Where THIS application's remembered findings live."""
    from boosthis import app_scope

    return app_scope.scoped_path("finding-recurrence")
#: Bounded like the candidate list. There are six AI kinds today; the ceiling
#: is what stops a future vocabulary growing the file without limit.
MAX_REMEMBERED_PATTERNS = 24
_MAX_RECENT_RELEASES = 32
_SECONDS_PER_DAY = 86_400.0
#: How stale "last seen" may get before a capture that saw nothing new is
#: still worth a write. Without it a pattern firing on every capture rewrites
#: the file on every capture.
_RECURRENCE_TOUCH_S = 60.0

#: Minted once per process. A restart is a new run; nothing else is.
_RUN_ID = _safe_hash_id(f"{time()}:{id(object())}")
_run_id_override: str | None = None
#: Last record read or written, kept in memory so the snapshot can read it
#: without a disk round trip. ``None`` means this process has not looked yet,
#: which is reported as silence and not as "nothing recurs".
_recurrence_mirror: dict[str, Any] | None = None
#: Flipped off the first time the store cannot be written. A kit that cannot
#: remember says so rather than letting a repeat read like a first sighting.
_recurrence_durable = True


def _current_run_id() -> str:
    return _run_id_override if _run_id_override is not None else _RUN_ID


def is_recurrence_kind(kind: str) -> bool:
    """Only the kinds this channel is for — shared vocabulary, AI half."""
    return is_problem_kind(kind) and kind.startswith("ai-")


def recurrence_memory_durable() -> bool:
    """Whether this kit has somewhere to remember what it has seen."""
    return _recurrence_durable


def _empty_recurrence() -> dict[str, Any]:
    return {"releaseHash": "", "releaseOrdinal": 0, "releaseHashes": [], "patterns": []}


#: How long the recurrence bookkeeping waits for a sibling worker's lock
#: before skipping this pass. A publish is a read plus a rename — far below
#: this — so reaching it means a stalled holder, and neither a reading nor a
#: shutdown flush may wait on one. Matches the Node kit's store.
_RECURRENCE_LOCK_WAIT_S = 0.5


class _RecurrenceLockBusy(Exception):
    """A sibling held the lock for the whole budget; this pass is skipped."""


def _try_lock(lock_fd: int) -> bool:
    """One non-blocking attempt. False means a sibling holds it; any other
    failure raises, because waiting cannot fix it."""
    if os.name == "nt":
        import msvcrt

        os.lseek(lock_fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            # The CRT reports a region another handle holds as EACCES (older
            # runtimes: EDEADLOCK).
            if exc.errno in (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)):
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(lock_fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(lock_fd, 0, os.SEEK_SET)
        msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_fd, fcntl.LOCK_UN)


@contextmanager
def _recurrence_lock(path: Path, wait_s: float | None = None):
    """One lock for the whole disk read/modify/atomic-publish, including Windows.

    Bounded: acquisition polls without blocking and gives up at the end of the
    budget with ``_RecurrenceLockBusy``, so the caller skips the bookkeeping
    rather than hanging a reading or a shutdown flush behind a stalled holder.
    The descriptor is closed on every path, including a failed acquisition.
    """
    budget = _RECURRENCE_LOCK_WAIT_S if wait_s is None else max(0.0, wait_s)
    lock_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        deadline = monotonic() + budget
        while not _try_lock(lock_fd):
            if monotonic() >= deadline:
                raise _RecurrenceLockBusy()
            sleep(0.005)
        locked = True
        yield
    finally:
        try:
            if locked:
                _unlock(lock_fd)
        except OSError:
            pass  # closing the descriptor below releases it regardless
        finally:
            os.close(lock_fd)


def _read_recurrence() -> dict[str, Any]:
    try:
        raw = json.loads(recurrence_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_recurrence()
    if not isinstance(raw, dict):
        return _empty_recurrence()
    patterns: list[dict[str, Any]] = []
    for p in raw.get("patterns", []) if isinstance(raw.get("patterns"), list) else []:
        if not isinstance(p, dict):
            continue
        kind = p.get("kind")
        runs = p.get("runs")
        first = p.get("first_seen_at")
        if not isinstance(kind, str) or not is_recurrence_kind(kind):
            continue
        if not isinstance(runs, (int, float)) or not isinstance(first, (int, float)):
            continue
        worst = p.get("worst")
        patterns.append(
            {
                "kind": kind,
                "runs": max(1, int(runs)),
                "releases": max(0, int(p["releases"]))
                if isinstance(p.get("releases"), (int, float))
                else 0,
                "first_seen_at": float(first),
                "last_seen_at": float(p["last_seen_at"])
                if isinstance(p.get("last_seen_at"), (int, float))
                else float(first),
                "last_run": str(p.get("last_run", "")),
                "last_ordinal": max(0, int(p["last_ordinal"]))
                if isinstance(p.get("last_ordinal"), (int, float))
                else 0,
                "release_hashes": [
                    h for h in p["release_hashes"] if isinstance(h, str)
                ][-_MAX_RECENT_RELEASES:]
                if isinstance(p.get("release_hashes"), list)
                else [raw["releaseHash"]]
                if p.get("last_ordinal") == raw.get("releaseOrdinal")
                and isinstance(raw.get("releaseHash"), str) and raw["releaseHash"]
                else [],
                "worst": worst if worst in _SEV_RANK else "low",
            }
        )
    return {
        "releaseHash": str(raw.get("releaseHash", "")),
        "releaseOrdinal": max(0, int(raw["releaseOrdinal"]))
        if isinstance(raw.get("releaseOrdinal"), (int, float))
        else 0,
        "releaseHashes": [
            h for h in raw["releaseHashes"] if isinstance(h, str)
        ][-_MAX_RECENT_RELEASES:]
        if isinstance(raw.get("releaseHashes"), list)
        else [raw["releaseHash"]]
        if isinstance(raw.get("releaseHash"), str) and raw["releaseHash"]
        else [],
        "patterns": patterns,
    }


def _finding_field(f: Any, key: str, default: Any) -> Any:
    if isinstance(f, dict):
        value = f.get(key, default)
    else:
        value = getattr(f, key, default)
    return default if value is None else value


def note_recurring_findings(
    findings: Iterable[Any],
    release_key: str | None = None,
    now: float | None = None,
) -> None:
    """Remember the AI findings this capture produced.

    ``release_key`` is whatever identifies the running build to THIS process —
    a commit, a build time. It is hashed immediately and the hash never
    leaves; an empty or missing key means this run cannot name its release, so
    the release counts simply do not move. They are never incremented on a
    guess and never reported as zero releases.
    """
    global _recurrence_mirror, _recurrence_durable
    try:
        if is_boosthis_disabled():
            return
        at = time() if now is None else now
        path = recurrence_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _recurrence_lock(path):
            record = _read_recurrence()
            changed = False
            touched = False
            key = (release_key or "").strip()
            digest = _safe_hash_id(f"release:{key}") if key else ""
            if digest and digest not in record["releaseHashes"]:
                record["releaseHashes"] = (record["releaseHashes"] + [digest])[-_MAX_RECENT_RELEASES:]
                record["releaseHash"] = digest
                record["releaseOrdinal"] = int(record["releaseOrdinal"]) + 1
                changed = True
            ordinal = int(record["releaseOrdinal"]) if key else 0

            run = _current_run_id()
            by_kind = {p["kind"]: p for p in record["patterns"]}
            for f in findings:
                kind = _finding_field(f, "kind", "")
                if not isinstance(kind, str) or not is_recurrence_kind(kind):
                    continue
                sev = _bucket_severity(float(_finding_field(f, "p95", 0) or 0))
                prev = by_kind.get(kind)
                if prev is None:
                    by_kind[kind] = {
                        "kind": kind, "runs": 1,
                        "releases": 1 if ordinal > 0 else 0,
                        "first_seen_at": at, "last_seen_at": at,
                        "last_run": run, "last_ordinal": ordinal,
                        "release_hashes": [digest] if digest else [],
                        "worst": sev,
                    }
                    changed = True
                    continue
                if prev["last_run"] != run:
                    prev["runs"] = int(prev["runs"]) + 1
                    changed = True
                if ordinal > 0 and digest not in prev["release_hashes"]:
                    prev["releases"] = int(prev["releases"]) + 1
                    prev["release_hashes"] = (prev["release_hashes"] + [digest])[-_MAX_RECENT_RELEASES:]
                    changed = True
                if ordinal > 0:
                    prev["last_ordinal"] = ordinal
                if at - float(prev["last_seen_at"]) >= _RECURRENCE_TOUCH_S:
                    touched = True
                prev["last_run"] = run
                prev["last_seen_at"] = at
                if _SEV_RANK[sev] > _SEV_RANK[str(prev["worst"])]:
                    prev["worst"] = sev
                    changed = True

            record["patterns"] = sorted(
                by_kind.values(), key=lambda p: p["last_seen_at"], reverse=True
            )[:MAX_REMEMBERED_PATTERNS]
            if changed or touched:
                tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
                try:
                    with os.fdopen(os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as fh:
                        json.dump(record, fh, indent=2)
                        fh.write("\n")
                    os.replace(tmp, path)
                finally:
                    tmp.unlink(missing_ok=True)
            _recurrence_mirror = record
            _recurrence_durable = True
    except _RecurrenceLockBusy:
        # A sibling worker is mid-publish or stalled holding the lock. Skip
        # this pass whole — the mirror and the durability flag stay as they
        # were, because a busy neighbour is not a store that cannot remember.
        # Nothing counted is lost: a release, run or kind is counted when the
        # record does not hold it yet, so the next pass adds it then.
        return
    except Exception:  # noqa: BLE001
        # Bookkeeping never disturbs the host.
        _recurrence_durable = False


def read_recurrence_rows(now: float | None = None) -> list[dict[str, Any]] | None:
    """The remembered patterns, as counts, for the AI axis.

    ``None`` when this process has not read the store yet — silence, because
    "we have not looked" and "nothing recurs" are different answers.

    A pattern seen only in THIS run and never before says nothing the finding
    itself does not already say, so it is held back by the same threshold the
    candidate list uses. A pattern that has STOPPED is always worth a row.
    """
    record = _recurrence_mirror
    if record is None:
        return None
    at = time() if now is None else now
    run = _current_run_id()
    out: list[dict[str, Any]] = []
    for p in record["patterns"]:
        seen_now = 1 if p["last_run"] == run else 0
        if seen_now == 1 and int(p["runs"]) < MIN_OCCURRENCES_TO_SURFACE:
            continue
        row: dict[str, Any] = {
            "kind": p["kind"],
            "runs": int(p["runs"]),
            "seenNow": seen_now,
            "firstSeenDaysAgo": max(
                0, int((at - float(p["first_seen_at"])) // _SECONDS_PER_DAY)
            ),
            "lastSeenDaysAgo": max(
                0, int((at - float(p["last_seen_at"])) // _SECONDS_PER_DAY)
            ),
        }
        if int(p["releases"]) > 0:
            row["releases"] = int(p["releases"])
        if int(p["last_ordinal"]) > 0 and int(record["releaseOrdinal"]) > 0:
            row["releasesSince"] = max(
                0, int(record["releaseOrdinal"]) - int(p["last_ordinal"])
            )
        out.append(row)
    return out


def _set_run_id_for_tests(run_id: str | None) -> None:
    global _run_id_override
    _run_id_override = run_id


def _reset_recurrence_for_tests() -> None:
    global _recurrence_mirror, _recurrence_durable
    _recurrence_mirror = None
    _recurrence_durable = True


__all__ = [
    "BASELINES_PATH",
    "CANDIDATES_PATH",
    "recurrence_path",
    "MAX_CANDIDATES",
    "MAX_REMEMBERED_PATTERNS",
    "MIN_OCCURRENCES_TO_SURFACE",
    "is_recurrence_kind",
    "note_recurring_findings",
    "read_recurrence_rows",
    "recurrence_memory_durable",
    "Finding",
    "CandidateRule",
    "signature_for",
    "set_candidate_submitter",
    "set_resolution_submitter",
    "list_candidate_rules",
    "list_surfaceable",
    "ingest_findings",
    "promote_candidate_local",
    "clear_all_candidates",
]
