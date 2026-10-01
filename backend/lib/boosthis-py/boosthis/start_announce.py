"""Ungated startup and refused-project-key announcements."""

from __future__ import annotations

import re
import os
import resource
import sys
import threading
from typing import Callable, Literal, Optional

ProjectKeyProblem = Literal["authWord", "quoted", "cutOff", "wrongShape"]
BadgeState = Literal["visible", "hidden-by-setting", "switched-off"]
_AUTH_WORDS = ("bearer", "token", "basic", "apikey", "api-key", "key")
_QUOTES = "\"'`‘’“”"
_KEY_CHARS = re.compile(r"^[A-Za-z0-9_-]+$")
_lock = threading.Lock()
_announced = False
_announcement_pending = False
_refused: set[str] = set()
_held_refusals: list[str] = []
_source_notice_checked = False
_procfs_probe_for_tests: Optional[Callable[[], Optional[str]]] = None
_cgroup_probe_for_tests: Optional[Callable[[], Optional[str]]] = None
_affinity_probe_for_tests: Optional[Callable[[], Optional[str]]] = None
_gil_probe_for_tests: Optional[Callable[[], Optional[str]]] = None


def find_project_key_problem(raw: object) -> Optional[ProjectKeyProblem]:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    lower = value.lower()
    for word in _AUTH_WORDS:
        if lower.startswith(word) and len(value) > len(word) and value[len(word)].isspace():
            return "authWord"
    if value[0] in _QUOTES or value[-1] in _QUOTES or (value[0] == "<" and value[-1] == ">"):
        return "quoted"
    if value.endswith("...") or value.endswith("…") or (lower.startswith("bk_") and len(value) < 20):
        return "cutOff"
    if len(value) < 8 or _KEY_CHARS.fullmatch(value) is None:
        return "wrongShape"
    return None


def _problem_sentence(problem: ProjectKeyProblem) -> str:
    return {
        "authWord": "it starts with an authorization word. Paste the key on its own, with nothing in front of it.",
        "quoted": "it is wrapped in quotes. Paste the key on its own, with no quotes around it.",
        "cutOff": "it looks cut off. Paste the whole key from your Boosthis Setup page.",
        "wrongShape": "it is not the shape of a project key. A project key is one unbroken value from your Boosthis Setup page.",
    }[problem]


def project_key_refusal_line(problem: ProjectKeyProblem, source: str) -> str:
    return f"[boosthis] Boosthis will not use the project key from {source}: {_problem_sentence(problem)}"


def project_key_tail(key: object) -> Optional[str]:
    if not isinstance(key, str) or not key.strip():
        return None
    value = key.strip()
    return value[-4:] if len(value) >= 4 else value


def kit_startup_line(tail: Optional[str], badge_state: BadgeState = "visible") -> str:
    head = f"[boosthis] Boosthis starting: project key ...{tail}. Registering next." if tail else "[boosthis] Boosthis starting: no project key. Nothing will register."
    if badge_state == "hidden-by-setting":
        return head + " Badge hidden by a setting; Boosthis is still running."
    if badge_state == "switched-off":
        return head + " Badge hidden: Boosthis is switched off."
    return head


def _say(line: str) -> None:
    try:
        print(line, file=sys.stderr)
    except BaseException:
        pass


def begin_start_announcement() -> None:
    global _announcement_pending
    with _lock:
        if not _announced:
            _announcement_pending = True


def say_after_startup_line(line: str) -> None:
    with _lock:
        if _announcement_pending:
            _held_refusals.append(line)
            return
    _say(line)


def _forced_cause(
    probe: Optional[Callable[[], Optional[str]]],
) -> tuple[bool, Optional[str]]:
    if probe is None:
        return False, None
    try:
        return True, probe()
    except BaseException:
        return True, None


def procfs_missing_cause() -> Optional[str]:
    """Why the Linux process readers are absent, or None when readable."""
    forced, cause = _forced_cause(_procfs_probe_for_tests)
    if forced:
        return cause
    procfs_readable = False
    rusage_readable = False
    try:
        os.listdir("/proc/self")
        procfs_readable = True
    except BaseException:
        pass
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # These are the fields used by the readers, not merely proof that the
        # resource module imported.
        usage.ru_maxrss
        usage.ru_nvcsw
        usage.ru_nivcsw
        usage.ru_majflt
        rusage_readable = True
    except BaseException:
        pass
    if procfs_readable and rusage_readable:
        return None
    if not procfs_readable and not rusage_readable:
        return "/proc/self and resource.getrusage cannot be read on this host"
    if not procfs_readable:
        return "/proc/self cannot be read on this host"
    return "resource.getrusage cannot be read on this host"


def _files_readable(*paths: str) -> bool:
    try:
        for path in paths:
            with open(path, "r") as source:
                source.read(1)
        return True
    except BaseException:
        return False


def cgroup_missing_cause() -> Optional[str]:
    """Why a cgroup memory or CPU reader is absent, or None when both exist."""
    forced, cause = _forced_cause(_cgroup_probe_for_tests)
    if forced:
        return cause
    memory_readable = (
        _files_readable(
            "/sys/fs/cgroup/memory.max",
            "/sys/fs/cgroup/memory.current",
        )
        or _files_readable(
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
        )
    )
    cpu_readable = (
        _files_readable(
            "/sys/fs/cgroup/cpu.max",
            "/sys/fs/cgroup/cpu.stat",
        )
        or _files_readable(
            "/sys/fs/cgroup/cpu/cpu.cfs_quota_us",
            "/sys/fs/cgroup/cpu/cpu.stat",
        )
    )
    if memory_readable and cpu_readable:
        return None
    if not memory_readable and not cpu_readable:
        return "no cgroup memory or CPU controller is visible"
    if not memory_readable:
        return "no cgroup memory controller is visible"
    return "no cgroup CPU controller is visible"


def affinity_missing_cause() -> Optional[str]:
    """Why CPU entitlement is absent, or None when the platform exposes it."""
    forced, cause = _forced_cause(_affinity_probe_for_tests)
    if forced:
        return cause
    return (
        None
        if callable(getattr(os, "sched_getaffinity", None))
        else "os.sched_getaffinity is not on this platform"
    )


def gil_missing_cause() -> Optional[str]:
    """Why runtime capability is absent, or None when the interpreter exposes it."""
    forced, cause = _forced_cause(_gil_probe_for_tests)
    if forced:
        return cause
    return (
        None
        if callable(getattr(sys, "_is_gil_enabled", None))
        else "sys._is_gil_enabled is not on this interpreter"
    )


def unreadable_sources_line(
    procfs_cause: Optional[str],
    cgroup_cause: Optional[str],
    affinity_cause: Optional[str],
    gil_cause: Optional[str],
) -> Optional[str]:
    """Build the one pure, combined missing-source sentence."""
    parts: list[str] = []
    if procfs_cause is not None:
        parts.append(
            "peak memory, context switches, page faults, disk I/O pressure, "
            "descriptor mix, descriptor headroom, and cold start "
            f"({procfs_cause}): these process readings come from Linux "
            "/proc/self and resource.getrusage"
        )
    if cgroup_cause is not None:
        parts.append(
            "container memory pressure and CPU throttling "
            f"({cgroup_cause}): container memory and CPU controller readings "
            "come from cgroup v1 or v2"
        )
    if affinity_cause is not None:
        parts.append(
            f"CPU entitlement ({affinity_cause}): the scheduler's allowed CPU "
            "set comes from os.sched_getaffinity"
        )
    if gil_cause is not None:
        parts.append(
            f"runtime capability ({gil_cause}): free-threading state comes "
            "from sys._is_gil_enabled"
        )
    if not parts:
        return None
    tail = (
        ". "
        + ("That one meter stays" if len(parts) == 1 else "Those meters stay")
        + " absent for the life of this process; nothing else about Boosthis is affected."
    )
    if len(parts) == 1:
        return "[boosthis] This Python runtime cannot report " + parts[0] + tail
    return (
        f"[boosthis] This Python runtime cannot report {len(parts)} "
        "of Boosthis's readings — "
        + "; ".join(parts)
        + tail
    )


def announce_unreadable_sources() -> None:
    """Judge once per process and safely say the combined source notice."""
    global _source_notice_checked
    with _lock:
        if _source_notice_checked:
            return
        _source_notice_checked = True
    try:
        line = unreadable_sources_line(
            procfs_missing_cause(),
            cgroup_missing_cause(),
            affinity_missing_cause(),
            gil_missing_cause(),
        )
        if line is not None:
            # The existing channel preserves ordering with the startup line.
            say_after_startup_line(line)
    except BaseException:
        # Guest safety outranks this explanatory notice.
        pass


def _set_procfs_probe_for_tests(
    probe: Optional[Callable[[], Optional[str]]],
) -> None:
    global _procfs_probe_for_tests
    _procfs_probe_for_tests = probe


def _set_cgroup_probe_for_tests(
    probe: Optional[Callable[[], Optional[str]]],
) -> None:
    global _cgroup_probe_for_tests
    _cgroup_probe_for_tests = probe


def _set_affinity_probe_for_tests(
    probe: Optional[Callable[[], Optional[str]]],
) -> None:
    global _affinity_probe_for_tests
    _affinity_probe_for_tests = probe


def _set_gil_probe_for_tests(
    probe: Optional[Callable[[], Optional[str]]],
) -> None:
    global _gil_probe_for_tests
    _gil_probe_for_tests = probe


def _clear_source_probes_for_tests() -> None:
    _set_procfs_probe_for_tests(None)
    _set_cgroup_probe_for_tests(None)
    _set_affinity_probe_for_tests(None)
    _set_gil_probe_for_tests(None)


def _reset_source_notice_for_tests() -> None:
    global _source_notice_checked
    with _lock:
        _source_notice_checked = False


def flush_held_refusals() -> None:
    global _announcement_pending
    with _lock:
        _announcement_pending = False
        lines = list(_held_refusals)
        _held_refusals.clear()
    for line in lines:
        _say(line)


def announce_kit_start(key: object, badge_state: BadgeState) -> None:
    global _announced
    should_print = False
    with _lock:
        if not _announced:
            _announced = True
            should_print = True
    if should_print:
        _say(kit_startup_line(project_key_tail(key), badge_state))
        flush_held_refusals()


def warn_project_key_refused(raw: object, source: str) -> bool:
    problem = find_project_key_problem(raw)
    if problem is None:
        return False
    line: Optional[str] = None
    with _lock:
        if source not in _refused:
            _refused.add(source)
            line = project_key_refusal_line(problem, source)
            if _announcement_pending:
                _held_refusals.append(line)
                line = None
    if line is not None:
        _say(line)
    return True


def _reset_start_announce_for_tests() -> None:
    global _announced, _announcement_pending
    with _lock:
        _announced = False
        _announcement_pending = False
        _refused.clear()
        _held_refusals.clear()