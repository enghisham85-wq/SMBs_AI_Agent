"""Boosthis: the 2026-08 "honest limit" Python meter batch (11 axes).

The final additive batch from the toward-50 research
(``suggestions/meter-research-toward-50-2026-08.md``), taking Python from 26
meters to its honest ceiling of 37. Every axis is ADDITIVE — display-only,
never feeds the composite Speed score — and rides the existing snapshot
upload + PII guard. All sources are cheap standard-library reads sampled at
the EXISTING throttled request boundary (no spawned thread, no global
profiler, no ``sys.monitoring`` hook, no hot-path cost):

- ``allocChurn``        — net growth of live allocator blocks
                          (``sys.getallocatedblocks()`` trend).
- ``peakRss``           — highest resident memory the run has reached
                          (``ru_maxrss``) vs current RSS: transient spikes a
                          current-RSS trend can miss. Linux only.
- ``contextSwitching``  — voluntary vs involuntary context switches
                          (``ru_nvcsw``/``ru_nivcsw``): CPU-contention
                          preemption share. Linux only.
- ``pageFaults``        — major (I/O-backed) page-fault rate (``ru_majflt``).
                          Linux only.
- ``ioPressure``        — storage bytes read/written per second
                          (``/proc/self/io``). Linux only.
- ``cpuEntitlement``    — CPUs the process *sees* vs CPUs the scheduler
                          affinity mask actually *allows*
                          (``os.cpu_count()`` vs ``os.sched_getaffinity``).
                          Only on platforms exposing the affinity call.
- ``descriptorMix``     — open descriptors classified as sockets / pipes /
                          files / other (``/proc/self/fd`` + ``stat``), making
                          fd exhaustion actionable. Linux only. Counts only —
                          never a path or target.
- ``importChurn``       — loaded-module count still growing after startup
                          (``len(sys.modules)`` trend past the frozen
                          startupImport baseline).
- ``asyncSlowCallbacks`` — asyncio's OWN slow-callback warnings ("Executing
                          <handle> took N seconds"), counted through the
                          record factory extra_meters already chains. Present
                          ONLY when the host itself enabled asyncio debug
                          mode — we never flip it on. Counts only, never the
                          message.
- ``gcGenOccupancy``    — per-generation pending object counts + the
                          uncollectable total (``gc.get_count()`` /
                          ``gc.get_stats()``): retained population, not
                          collection frequency.
- ``runtimeCapability`` — free-threading / JIT state on CPython 3.13+
                          (``sys._is_gil_enabled`` / ``sys._jit``): context
                          for reading the GIL and execution meters. State,
                          not a health verdict — always rated good.

HONESTY: Linux-only sources are ABSENT (never guessed) elsewhere; the
asyncio-debug meter is absent unless the HOST enabled debug mode; trend axes
are omitted until enough samples span enough wall time. Dropped from the
research list with reasons: frozen-object share (no public count API — shim),
tracemalloc growth (operator-level opt-in tracing, not a drop-in meter; the
research doc itself excludes both from the 37 ceiling).

PRIVACY: numbers only — counts, durations, rates, ratios. Never a module
name, command, path, descriptor target, or log message. Captions are rebuilt
server-side.

GUEST-SAFETY: every entry point re-checks the LIVE collection gate and is
wrapped so it can never raise into the host.
"""

from __future__ import annotations

import gc
import logging
import os
import resource
import stat as stat_mod
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .runtime_flags import is_boosthis_disabled, is_collection_gated
from .health_axes import linear_score, rating_for

_IS_LINUX = sys.platform.startswith("linux")
_BYTES_PER_MB = 1024 * 1024
_SCOPE_PROCESS = 1
_SCOPE_CONTAINER = 2
_SCOPE_MACHINE = 3

# ── Bands + warm-up gates (axis-specific; display-only) ─────────────────────
# allocChurn — net live-allocator-block growth per minute. A flat/shrinking
# count is healthy; a sustained climb means objects are accumulating even when
# RSS looks flat. <=2k blocks/min -> 100; >=200k/min -> 0.
ALLOC_GOOD_PER_MIN = 2_000
ALLOC_POOR_PER_MIN = 200_000
ALLOC_MIN_SAMPLES = 12
ALLOC_MIN_SPAN_MS = 60_000

# peakRss is deliberately raw: CPython/OS exposes no honest fixed ceiling.

# contextSwitching — share of context switches that were INVOLUNTARY (the OS
# preempted us: CPU contention), over the observed window. <=10% -> 100;
# >=60% -> 0.
CTX_INVOL_PCT_GOOD = 10
CTX_INVOL_PCT_POOR = 60
CTX_MIN_SWITCHES = 500
CTX_MIN_SPAN_MS = 60_000

# cpuConsumption — process CPU seconds differenced against monotonic wall time.
# Percent is of ONE core, so two cores continuously occupied reads 200%.
CPU_CONSUMPTION_GOOD_PCT = 70.0
CPU_CONSUMPTION_POOR_PCT = 100.0
CPU_CONSUMPTION_MIN_SAMPLES = 3
CPU_CONSUMPTION_MIN_SPAN_MS = 1_000

# threadFootprint — thread-count trend after the first minute of process startup.
THREAD_FOOTPRINT_GOOD_PER_MIN = 0.5
THREAD_FOOTPRINT_POOR_PER_MIN = 5.0
THREAD_FOOTPRINT_MIN_SAMPLES = 12
THREAD_FOOTPRINT_MIN_SPAN_MS = 60_000
THREAD_FOOTPRINT_STARTUP_EXCLUSION_MS = 60_000

# pageFaults — MAJOR faults (require storage I/O) per minute. <=1/min -> 100;
# >=100/min -> 0 (a process constantly faulting to disk is thrashing).
FAULT_GOOD_PER_MIN = 1.0
FAULT_POOR_PER_MIN = 100.0
FAULT_MIN_SPAN_MS = 60_000

# ioPressure — storage bytes read+written per second over the window.
# <=2 MB/s -> 100; >=100 MB/s -> 0.
IO_GOOD_MB_PER_SEC = 2.0
IO_POOR_MB_PER_SEC = 100.0
IO_MIN_SPAN_MS = 60_000

# diskPressure — used capacity of the volume holding the kit's state directory
# (or cwd when no state directory is configured). <=75% -> 100; >=95% -> 0.
DISK_USED_PCT_GOOD = 75.0
DISK_USED_PCT_POOR = 95.0

# systemLoad — kernel one-minute load average per usable CPU. <=0.7 -> 100;
# >=2.0 -> 0. The kernel average is not meaningful before one minute uptime.
SYSTEM_LOAD_GOOD = 0.7
SYSTEM_LOAD_POOR = 2.0
SYSTEM_LOAD_MIN_UPTIME_SEC = 60.0

# kit-probe-file storage pair. One tiny write+read+delete round trip in the
# configured BOOSTHIS_STATE_DIR is one attempted operation. The same five-
# attempt gate releases both readings; completed latencies use a bounded ring.
STORAGE_LATENCY_GOOD_MS = 8.0
STORAGE_LATENCY_POOR_MS = 120.0
STORAGE_FAILURE_GOOD = 0.01
STORAGE_FAILURE_POOR = 0.10
STORAGE_MIN_OPERATIONS = 5
STORAGE_RING_CAP = 200

# cpuEntitlement — visible CPUs / affinity-allowed CPUs. 1x (aligned) -> 100;
# >=4x (process believes it has 4x the CPUs it may use) -> 0.
ENTITLE_RATIO_GOOD = 1.0
ENTITLE_RATIO_POOR = 4.0

# descriptorMix — the share of open descriptors that are sockets. Sockets
# dominating the table is the classic leak shape behind fd exhaustion.
# <=70% -> 100; >=95% -> 0. Below MIN_FDS the mix is noise -> omitted.
FDMIX_SOCK_PCT_GOOD = 70
FDMIX_SOCK_PCT_POOR = 95
FDMIX_MIN_FDS = 20
FDMIX_SCAN_CAP = 4_096  # bound the /proc/self/fd walk

# importChurn — modules loaded per HOUR after startup (slope of the sampled
# len(sys.modules)). Lazy imports settle; endless growth is a leak.
# <=2/hour -> 100; >=60/hour -> 0.
IMPORT_GOOD_PER_HOUR = 2.0
IMPORT_POOR_PER_HOUR = 60.0
IMPORT_MIN_SAMPLES = 12
IMPORT_MIN_SPAN_MS = 5 * 60_000

# asyncSlowCallbacks — asyncio-debug slow-callback warnings per minute.
# <=0.2/min -> 100; >=10/min -> 0.
SLOWCB_GOOD_PER_MIN = 0.2
SLOWCB_POOR_PER_MIN = 10.0
SLOWCB_MIN_WINDOW_MS = 5 * 60_000
SLOWCB_WINDOW_MS = 30 * 60_000
SLOWCB_RING_CAP = 300

# gcGenOccupancy — scored on the UNCOLLECTABLE total (objects the collector
# found but can never free — a real leak class). 0 -> 100; >=50 -> 0. The
# per-generation pending counts ride along as context.
UNCOLLECTABLE_GOOD = 0
UNCOLLECTABLE_POOR = 50

# Shared sampling throttle + ring bound for the boundary sampler.
SAMPLE_THROTTLE_MS = 5_000
SAMPLE_RING_CAP = 240

# Turbo first-run ramp: the boundary sampler's reads are genuinely cheap (one
# getallocatedblocks / len(sys.modules) / getrusage + one small /proc read), so
# for the first few minutes of a session we sample once a second instead of
# every SAMPLE_THROTTLE_MS. This lets count-based meters warm up sooner without
# touching any axis gate (only the reporting density changes, never a MIN_*
# threshold). Parity with the sibling kits' turbo window; `BOOSTHIS_TURBO=0`
# (and only that exact value) opts out.
TURBO_SAMPLE_THROTTLE_MS = 1_000
TURBO_WINDOW_MS = 180_000

# ── State ────────────────────────────────────────────────────────────────────
_started = False
_started_at = 0
_last_sample_at = 0

# (ts_ms, allocated_blocks)
_alloc_samples: List[Tuple[int, int]] = []
# (ts_ms, module_count)
_module_samples: List[Tuple[int, int]] = []
_module_baseline: Optional[int] = None
# (ts_ms, nvcsw, nivcsw, majflt, cpu_seconds) — cumulative rusage counters.
_rusage_samples: List[Tuple[int, int, int, int, float]] = []
# (ts_ms, active_threads), sampled without a per-thread walk.
_thread_samples: List[Tuple[int, int]] = []
# (ts_ms, read_bytes, write_bytes) — cumulative /proc/self/io counters.
_io_samples: List[Tuple[int, int, int]] = []
_storage_samples: List[float] = []
_storage_attempts = 0
_storage_failures = 0
_storage_lock = threading.Lock()
# asyncio debug mode observed True at any boundary (presence gate for
# asyncSlowCallbacks — the host must have turned debug on itself).
_loop_debug_observed = False
# Slow-callback warning timestamps (counts only; the record is never kept).
_slowcb_ts: List[int] = []
_sampled_peak_rss_mb = 0.0

# Test overrides
_now_override: Optional[int] = None
_rusage_override: Optional[Any] = None       # object with ru_* attrs or "unavailable"
_io_override: Optional[Any] = None           # {"read": int, "write": int} or "unavailable"
_fd_mix_override: Optional[Any] = None       # {"sockets","pipes","files","other"} or "unavailable"
_cpu_override: Optional[Any] = None          # {"visible","allowed"} or "unavailable"
_gc_occupancy_override: Optional[Any] = None # {"gen0","gen1","gen2","uncollectable"}
_capability_override: Optional[Any] = None   # {"gil","jitAvailable","jitEnabled"} or "unavailable"
_alloc_reader_override: Optional[Callable[[], int]] = None
_linux_override: Optional[bool] = None
_rss_override: Optional[float] = None
_statvfs_override: Optional[Any] = None
_load_override: Optional[Any] = None
_uptime_override: Optional[float] = None
_container_override: Optional[bool] = None
_containerised_cache: Optional[bool] = None


def _now_ms() -> int:
    if _now_override is not None:
        return _now_override
    return int(time.time() * 1000)


def _turbo_disabled() -> bool:
    """``BOOSTHIS_TURBO=0`` (and only that exact value) opts out of the turbo
    ramp; any other value — or an unreadable env — leaves it on."""
    try:
        return os.environ.get("BOOSTHIS_TURBO") == "0"
    except Exception:  # noqa: BLE001
        return False


def _sample_throttle_ms(now_ms: int) -> int:
    """The boundary sampler's throttle: the dense turbo throttle while the
    session is young (age < :data:`TURBO_WINDOW_MS`), then the normal
    :data:`SAMPLE_THROTTLE_MS`. Age-driven so an idle early session cannot burn
    the ramp; never touches any axis gate."""
    if _turbo_disabled() or _started_at <= 0:
        return SAMPLE_THROTTLE_MS
    if now_ms - _started_at < TURBO_WINDOW_MS:
        return TURBO_SAMPLE_THROTTLE_MS
    return SAMPLE_THROTTLE_MS


def _is_linux() -> bool:
    if _linux_override is not None:
        return _linux_override
    return _IS_LINUX


def _is_containerised() -> bool:
    """Cached contract test: /.dockerenv, or a known container marker in the
    current process cgroup. An unreadable pair means machine."""
    global _containerised_cache
    if _container_override is not None:
        return _container_override
    if _containerised_cache is not None:
        return _containerised_cache
    found = False
    try:
        found = os.path.exists("/.dockerenv")
    except Exception:  # noqa: BLE001
        pass
    if not found:
        try:
            with open("/proc/self/cgroup", "r") as fh:
                cgroup = fh.read().lower()
            found = any(word in cgroup for word in ("docker", "kubepods", "containerd", "lxc"))
        except Exception:  # noqa: BLE001
            pass
    _containerised_cache = found
    return found


def _volume_scope_code() -> int:
    return _SCOPE_CONTAINER if _is_containerised() else _SCOPE_MACHINE


def _collecting() -> bool:
    """The LIVE recording gate, consulted at every record point — flipping the
    kill switch or an entitlement lock mid-run stops collection at once."""
    if not _started:
        return False
    try:
        return not is_collection_gated()
    except Exception:  # noqa: BLE001
        return False


def start_os_meters() -> None:
    """Arm the batch. Idempotent, no-op under the kill-switch, never raises.
    Called from ``enable_telemetry``. No hook is installed here — everything
    is boundary-sampled or read at snapshot time; the slow-callback observer
    rides the record factory extra_meters already chains."""
    global _started, _started_at, _module_baseline
    if is_boosthis_disabled() or _started:
        return
    _started = True
    _started_at = _now_ms()
    try:
        if _module_baseline is None:
            _module_baseline = len(sys.modules)
    except Exception:  # noqa: BLE001
        _module_baseline = None


def note_request_boundary() -> None:
    """Throttled sampler piggybacking on the existing request boundary (called
    from ``runtime_vitals.note_request`` — no new call sites in the host).
    One ``getallocatedblocks``/``len(sys.modules)``/``getrusage`` read plus one
    small /proc read every SAMPLE_THROTTLE_MS at most. Never raises."""
    global _last_sample_at, _loop_debug_observed, _sampled_peak_rss_mb
    if not _collecting():
        return
    try:
        ts = _now_ms()
        if ts - _last_sample_at < _sample_throttle_ms(ts):
            return
        _last_sample_at = ts
        sampled_rss = _read_rss_mb()
        if sampled_rss is not None:
            _sampled_peak_rss_mb = max(_sampled_peak_rss_mb, sampled_rss)
        # Allocator blocks (always available).
        try:
            blocks = (
                _alloc_reader_override()
                if _alloc_reader_override is not None
                else sys.getallocatedblocks()
            )
            _alloc_samples.append((ts, int(blocks)))
            if len(_alloc_samples) > SAMPLE_RING_CAP:
                del _alloc_samples[0 : len(_alloc_samples) - SAMPLE_RING_CAP]
        except Exception:  # noqa: BLE001
            pass
        # Loaded-module count (always available).
        try:
            _module_samples.append((ts, len(sys.modules)))
            if len(_module_samples) > SAMPLE_RING_CAP:
                del _module_samples[0 : len(_module_samples) - SAMPLE_RING_CAP]
        except Exception:  # noqa: BLE001
            pass
        # rusage counters (Linux only — ru_* fields are guessed-at elsewhere).
        ru = _read_rusage()
        if ru is not None:
            cpu_seconds = float(ru.ru_utime) + float(ru.ru_stime)
            # A process replacement / counter reset invalidates the old anchor.
            # Clear before appending so no negative or lifetime absolute leaks.
            if _rusage_samples and cpu_seconds < _rusage_samples[-1][4]:
                _rusage_samples.clear()
            _rusage_samples.append(
                (
                    ts,
                    int(ru.ru_nvcsw),
                    int(ru.ru_nivcsw),
                    int(ru.ru_majflt),
                    cpu_seconds,
                )
            )
            if len(_rusage_samples) > SAMPLE_RING_CAP:
                del _rusage_samples[0 : len(_rusage_samples) - SAMPLE_RING_CAP]
        # One O(1) runtime counter read; never enumerate /proc/self/task.
        if ts - _started_at >= THREAD_FOOTPRINT_STARTUP_EXCLUSION_MS:
            _thread_samples.append((ts, int(threading.active_count())))
            if len(_thread_samples) > SAMPLE_RING_CAP:
                del _thread_samples[0 : len(_thread_samples) - SAMPLE_RING_CAP]
        # /proc/self/io counters (Linux only, may be unreadable in hardened
        # containers -> honestly absent).
        io = _read_proc_io()
        if io is not None:
            _io_samples.append((ts, io[0], io[1]))
            if len(_io_samples) > SAMPLE_RING_CAP:
                del _io_samples[0 : len(_io_samples) - SAMPLE_RING_CAP]
        # asyncio debug flag: presence gate for asyncSlowCallbacks. We only
        # OBSERVE the host's own choice — never enable debug ourselves.
        if not _loop_debug_observed:
            try:
                import asyncio

                loop = asyncio.get_running_loop()
                if loop.get_debug():
                    _loop_debug_observed = True
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def note_log_record(record: logging.LogRecord) -> None:
    """Observe one log record via the record factory extra_meters chains
    (called from its ``_note_log_record`` before the ERROR filter). Counts
    asyncio's own slow-callback warnings — 'Executing <Handle …> took N
    seconds' on the 'asyncio' logger at WARNING. Records ONLY a timestamp —
    never the message. Never raises."""
    if not _collecting():
        return
    try:
        if record.name != "asyncio" or record.levelno != logging.WARNING:
            return
        msg = record.msg
        if not isinstance(msg, str) or not msg.startswith("Executing"):
            return
        _slowcb_ts.append(_now_ms())
        if len(_slowcb_ts) > SLOWCB_RING_CAP:
            del _slowcb_ts[0 : len(_slowcb_ts) - SLOWCB_RING_CAP]
    except Exception:  # noqa: BLE001
        pass


# ── Source readers (each returns None when honestly unavailable) ────────────
def _read_rusage() -> Optional[Any]:
    if _rusage_override == "unavailable":
        return None
    if _rusage_override is not None:
        return _rusage_override
    if not _is_linux():
        return None  # ru_* field meanings/units are Linux-verified only
    try:
        return resource.getrusage(resource.RUSAGE_SELF)
    except Exception:  # noqa: BLE001
        return None


def _read_proc_io() -> Optional[Tuple[int, int]]:
    """(read_bytes, write_bytes) from /proc/self/io, or None."""
    if _io_override == "unavailable":
        return None
    if _io_override is not None:
        return (int(_io_override["read"]), int(_io_override["write"]))
    if not _is_linux():
        return None
    try:
        read_b = write_b = None
        with open("/proc/self/io", "r") as fh:
            for line in fh:
                if line.startswith("read_bytes:"):
                    read_b = int(line.split(":")[1])
                elif line.startswith("write_bytes:"):
                    write_b = int(line.split(":")[1])
        if read_b is None or write_b is None:
            return None
        return (read_b, write_b)
    except Exception:  # noqa: BLE001
        return None


def _read_rss_mb() -> Optional[float]:
    """Current RSS in MB via /proc/self/statm (kept local — no runtime_vitals
    import, same pattern as extra_meters' process-age reader)."""
    if _rss_override is not None:
        return _rss_override
    try:
        with open("/proc/self/statm", "r") as fh:
            fields = fh.read().split()
        return (int(fields[1]) * os.sysconf("SC_PAGE_SIZE")) / _BYTES_PER_MB
    except Exception:  # noqa: BLE001
        return None


def _read_fd_mix() -> Optional[Dict[str, int]]:
    """Classify open descriptors via /proc/self/fd + fstat. Counts only —
    the link target is never read. None when /proc is unavailable."""
    if _fd_mix_override == "unavailable":
        return None
    if _fd_mix_override is not None:
        return dict(_fd_mix_override)
    if not _is_linux():
        return None
    try:
        names = os.listdir("/proc/self/fd")[:FDMIX_SCAN_CAP]
        sockets = pipes = files = other = 0
        for name in names:
            try:
                mode = os.stat(f"/proc/self/fd/{name}").st_mode
            except Exception:  # noqa: BLE001
                continue  # fd closed between listdir and stat
            if stat_mod.S_ISSOCK(mode):
                sockets += 1
            elif stat_mod.S_ISFIFO(mode):
                pipes += 1
            elif stat_mod.S_ISREG(mode):
                files += 1
            else:
                other += 1
        return {"sockets": sockets, "pipes": pipes, "files": files, "other": other}
    except Exception:  # noqa: BLE001
        return None


# ── Axis readers (None = warming / honestly absent) ─────────────────────────
def _declared_silence(scope_code: int) -> Dict[str, Any]:
    return {
        "present": 1,
        "measurable": 0,
        "reasonCode": 8,  # BLOCKED_BY_ENVIRONMENT
        "scopeCode": scope_code,
    }


def _read_disk_pressure() -> Optional[Dict[str, Any]]:
    scope = _volume_scope_code()
    try:
        path = os.environ.get("BOOSTHIS_STATE_DIR") or os.getcwd()
        fs = _statvfs_override if _statvfs_override is not None else os.statvfs(path)
        total = int(fs.f_blocks) * int(fs.f_frsize)
        free = int(fs.f_bavail) * int(fs.f_frsize)
        if total <= 0 or free < 0:
            return None
        used_pct = max(0.0, min(100.0, 100.0 * (total - free) / total))
        score = linear_score(used_pct, DISK_USED_PCT_GOOD, DISK_USED_PCT_POOR)
        return {
            "score": score,
            "rating": rating_for(score),
            "usedPct": round(used_pct * 10) / 10,
            "freeMb": round(free / _BYTES_PER_MB),
            "totalMb": round(total / _BYTES_PER_MB),
            "scopeCode": scope,
            "caption": f"{round(used_pct * 10) / 10}% used",
        }
    except OSError:
        return _declared_silence(scope)
    except Exception:  # noqa: BLE001
        return None


def _read_uptime_seconds() -> Optional[float]:
    if _uptime_override is not None:
        return _uptime_override
    try:
        with open("/proc/uptime", "r") as fh:
            return float(fh.read().split()[0])
    except Exception:  # noqa: BLE001
        return None


def _read_system_load() -> Optional[Dict[str, Any]]:
    uptime = _read_uptime_seconds()
    if uptime is None or uptime < SYSTEM_LOAD_MIN_UPTIME_SEC:
        return None
    try:
        load = float(_load_override if _load_override is not None else os.getloadavg()[0])
        getaff = getattr(os, "sched_getaffinity", None)
        cpus = len(getaff(0)) if callable(getaff) else int(os.cpu_count() or 0)
        if cpus <= 0 or load < 0:
            return None
        per_cpu = load / cpus
        score = linear_score(per_cpu, SYSTEM_LOAD_GOOD, SYSTEM_LOAD_POOR)
        return {
            "score": score,
            "rating": rating_for(score),
            "loadPerCpu": round(per_cpu * 100) / 100,
            "load": round(load * 100) / 100,
            "cpus": cpus,
            "scopeCode": _SCOPE_MACHINE,
            "caption": f"{round(per_cpu * 100) / 100} load per CPU",
        }
    except OSError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _sample_storage_probe() -> None:
    """Take one configured-state-dir probe. No configured dir means no store
    and therefore no attempt, not a fabricated failure."""
    global _storage_attempts, _storage_failures
    state_dir = os.environ.get("BOOSTHIS_STATE_DIR")
    if not state_dir:
        return
    with _storage_lock:
        _storage_attempts += 1
        path = os.path.join(state_dir, f".boosthis-storage-probe-{os.getpid()}")
        started = time.perf_counter()
        completed = False
        try:
            with open(path, "wb") as fh:
                fh.write(b"boosthis")
                fh.flush()
            with open(path, "rb") as fh:
                if fh.read() != b"boosthis":
                    raise OSError("storage probe read mismatch")
            os.unlink(path)
            completed = True
        except Exception:  # noqa: BLE001
            _storage_failures += 1
            try:
                os.unlink(path)
            except Exception:  # noqa: BLE001
                pass
        if completed:
            _storage_samples.append(max(0.0, (time.perf_counter() - started) * 1000.0))
            if len(_storage_samples) > STORAGE_RING_CAP:
                del _storage_samples[0 : len(_storage_samples) - STORAGE_RING_CAP]


def _read_storage_latency() -> Optional[Dict[str, Any]]:
    if _storage_attempts < STORAGE_MIN_OPERATIONS:
        return None
    scope = _volume_scope_code()
    if not _storage_samples:
        return _declared_silence(scope)
    ordered = sorted(_storage_samples)
    index = min(len(ordered) - 1, round(0.75 * (len(ordered) - 1)))
    p75 = ordered[index]
    worst = max(ordered)
    score = linear_score(p75, STORAGE_LATENCY_GOOD_MS, STORAGE_LATENCY_POOR_MS)
    return {
        "score": score,
        "rating": rating_for(score),
        "p75Ms": round(p75 * 100) / 100,
        "worstMs": round(worst * 100) / 100,
        "opCount": len(ordered),
        "scopeCode": scope,
        "caption": f"{round(p75 * 100) / 100} ms p75",
    }


def _read_storage_failures() -> Optional[Dict[str, Any]]:
    if _storage_attempts < STORAGE_MIN_OPERATIONS:
        return None
    fail_pct = 100.0 * _storage_failures / _storage_attempts
    fail_share = _storage_failures / _storage_attempts
    score = linear_score(fail_share, STORAGE_FAILURE_GOOD, STORAGE_FAILURE_POOR)
    return {
        "score": score,
        "rating": rating_for(score),
        "failPct": round(fail_pct * 10) / 10,
        "failCount": _storage_failures,
        "opCount": _storage_attempts,
        "scopeCode": _volume_scope_code(),
        "caption": f"{round(fail_pct * 10) / 10}% failed",
    }


def _read_alloc_churn() -> Optional[Dict[str, Any]]:
    if len(_alloc_samples) < ALLOC_MIN_SAMPLES:
        return None
    first_ts, first_n = _alloc_samples[0]
    last_ts, last_n = _alloc_samples[-1]
    span_ms = last_ts - first_ts
    if span_ms < ALLOC_MIN_SPAN_MS:
        return None
    per_min = (last_n - first_n) / (span_ms / 60_000.0)
    score = linear_score(max(0.0, per_min), ALLOC_GOOD_PER_MIN, ALLOC_POOR_PER_MIN)
    return {
        "score": score,
        "rating": rating_for(score),
        "blocks": int(last_n),
        "blocksPerMin": round(per_min),
        "sampleCount": len(_alloc_samples),
        "caption": (
            f"{int(last_n)} live blocks \u00b7 "
            + (f"+{round(per_min)}/min" if per_min > 0 else "flat")
        ),
    }


def _read_peak_rss() -> Optional[Dict[str, Any]]:
    peak_mb: Optional[float] = None
    exact = 0
    if _rusage_override is None and _is_linux():
        try:
            with open("/proc/self/status", "r") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):
                        peak_mb = float(line.split()[1]) / 1024.0
                        exact = 1
                        break
        except Exception:  # noqa: BLE001
            pass
    ru = _read_rusage()
    if ru is None and sys.platform == "darwin" and _rusage_override is None:
        try:
            ru = resource.getrusage(resource.RUSAGE_SELF)
        except Exception:  # noqa: BLE001
            pass
    try:
        if peak_mb is None and ru is not None:
            raw = float(ru.ru_maxrss)
            # ru_maxrss is KB on Linux and bytes on macOS.
            peak_mb = raw / (_BYTES_PER_MB if sys.platform == "darwin" else 1024.0)
            exact = 1
    except Exception:  # noqa: BLE001
        peak_mb = None
    if peak_mb is None:
        peak_mb = _sampled_peak_rss_mb or _read_rss_mb()
    if peak_mb is None or peak_mb <= 0:
        return None
    rss_mb = _read_rss_mb()
    if rss_mb is None or rss_mb <= 0:
        rss_mb = peak_mb
    return {
        "rating": "not-scored",
        "peakRssMb": round(peak_mb),
        "rssMb": round(rss_mb),
        "sampleCount": 1,
        "exact": exact,
        "caption": f"peak {round(peak_mb)} MB \u00b7 now {round(rss_mb)} MB",
    }


def _read_context_switching() -> Optional[Dict[str, Any]]:
    if len(_rusage_samples) < 2:
        return None
    t0, nv0, ni0, _, _cpu0 = _rusage_samples[0]
    t1, nv1, ni1, _, _cpu1 = _rusage_samples[-1]
    span_ms = t1 - t0
    if span_ms < CTX_MIN_SPAN_MS:
        return None
    d_vol = max(0, nv1 - nv0)
    d_invol = max(0, ni1 - ni0)
    total = d_vol + d_invol
    if total < CTX_MIN_SWITCHES:
        return None  # too few switches for the share to mean anything
    invol_pct = (100.0 * d_invol) / total
    per_sec = total / (span_ms / 1000.0)
    score = linear_score(invol_pct, CTX_INVOL_PCT_GOOD, CTX_INVOL_PCT_POOR)
    return {
        "score": score,
        "rating": rating_for(score),
        "involuntaryPct": round(invol_pct * 10) / 10,
        "switchesPerSec": round(per_sec),
        "windowMin": round(span_ms / 60_000.0 * 10) / 10,
        "caption": (
            f"{round(invol_pct)}% preempted \u00b7 {round(per_sec)} switches/s"
        ),
    }


def _read_cpu_consumption() -> Optional[Dict[str, Any]]:
    if len(_rusage_samples) < CPU_CONSUMPTION_MIN_SAMPLES:
        return None
    t0, _nv0, _ni0, _maj0, cpu0 = _rusage_samples[0]
    t1, _nv1, _ni1, _maj1, cpu1 = _rusage_samples[-1]
    span_ms = t1 - t0
    if span_ms < CPU_CONSUMPTION_MIN_SPAN_MS or cpu1 < cpu0:
        return None
    cpu_pct = 100.0 * (cpu1 - cpu0) / (span_ms / 1000.0)
    score = linear_score(
        cpu_pct, CPU_CONSUMPTION_GOOD_PCT, CPU_CONSUMPTION_POOR_PCT
    )
    return {
        "score": score,
        "rating": rating_for(score),
        "cpuPct": round(cpu_pct * 10) / 10,
        "sampleCount": len(_rusage_samples),
        "windowSec": round(span_ms / 100.0) / 10,
        "caption": f"{round(cpu_pct * 10) / 10}% of one core",
    }


def _read_thread_footprint() -> Optional[Dict[str, Any]]:
    n = len(_thread_samples)
    if n < THREAD_FOOTPRINT_MIN_SAMPLES:
        return None
    first_ts = _thread_samples[0][0]
    last_ts = _thread_samples[-1][0]
    span_ms = last_ts - first_ts
    if span_ms < THREAD_FOOTPRINT_MIN_SPAN_MS:
        return None
    xs = [(ts - first_ts) / 60_000.0 for ts, _count in _thread_samples]
    ys = [float(count) for _ts, count in _thread_samples]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    sxx = sum((x - x_mean) ** 2 for x in xs)
    if sxx <= 0:
        return None
    growth = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / sxx
    residual = sum(
        (y - (y_mean + growth * (x - x_mean))) ** 2 for x, y in zip(xs, ys)
    )
    stderr = ((residual / max(1, n - 2)) / sxx) ** 0.5
    axis: Dict[str, Any] = {
        "threads": int(ys[-1]),
        "peakThreads": int(max(ys)),
        "growthPerMin": round(growth * 10) / 10,
        "sampleCount": n,
        "windowMin": round(span_ms / 60_000.0 * 10) / 10,
        "caption": f"{int(ys[-1])} threads",
    }
    # A slope inside its own noise is observed but deliberately unscored.
    if growth > stderr:
        score = linear_score(
            growth,
            THREAD_FOOTPRINT_GOOD_PER_MIN,
            THREAD_FOOTPRINT_POOR_PER_MIN,
        )
        axis["score"] = score
        axis["rating"] = rating_for(score)
    return axis


def _read_page_faults() -> Optional[Dict[str, Any]]:
    if len(_rusage_samples) < 2:
        return None
    t0 = _rusage_samples[0][0]
    t1 = _rusage_samples[-1][0]
    span_ms = t1 - t0
    if span_ms < FAULT_MIN_SPAN_MS:
        return None
    d_maj = max(0, _rusage_samples[-1][3] - _rusage_samples[0][3])
    per_min = d_maj / (span_ms / 60_000.0)
    score = linear_score(per_min, FAULT_GOOD_PER_MIN, FAULT_POOR_PER_MIN)
    return {
        "score": score,
        "rating": rating_for(score),
        "majorFaults": int(d_maj),
        "majorPerMin": round(per_min * 10) / 10,
        "windowMin": round(span_ms / 60_000.0 * 10) / 10,
        "scopeCode": _SCOPE_PROCESS,
        "caption": (
            "no major page faults"
            if d_maj == 0
            else f"{round(per_min * 10) / 10} major faults/min"
        ),
    }


def _read_io_pressure() -> Optional[Dict[str, Any]]:
    if len(_io_samples) < 2:
        return None
    t0, r0, w0 = _io_samples[0]
    t1, r1, w1 = _io_samples[-1]
    span_ms = t1 - t0
    if span_ms < IO_MIN_SPAN_MS:
        return None
    d_read = max(0, r1 - r0)
    d_write = max(0, w1 - w0)
    mb_per_sec = ((d_read + d_write) / _BYTES_PER_MB) / (span_ms / 1000.0)
    score = linear_score(mb_per_sec, IO_GOOD_MB_PER_SEC, IO_POOR_MB_PER_SEC)
    return {
        "score": score,
        "rating": rating_for(score),
        "mbPerSec": round(mb_per_sec * 100) / 100,
        "readMb": round(d_read / _BYTES_PER_MB),
        "writtenMb": round(d_write / _BYTES_PER_MB),
        "windowMin": round(span_ms / 60_000.0 * 10) / 10,
        "scopeCode": _SCOPE_PROCESS,
        "caption": f"{round(mb_per_sec * 100) / 100} MB/s storage I/O",
    }


def _read_cpu_entitlement() -> Optional[Dict[str, Any]]:
    if _cpu_override == "unavailable":
        return None
    if _cpu_override is not None:
        visible = int(_cpu_override["visible"])
        allowed = int(_cpu_override["allowed"])
    else:
        getaff = getattr(os, "sched_getaffinity", None)
        if not callable(getaff):
            return None  # platform doesn't expose the affinity mask -> absent
        try:
            visible = int(os.cpu_count() or 0)
            allowed = len(getaff(0))
        except Exception:  # noqa: BLE001
            return None
    if visible <= 0 or allowed <= 0:
        return None
    ratio = visible / allowed
    score = linear_score(max(1.0, ratio), ENTITLE_RATIO_GOOD, ENTITLE_RATIO_POOR)
    return {
        "score": score,
        "rating": rating_for(score),
        "visibleCpus": visible,
        "allowedCpus": allowed,
        "mismatchRatio": round(ratio * 100) / 100,
        "caption": (
            f"{allowed} CPUs allowed of {visible} visible"
            if visible != allowed
            else f"{visible} CPUs \u00b7 affinity aligned"
        ),
    }


def _read_descriptor_mix() -> Optional[Dict[str, Any]]:
    mix = _read_fd_mix()
    if mix is None:
        return None
    total = mix["sockets"] + mix["pipes"] + mix["files"] + mix["other"]
    if total < FDMIX_MIN_FDS:
        return None  # too few descriptors for the mix to mean anything
    sock_pct = (100.0 * mix["sockets"]) / total
    score = linear_score(sock_pct, FDMIX_SOCK_PCT_GOOD, FDMIX_SOCK_PCT_POOR)
    return {
        "score": score,
        "rating": rating_for(score),
        "sockets": int(mix["sockets"]),
        "pipes": int(mix["pipes"]),
        "files": int(mix["files"]),
        "otherFds": int(mix["other"]),
        "socketsPct": round(sock_pct),
        "scopeCode": _SCOPE_PROCESS,
        "caption": (
            f"{mix['sockets']} sockets \u00b7 {mix['files']} files \u00b7 "
            f"{mix['pipes']} pipes \u00b7 {mix['other']} other"
        ),
    }


def _read_import_churn() -> Optional[Dict[str, Any]]:
    if len(_module_samples) < IMPORT_MIN_SAMPLES:
        return None
    t0, n0 = _module_samples[0]
    t1, n1 = _module_samples[-1]
    span_ms = t1 - t0
    if span_ms < IMPORT_MIN_SPAN_MS:
        return None
    per_hour = (n1 - n0) / (span_ms / 3_600_000.0)
    score = linear_score(max(0.0, per_hour), IMPORT_GOOD_PER_HOUR, IMPORT_POOR_PER_HOUR)
    added = (n1 - _module_baseline) if _module_baseline is not None else None
    out: Dict[str, Any] = {
        "score": score,
        "rating": rating_for(score),
        "modules": int(n1),
        "growthPerHour": round(per_hour * 10) / 10,
        "caption": (
            f"{int(n1)} modules \u00b7 "
            + (f"+{round(per_hour * 10) / 10}/hour" if per_hour > 0 else "settled")
        ),
    }
    if added is not None and added >= 0:
        out["addedSinceStart"] = int(added)
    return out


def _read_async_slow_callbacks() -> Optional[Dict[str, Any]]:
    if not _loop_debug_observed:
        return None  # host never enabled asyncio debug -> honest N/A
    now = _now_ms()
    window_ms = min(now - _started_at, SLOWCB_WINDOW_MS)
    if window_ms < SLOWCB_MIN_WINDOW_MS:
        return None
    cutoff = now - window_ms
    count = sum(1 for t in _slowcb_ts if t >= cutoff)
    per_min = count / (window_ms / 60_000.0)
    score = linear_score(per_min, SLOWCB_GOOD_PER_MIN, SLOWCB_POOR_PER_MIN)
    return {
        "score": score,
        "rating": rating_for(score),
        "count": int(count),
        "perMin": round(per_min * 100) / 100,
        "windowMin": round(window_ms / 60_000.0 * 10) / 10,
        "caption": (
            "no slow callbacks flagged"
            if count == 0
            else f"{int(count)} slow callback{'s' if count != 1 else ''} flagged"
        ),
    }


def _read_gc_gen_occupancy() -> Optional[Dict[str, Any]]:
    try:
        if _gc_occupancy_override is not None:
            gen0 = int(_gc_occupancy_override["gen0"])
            gen1 = int(_gc_occupancy_override["gen1"])
            gen2 = int(_gc_occupancy_override["gen2"])
            uncollectable = int(_gc_occupancy_override["uncollectable"])
        else:
            counts = gc.get_count()
            if len(counts) < 3:
                return None
            gen0, gen1, gen2 = int(counts[0]), int(counts[1]), int(counts[2])
            uncollectable = 0
            for s in gc.get_stats():
                uncollectable += int(s.get("uncollectable", 0) or 0)
    except Exception:  # noqa: BLE001
        return None
    score = linear_score(uncollectable, UNCOLLECTABLE_GOOD, UNCOLLECTABLE_POOR)
    return {
        "score": score,
        "rating": rating_for(score),
        "gen0": gen0,
        "gen1": gen1,
        "gen2": gen2,
        "uncollectable": uncollectable,
        "caption": (
            f"{gen0}/{gen1}/{gen2} pending by generation"
            + (f" \u00b7 {uncollectable} uncollectable" if uncollectable > 0 else "")
        ),
    }


def _read_runtime_capability() -> Optional[Dict[str, Any]]:
    if _capability_override == "unavailable":
        return None
    if _capability_override is not None:
        gil = bool(_capability_override["gil"])
        jit_avail = bool(_capability_override["jitAvailable"])
        jit_on = bool(_capability_override["jitEnabled"])
    else:
        probe = getattr(sys, "_is_gil_enabled", None)
        if not callable(probe):
            return None  # pre-3.13 CPython exposes no capability flags -> absent
        try:
            gil = bool(probe())
        except Exception:  # noqa: BLE001
            return None
        jit_avail = jit_on = False
        try:
            jit = getattr(sys, "_jit", None)
            if jit is not None:
                jit_avail = bool(jit.is_available())
                jit_on = bool(jit.is_enabled())
        except Exception:  # noqa: BLE001
            jit_avail = jit_on = False
    # A capability STATE is context, not a health problem — always rated good.
    return {
        "score": 100,
        "rating": rating_for(100),
        "gilEnabled": 1 if gil else 0,
        "jitAvailable": 1 if jit_avail else 0,
        "jitEnabled": 1 if jit_on else 0,
        "caption": (
            ("GIL on" if gil else "free-threaded")
            + " \u00b7 JIT "
            + ("on" if jit_on else ("available, off" if jit_avail else "absent"))
        ),
    }


def read_os_meters() -> Dict[str, Dict[str, Any]]:
    """Assemble every currently-available axis in the batch. Warming/N-A axes
    are OMITTED (the server renders an absent expected axis as pending;
    platform-gated axes stay honestly absent forever off-Linux). Pure read;
    never raises."""
    out: Dict[str, Dict[str, Any]] = {}
    if not _collecting():
        return out
    try:
        _sample_storage_probe()
    except Exception:  # noqa: BLE001
        pass
    for key, reader in (
        ("diskPressure", _read_disk_pressure),
        ("storageLatency", _read_storage_latency),
        ("storageFailures", _read_storage_failures),
        ("systemLoad", _read_system_load),
        ("allocChurn", _read_alloc_churn),
        ("peakRss", _read_peak_rss),
        ("cpuConsumption", _read_cpu_consumption),
        ("contextSwitching", _read_context_switching),
        ("threadFootprint", _read_thread_footprint),
        ("pageFaults", _read_page_faults),
        ("ioPressure", _read_io_pressure),
        ("cpuEntitlement", _read_cpu_entitlement),
        ("descriptorMix", _read_descriptor_mix),
        ("importChurn", _read_import_churn),
        ("asyncSlowCallbacks", _read_async_slow_callbacks),
        ("gcGenOccupancy", _read_gc_gen_occupancy),
        ("runtimeCapability", _read_runtime_capability),
    ):
        try:
            axis = reader()
        except Exception:  # noqa: BLE001
            axis = None
        if axis:
            out[key] = axis
    return out


def clear_os_meters() -> None:
    """Wipe all state (wired into ``forget()``). Nothing was hooked and no
    file was written, so clearing state is the whole job. Idempotent. Never
    raises."""
    global _started, _started_at, _last_sample_at
    global _module_baseline, _loop_debug_observed
    global _storage_attempts, _storage_failures, _sampled_peak_rss_mb
    _started = False
    _started_at = 0
    _sampled_peak_rss_mb = 0.0
    _last_sample_at = 0
    _alloc_samples.clear()
    _module_samples.clear()
    _module_baseline = None
    _rusage_samples.clear()
    _thread_samples.clear()
    _io_samples.clear()
    _storage_samples.clear()
    _storage_attempts = 0
    _storage_failures = 0
    _loop_debug_observed = False
    _slowcb_ts.clear()


def _reset_async_slow_callbacks() -> None:
    """Drop the asyncio slow-callback observations."""
    global _loop_debug_observed
    _loop_debug_observed = False
    _slowcb_ts.clear()


#: Axis key -> how to forget everything that axis has accumulated so far.
#: Read by ``host_surface.forget_foreign_reading`` when a host that cannot take
#: the reading takes over the process; see ``extra_meters.AXIS_STATE_RESETS``
#: for the full reasoning. Only axes that ACCUMULATE need an entry — the rest
#: of this module computes live from the OS and has nothing to forget.
AXIS_STATE_RESETS: Dict[str, Any] = {
    "asyncSlowCallbacks": _reset_async_slow_callbacks,
}


def drop_axis_state(key: str) -> bool:
    """Forget what ONE axis has accumulated. ``True`` when this module owns it.

    Never raises: it runs on the mount path, and the kit may never break the
    host app."""
    reset = AXIS_STATE_RESETS.get(str(key))
    if reset is None:
        return False
    try:
        reset()
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return False
    return True


# ── Test hooks (mirroring extra_meters' private-override style) ─────────────
def _force_start_for_test(started_at: Optional[int] = None) -> None:
    global _started, _started_at
    _started = True
    _started_at = started_at if started_at is not None else _now_ms()


def _set_now_for_test(ts: Optional[int]) -> None:
    global _now_override
    _now_override = ts


def _set_linux_for_test(value: Optional[bool]) -> None:
    global _linux_override
    _linux_override = value


def _set_rusage_for_test(value: Optional[Any]) -> None:
    global _rusage_override
    _rusage_override = value


def _set_io_for_test(value: Optional[Any]) -> None:
    global _io_override
    _io_override = value


def _set_fd_mix_for_test(value: Optional[Any]) -> None:
    global _fd_mix_override
    _fd_mix_override = value


def _set_cpu_for_test(value: Optional[Any]) -> None:
    global _cpu_override
    _cpu_override = value


def _set_gc_occupancy_for_test(value: Optional[Any]) -> None:
    global _gc_occupancy_override
    _gc_occupancy_override = value


def _set_capability_for_test(value: Optional[Any]) -> None:
    global _capability_override
    _capability_override = value


def _set_alloc_reader_for_test(reader: Optional[Callable[[], int]]) -> None:
    global _alloc_reader_override
    _alloc_reader_override = reader


def _set_rss_for_test(value: Optional[float]) -> None:
    global _rss_override
    _rss_override = value


def _set_statvfs_for_test(value: Optional[Any]) -> None:
    global _statvfs_override
    _statvfs_override = value


def _set_load_for_test(value: Optional[Any], uptime: Optional[float] = None) -> None:
    global _load_override, _uptime_override
    _load_override = value
    _uptime_override = uptime


def _set_containerised_for_test(value: Optional[bool]) -> None:
    global _container_override, _containerised_cache
    _container_override = value
    _containerised_cache = None


def _push_storage_for_test(duration_ms: Optional[float]) -> None:
    global _storage_attempts, _storage_failures
    _storage_attempts += 1
    if duration_ms is None:
        _storage_failures += 1
    else:
        _storage_samples.append(duration_ms)


def _push_alloc_sample_for_test(ts: int, blocks: int) -> None:
    _alloc_samples.append((ts, blocks))


def _push_module_sample_for_test(ts: int, count: int) -> None:
    _module_samples.append((ts, count))


def _set_module_baseline_for_test(n: Optional[int]) -> None:
    global _module_baseline
    _module_baseline = n


def _push_rusage_sample_for_test(
    ts: int,
    nvcsw: int,
    nivcsw: int,
    majflt: int,
    cpu_seconds: float = 0.0,
) -> None:
    if _rusage_samples and cpu_seconds < _rusage_samples[-1][4]:
        _rusage_samples.clear()
    _rusage_samples.append((ts, nvcsw, nivcsw, majflt, cpu_seconds))


def _push_thread_sample_for_test(ts: int, count: int) -> None:
    _thread_samples.append((ts, count))


def _push_io_sample_for_test(ts: int, read_b: int, write_b: int) -> None:
    _io_samples.append((ts, read_b, write_b))


def _set_loop_debug_observed_for_test(value: bool) -> None:
    global _loop_debug_observed
    _loop_debug_observed = value


def _push_slowcb_for_test(ts: int) -> None:
    _slowcb_ts.append(ts)
