"""Boosthis: the 2026-08 Python meter batch (additive, display-only axes).

Python sibling of ``lib/boosthis-runtime-node/src/extraMeters.ts``. Seven more
honest meters, all ADDITIVE — none ever feeds the Speed score — and all riding
the existing snapshot upload + PII guard:

- ``gilContention``   — how long a ``time.sleep(0)`` yield takes to get the GIL
                        back when several threads are live. Probed ONLY at
                        throttled request boundaries (no idle poller); omitted
                        on free-threaded (no-GIL) builds and single-threaded
                        apps — an honest N/A, never a fake meter.
- ``taskBacklog``     — pending asyncio tasks sampled at throttled request
                        boundaries (async apps only; sync/WSGI omit).
- ``workerRestarts``  — under gunicorn/uWSGI, how long THIS worker process has
                        been alive. A worker that is always young is being
                        recycled/OOM-killed; today that looks like nothing.
- ``swallowedErrors`` — ERROR-level log records that did NOT crash the app
                        (degraded-but-alive), observed through a CHAINED
                        ``logging`` record factory. We never add or remove a
                        handler, so the host's own ``logging.basicConfig``
                        keeps working, and records Boosthis itself logs are
                        never counted against the host.
- ``threadPoolStarvation`` — queue wait before ``ThreadPoolExecutor`` work
                        items start running (submit is wrapped, restored on
                        forget). Only present when the app uses executors.
- ``blockingAsync``   — event-loop stalls >= BLOCK_MS observed by the EXISTING
                        request-boundary loop-lag probe: sync I/O inside async
                        handlers. Async apps only.
- ``forkChurn``       — processes spawned per minute (``os.register_at_fork``
                        + a counting wrapper on ``subprocess.Popen``). Runaway
                        subprocess use is a classic invisible CPU burner.

GUEST-SAFETY: every hook is wrapped so it can never raise into the host, and
the host's own logging configuration is never touched (no handler is added or
removed — adding one silently turns ``logging.basicConfig`` into a no-op).
Every recording point re-checks the LIVE gate (``_collecting()``: started AND
not kill-switched AND not entitlement-inert), so flipping the kill switch, or an
account going revoked/unpaid mid-run, stops collection immediately — not just
display. The ``os.register_at_fork`` hook cannot be unregistered by CPython
design, so it consults that same gate and is inert forever after
``clear_extra_meters()``.

PRIVACY: numbers only — counts, durations, rates. Never a task name, log
message, command line, or module name. Captions are rebuilt server-side.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import app_scope
from .runtime_flags import is_boosthis_disabled, is_collection_gated
from .health_axes import linear_score, rating_for

# ── Bands + warm-up gates (axis-specific; display-only) ─────────────────────
GIL_GOOD_MS = 5.0
GIL_POOR_MS = 50.0
GIL_MIN_SAMPLES = 20
GIL_MIN_THREADS = 3  # main + our heartbeat + at least one real worker thread
GIL_THROTTLE_MS = 5_000
GIL_RING_CAP = 300

BACKLOG_GOOD = 100
BACKLOG_POOR = 1_000
BACKLOG_MIN_SAMPLES = 6
BACKLOG_THROTTLE_MS = 5_000
BACKLOG_RING_CAP = 120

WORKER_GOOD_RESTARTS_PER_HOUR = 2.0   # ordinary deploys / graceful reloads
WORKER_POOR_RESTARTS_PER_HOUR = 12.0  # recycled every ~5 minutes
WORKER_MIN_HISTORY_MS = 10 * 60_000   # need >=10min of boot history to judge
WORKER_WINDOW_MS = 60 * 60_000
WORKER_BOOTS_CAP = 50
# The boot history is keyed PER PROJECT (``app_scope``): two Boosthis-
# instrumented Python services sharing one machine account must never blend
# their boot histories, or each would report restarts that never happened.
WORKER_BOOTS_STEM = "worker-boots"
# The pre-1.0.0a37 machine-wide file. Never written any more; only erased.
LEGACY_WORKER_BOOTS_FILE = "~/.boosthis/worker-boots.json"

SWALLOWED_GOOD_PER_HOUR = 1.0
SWALLOWED_POOR_PER_HOUR = 60.0
SWALLOWED_MIN_WINDOW_MS = 5 * 60_000
SWALLOWED_WINDOW_MS = 60 * 60_000
SWALLOWED_RING_CAP = 400

# leakWatch — the CUSTOMER's app leaking stack traces / secrets / PII to its own
# users through what it writes to logs. ADDITIVE / display-only (never feeds the
# Speed score), same gates and ring mechanics as swallowedErrors, observed
# through the SAME chained record factory (never a second hook). Python's mount
# serves only the read-only dashboard, so there is NO customer response path to
# scan — routeClassCount is omitted entirely from the wire in this runtime.
LEAK_MIN_WINDOW_MS = 5 * 60_000
LEAK_WINDOW_MS = 60 * 60_000
LEAK_RING_CAP = 200
# Only the first this-many chars of a stringified record are scanned — bounded
# work per record; the slice is examined and dropped inside the classify scope
# and NEVER stored, logged, or put in any field.
LEAK_SCAN_MAX_CHARS = 4096

POOL_GOOD_MS = 50.0
POOL_POOR_MS = 1_000.0
POOL_MIN_SAMPLES = 10
POOL_RING_CAP = 300

BLOCK_MS = 100.0  # a loop-lag sample at/over this counts as one blocking event
BLOCK_GOOD_PER_MIN = 0.5
BLOCK_POOR_PER_MIN = 10.0
BLOCK_MIN_WINDOW_MS = 5 * 60_000
BLOCK_WINDOW_MS = 30 * 60_000
BLOCK_RING_CAP = 300

FORK_GOOD_PER_MIN = 6.0
FORK_POOR_PER_MIN = 60.0
FORK_MIN_ENABLED_MS = 60_000
FORK_WINDOW_MS = 10 * 60_000
FORK_RING_CAP = 300
QUEUE_TIME_GOOD_MS = 10.0
QUEUE_TIME_POOR_MS = 250.0
QUEUE_TIME_MIN_SAMPLES = 20
QUEUE_TIME_RING_CAP = 300
WORKER_IMBALANCE_GOOD = 1.5
WORKER_IMBALANCE_POOR = 4.0
WORKER_IMBALANCE_MIN_WORKERS = 2

# loadDeflection — how much the app's latency is OBSERVED to "bend" under
# concurrency. Each completed request files its duration into a bucket chosen by
# how many requests were in flight when it STARTED; the p75 of the least-loaded
# bucket vs the most-loaded one gives a deflection ratio (highMs / lowMs), scored
# on the ratio so a single-threaded app that never overlaps requests reports
# nothing (an honest N/A — a trickle cannot deflect), not a fabricated 1.0.
#
# HONEST CLAIM — CORRELATION, NOT CAUSATION: this compares busy moments to quiet
# moments in the SAME app, but the routes in each bucket are NOT matched, so a
# higher ratio is an OBSERVED correlation between concurrency and latency, not
# proof that concurrency caused the slowdown (a busy window may simply happen to
# hit heavier endpoints). Every caption/label the kit derives must say "observed"
# / "measured busy vs quiet" and must never assert that load CAUSED the change.
#
# Bucket floors (an in-flight count maps to the bucket whose floor it meets):
#   1, 2, 3, 5, 9, 17  →  labels 1, 2, 3-4, 5-8, 9-16, 17+
DEFLECT_BUCKET_FLOORS = (1, 2, 3, 5, 9, 17)
DEFLECT_RING_CAP = 400          # per-bucket bounded duration ring (fixed size)
DEFLECT_MIN_SAMPLES = 20        # total completed requests before we report
DEFLECT_MIN_BUCKET_SAMPLES = 5  # per used bucket, before its p75 is honest
DEFLECT_MIN_PEAK_IN_FLIGHT = 2  # no overlap ever observed → no deflection to see
DEFLECT_RATIO_GOOD = 1.5        # ratio <= this rates good
DEFLECT_RATIO_POOR = 3.0        # ratio >= this rates poor
DEFLECT_SCORE_GOOD = 1.2        # ratio <= this scores 100
DEFLECT_SCORE_POOR = 4.0        # ratio >= this scores 0
# Merged-in requestConcurrency fields: clamp + slope gate. The clamp bounds the
# duration that feeds ONLY the soloMeanMs / deflectionMsPerReq accumulators (the
# p75 bucket rings keep the raw value). The slope is null unless there are >= 10
# loaded samples (started with overlap) AND at least one solo sample.
DEFLECT_DURATION_MAX_MS = 120_000
DEFLECT_MIN_LOADED_FOR_SLOPE = 10

# ── State ────────────────────────────────────────────────────────────────────
_started = False
_started_at = 0

_gil_samples: List[float] = []
_last_gil_probe_at = 0

_backlog_samples: List[Tuple[int, int]] = []  # (ts_ms, pending_count)
_last_backlog_probe_at = 0

_swallowed_ts: List[int] = []
# swallowedErrors is observed through a CHAINED logging record factory: the host
# keeps whatever handlers it has (so ``logging.basicConfig`` still configures
# them) and we only look at records as they are created.
_swallowed_armed = False
_orig_record_factory: Optional[Callable[..., logging.LogRecord]] = None
_our_record_factory: Optional[Callable[..., logging.LogRecord]] = None

# leakWatch — a bounded ring of (ts_ms, category) where category is one of
# "secret" / "pii" / "stack". Each observation is classified into EXACTLY ONE
# category (priority secret > pii > stack) so the per-category counts always sum
# to the total. NOTHING but the timestamp and the one-word category is ever
# stored: never the matched text, the matched value, or the log line.
#
# Each entry is (timestamp, category, surface code, shape code). The last two
# come from the CLOSED, code-defined vocabulary at the top of the leakWatch
# section — integers, so they can never carry a matched value or a path.
_leak_events: List[Tuple[int, str, int, int]] = []

# cookieExposure — cookies leaving the process WITHOUT their safe flags. The kit
# is inside the running process, so it observes the outgoing ``Set-Cookie``
# headers AFTER the framework/proxy/CDN have rewritten them — config-reading
# tools are routinely wrong there. This meter reports EXPOSURE, never safety: a
# clean tile means "we did not observe an unsafe flag on the responses we
# watched", NEVER "your cookies are safe".
#
# ABSOLUTE PRIVACY RULE (mirrors leakWatch): the cookie NAME, VALUE, header text
# and route/path are NEVER stored, logged, or placed in any field/caption. Only
# COUNTS are kept — a handful of cumulative saturating scalars for this process.
COOKIE_MAX_PER_RESPONSE = 20  # inspect at most 20 Set-Cookie values per response
COOKIE_MAX_ATTR_SEGMENTS = 20  # inspect at most 20 attribute segments per cookie
COOKIE_ATTR_SCAN_CHARS = 2048  # cap the attribute-list BYTE slice we parse
COOKIE_OVERSIZE_BYTES = 4096  # a Set-Cookie whose RAW WIRE byte length exceeds this
_COOKIE_COUNTER_CAP = 2**31 - 1  # counters saturate here rather than overflow

_cookie_set_count = 0  # cookies observed being set (cumulative, this process)
_cookie_responses = 0  # responses that set at least one cookie
_cookie_no_secure = 0  # cookies set without the Secure attribute
_cookie_no_http_only = 0  # ... without HttpOnly
_cookie_no_same_site = 0  # ... without any SameSite attribute
_cookie_none_without_secure = 0  # SameSite=None without Secure (browsers reject)
_cookie_oversize = 0  # single Set-Cookie value longer than 4096 bytes

# devPosture — whether this live app is still wearing its development clothes.
# EXPOSURE, never safety: a clean tile means "none of the development settings we
# can read were on", NEVER "you are production-hardened". Additive/display-only —
# it never feeds the Speed score.
#
# READ ONCE AT STARTUP (freeze-at-init, like the cold-start contract): the checks
# are evaluated a single time when the batch starts, cached here, and the SAME
# frozen result is returned on every snapshot. Reset/override via the test seams
# below. The frozen shape is a dict of only the flags the runtime could actually
# READ this session (value 1 = the development setting is ON, 0 = read-and-off);
# an UNREADABLE check is OMITTED entirely — its absence means "we did not look",
# never a 0 standing in for "did not look". Python reads exactly TWO checks
# (debugFlag, profilingOpen); verboseErrors/sourceMaps are a genuine can't-know
# for this runtime and are always omitted.
_DEV_DEBUG_TRUTHY = frozenset({"1", "true", "True", "yes", "on"})
_dev_posture_frozen: Optional[Dict[str, int]] = None  # None until first read
_dev_posture_override: Optional[Dict[str, int]] = None  # test seam
# The ASGI/WSGI app object ``mount()`` was given, if any — the ONLY thing the
# kit holds that carries a truthy ``.debug`` (Flask/Starlette/FastAPI). Kept as
# a weak-ish plain reference (mount() sets it); consulted read-only by the
# devPosture debugFlag probe, never mutated. None when mount() was not called.
_mounted_app: Optional[Any] = None
# accessPressure — break-in pressure + traffic surge. The first status-shaped
# signal on the wire: counts of the app's OWN answers — rejected (401/403/429),
# not-found (404), server-error (5xx) — plus the peak per-minute rejection
# burst, the peak per-minute request rate against the session's own median (the
# surge ratio), and an anonymous estimate of how many distinct clients the
# rejections came from.
#
# ABSOLUTE PRIVACY RULE: no URL, no route, no client address, no header, no user
# identity ever leaves the app. The client address is hashed with a random
# per-day salt into ONE of 63 bit positions inside the local scope of
# ``note_access_outcome`` (the same windowed-OR-sketch design as leakWatch reach)
# and is retained NOWHERE — only the sketch's popcount ever crosses the wire.
#
# Banding keys on the SHAPE of traffic — share of rejections, burst against the
# session's own quiet baseline — never raw counts. Where there is no baseline
# yet the axis is honestly ABSENT (a warming tile), never a guess.
# Additive/display-only: never feeds the Speed score. Wire shape byte-identical
# across the Node/Go/Java/Python kits.
AP_BUCKET_MS = 60_000  # length of one rate bucket (the "60-second window")
AP_MAX_MINUTES = 240  # completed minute buckets kept for the baseline median
AP_MIN_TOTAL_FOR_RATING = 50  # responses required before shape is worth rating
AP_MIN_BASELINE_MINUTES = 10  # completed minutes required before a baseline exists
AP_SURGE_MIN_PEAK = 30  # a peak minute below this is noise, never a surge
AP_SURGE_WARN = 3  # surge-ratio needs-work band (peak minute vs median minute)
AP_SURGE_POOR = 8  # surge-ratio poor band
AP_SHARE_WARN = 0.2  # rejection-share needs-work band
AP_SHARE_POOR = 0.5  # rejection-share poor band
AP_SURGE_CAP = 999  # ratio ceiling so a zero-median session prints no absurd number
_AP_COUNT_MAX = 1_000_000_000  # counter saturation ceiling

_ap_total = 0
_ap_unauth = 0
_ap_forbidden = 0
_ap_limited = 0
_ap_not_found = 0
_ap_server_error = 0
_ap_protocol_answers = 0
_ap_unattributed = 0
_ap_bucket_start = 0
_ap_cur_req = 0
_ap_cur_rej = 0
_ap_req_minutes: List[int] = []  # completed per-minute request counts (median source)
_ap_peak_req = 0  # session-running peaks including the still-open minute
_ap_peak_rej = 0
_ap_sketch = 0  # 63-bit OR sketch of clients that received a rejection
_ap_salt_day = ""
_ap_salt = ""

# refusalHonesty — presence-only finished-response counts. No header value,
# route, URL, body, address, or identity is retained.
REFUSAL_MIN_WINDOW_MS = 10 * 60 * 1000
_refusal_started_at = 0
_refusal_refusals = 0
_refusal_challenged = 0
_refusal_credentialed = 0
_refusal_misleading = 0
_refusal_measurable = 1

_pool_waits: List[float] = []
_orig_pool_submit: Optional[Callable[..., Any]] = None

_block_ts: List[int] = []
_block_worst_ms = 0.0

# workerRestarts — this process's boot is written to the per-project history
# once, the first time the gate is open (start time may still be LOCKED, before
# the server handshake completes).
_worker_boot_recorded = False
# Set when a host that cannot recycle workers takes the process (see
# _reset_worker_restarts): the boot history is per PROJECT, so abstaining is the
# only way to stop answering with another process's fleet.
_worker_restarts_forgotten = False
# Unsubscribe handle for the entitlement-gate listener that retries that write
# the moment the gate opens (see start_extra_meters).
_gate_unsub: Optional[Callable[[], None]] = None

_fork_ts: List[int] = []
_fork_total = 0
_fork_hook_registered = False  # register_at_fork is PERMANENT — register once
_in_popen = threading.local()  # suppresses the at-fork hook during Popen (no double count)
_orig_popen_init: Optional[Callable[..., Any]] = None
_queue_time_samples: List[float] = []
_worker_completed = 0
_worker_identity: Optional[str] = None

# loadDeflection — a FIXED-SIZE array of per-bucket duration rings (one ring per
# DEFLECT_BUCKET_FLOORS entry), the running total of completed requests, and the
# highest simultaneous in-flight count ever seen. No per-request allocation
# beyond appending one float into an already-allocated ring. State is per
# PROCESS (== per project, the same scoping the on-disk meters use), never
# shared across projects.
_deflect_buckets: List[List[float]] = [[] for _ in DEFLECT_BUCKET_FLOORS]
_deflect_total = 0
_deflect_in_flight_sum = 0  # sum of start-time in-flight counts (for meanInFlight)
_deflect_peak_in_flight = 0

# soloMeanMs + deflectionMsPerReq accumulators (merged in from the retired
# requestConcurrency axis). Fed at the SAME point a bucket sample is recorded,
# from the CLAMPED duration (y = min(DEFLECT_DURATION_MAX_MS, max(0, durationMs)))
# so a rogue measurement can never skew either the solo mean or the least-squares
# slope. The p75 bucket rings keep the RAW duration; only these O(1) running
# scalars see the clamp. soloCount = samples that started ALONE (in-flight == 1);
# loadedCount = samples that started with overlap (in-flight >= 2).
_deflect_solo_count = 0
_deflect_solo_total_ms = 0.0
_deflect_loaded_count = 0
# Least-squares sums over ALL recorded samples (x = startInFlight, y = clamped
# durationMs), for the deflectionMsPerReq slope.
_deflect_ls_n = 0
_deflect_ls_sum_x = 0.0
_deflect_ls_sum_y = 0.0
_deflect_ls_sum_xy = 0.0
_deflect_ls_sum_x2 = 0.0

# Test overrides
_now_override: Optional[int] = None
_process_age_override: Optional[int] = None
_worker_env_override: Optional[bool] = None
_worker_boots_override: Optional[List[int]] = None
_worker_boots_path_override: Optional[str] = None
_worker_counts_override: Optional[Dict[str, int]] = None
_gil_enabled_override: Optional[bool] = None
_thread_count_override: Optional[int] = None


def _now_ms() -> int:
    if _now_override is not None:
        return _now_override
    return int(time.time() * 1000)


def _collecting() -> bool:
    """The LIVE recording gate, consulted at every record point (never only at
    start): the batch must be armed AND Boosthis must still be allowed to
    collect. ``is_collection_gated()`` covers the env kill-switch and the
    server-authority entitlement lock (revoked / unpaid / paused / tampered /
    offline-grace lapsed / never activated), so turning Boosthis off mid-run
    means the collectors go quiet at once."""
    if not _started:
        return False
    try:
        return not is_collection_gated()
    except Exception:  # noqa: BLE001
        return False


def _gil_enabled() -> bool:
    """True unless this is a free-threaded (no-GIL) CPython build (3.13+
    ``sys._is_gil_enabled``). On such builds the meter is an honest N/A."""
    if _gil_enabled_override is not None:
        return _gil_enabled_override
    try:
        probe = getattr(sys, "_is_gil_enabled", None)
        if callable(probe):
            return bool(probe())
    except Exception:  # noqa: BLE001
        pass
    return True


def _is_worker_server() -> bool:
    """Best-effort detection that this process is a gunicorn/uWSGI worker."""
    if _worker_env_override is not None:
        return _worker_env_override
    try:
        soft = (os.environ.get("SERVER_SOFTWARE") or "").lower()
        if "gunicorn" in soft or "uwsgi" in soft:
            return True
        for key in os.environ:
            up = key.upper()
            if up.startswith("GUNICORN_") or up.startswith("UWSGI_"):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _read_process_age_ms() -> Optional[int]:
    """Process age via /proc (same technique as runtime_vitals cold start; kept
    local to avoid an import cycle). None when unavailable."""
    if _process_age_override is not None:
        return _process_age_override
    try:
        with open("/proc/self/stat", "r") as fh:
            stat = fh.read()
        rparen = stat.rfind(")")
        if rparen < 0:
            return None
        rest = stat[rparen + 2 :].split()
        start_ticks = int(rest[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        if clk_tck <= 0:
            return None
        with open("/proc/uptime", "r") as fh:
            uptime_secs = float(fh.read().split()[0])
        return int((uptime_secs - start_ticks / clk_tck) * 1000)
    except Exception:  # noqa: BLE001
        return None


# ── leakWatch vocabulary ────────────────────────────────────────────────────
#
# Everything the leak meter is allowed to SAY about a detection is declared
# here, in code: a surface kind, a shape bit, and one fixed word per shape. A
# reading may carry these integers and these words and nothing else — never a
# matched substring, a value, a logger name or a path.

# Records Boosthis itself logs must NEVER count as the host app's swallowed
# errors — that would let the meter accuse a healthy app of hiding OUR
# problems. Matched as an exact logger name or a dotted parent
# ("boosthis.mcp"), plus the pre-rename namespace.
_OUR_LOGGER_ROOTS = ("boosthis", "boosten")

# Surface kinds — the KIND of place a detection was seen. Closed set. Python
# mounts a read-only dashboard and watches the app's own log output, so the log
# surface is the only one it can observe; the reply surface of the runtimes
# that sit in a response path is ABSENT here rather than reported as zero.
LEAK_SURFACE_LOG = 1  # the app's own log/console output

# Detection shapes — WHICH pattern matched. One bit each; a reading reports the
# OR of every shape seen in the window (``shapeMask``). Declared ABOVE the word
# and pattern tables that read them: a module-level table reading a name bound
# below it raises at import, and the kit would then refuse to load at all.
LEAK_SHAPE_JWT = 1
LEAK_SHAPE_BEARER = 2
LEAK_SHAPE_AWS_KEY_ID = 4
LEAK_SHAPE_PRIVATE_KEY = 8
LEAK_SHAPE_NAMED_VALUE = 16
LEAK_SHAPE_EMAIL = 32
LEAK_SHAPE_STACK_FRAME = 64

# Words for the matched shapes — read from the closed vocabulary above, never
# from anything the detector matched.
_LEAK_SHAPE_WORDS: Tuple[Tuple[int, str], ...] = (
    (LEAK_SHAPE_JWT, "a JSON Web Token"),
    (LEAK_SHAPE_BEARER, "a bearer credential"),
    (LEAK_SHAPE_AWS_KEY_ID, "an AWS access-key id"),
    (LEAK_SHAPE_PRIVATE_KEY, "a private-key header"),
    (LEAK_SHAPE_NAMED_VALUE, "a named api-key/secret/token value"),
    (LEAK_SHAPE_EMAIL, "an email address"),
    (LEAK_SHAPE_STACK_FRAME, "a stack frame"),
)

# ── Placeholders are not credentials ────────────────────────────────────────
#
# A value written in a placeholder form is, by construction, the ABSENCE of a
# credential: it is what a message prints WHERE a credential would go. Only the
# two shapes that read a free-form value — bearer, and the named
# api-key/secret/token value — can ever meet one; the other three match a fixed
# alphabet no placeholder satisfies.
#
# This narrowing was written after our own site's Leak Watch reported 50
# secret-class detections an hour, every one of them the string
# "Authorization: Bearer <project-key>" inside our own 403 body telling a caller
# HOW to send its key. Counting an instruction as a leak makes the tile
# unactionable for us and for every customer who documents an auth header in an
# error reply, which is the ordinary thing to do.
#
# The set is small and CLOSED — bracketed forms, template expressions,
# redaction runs, and a handful of ALL-CAPS stand-in words. It is NOT a general
# "looks unimportant" filter: anything outside it is still a detection, and
# widening it would hide real leaks. See docs/leak-detection-vocabulary.md.
#
# The pattern anchors at the START of the value and then allows only NON-VALUE
# characters to the end, because a captured value runs to the next space and so
# drags whatever punctuation the surrounding sentence or JSON encoding put
# there ("Bearer <project-key>)\"}"). A real credential can still follow a
# bracketed form — "<x>realkey123456" is not a placeholder, because its tail is
# made of value characters. The words are matched CASE-SENSITIVELY in caps,
# which is how a document writes a stand-in; lower-case "sampleofakey…" is a
# plausible real credential and stays a detection.
_LEAK_VALUE_LTRIM = "(\"'`["
_LEAK_PLACEHOLDER_VALUE_RE = re.compile(
    r"^(?:<[^>]*>|\{\{[^}]*\}\}|\$\{[^}]*\}|\*{3,}|[xX]{3,}|\.{3,}|\u2026"
    r"|(?:YOUR|EXAMPLE|SAMPLE|PLACEHOLDER|REPLACE|INSERT)[A-Z0-9_-]*)"
    r"[^A-Za-z0-9_-]*$"
)

# How many matches of ONE shape a single scan inspects. A document that shows a
# placeholder and then leaks a real credential below it is the ordinary case,
# so a shape must not end at its FIRST match — but the walk stays bounded, on
# top of the already-bounded slice, so no input can spin here.
_LEAK_MAX_MATCHES_PER_SHAPE = 16

# ── leakWatch detection patterns (conservative; log-output categories only) ──
# These match the SHAPE of a leak, never its content. The matched text decides
# the category and the shape code, and is discarded immediately after.
#
# secret (priority 1), in the order they are tried — the reported shape is
# deterministically the FIRST that matched. The two free-form shapes capture
# their value as group 1 so it can be judged against the placeholder set; the
# other three capture nothing, because a fixed alphabet leaves nothing to judge.
_LEAK_SECRET_RES: Tuple[Tuple[Any, int], ...] = (
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), LEAK_SHAPE_JWT),
    (re.compile(r"bearer\s+(\S{8,})", re.IGNORECASE), LEAK_SHAPE_BEARER),
    (re.compile(r"AKIA[0-9A-Z]{16}"), LEAK_SHAPE_AWS_KEY_ID),
    (re.compile(r"-----BEGIN [\s\S]{0,64}PRIVATE KEY"), LEAK_SHAPE_PRIVATE_KEY),
    (
        re.compile(
            r"(?:api[_-]?key|secret|token)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-]{16,})",
            re.IGNORECASE,
        ),
        LEAK_SHAPE_NAMED_VALUE,
    ),
)
# pii (priority 2): the kit's EXISTING email regex — reused from ``pii.py`` so
# the two stay in lockstep, never a second copy.
from .pii import _EMAIL_RE as _LEAK_EMAIL_RE  # noqa: E402

# stack (priority 3): a Python traceback header written to the log. The spec's
# cross-runtime stack patterns list "Traceback (most recent call last)" for
# Python; that is the only stack shape counted from Python log output.
_LEAK_STACK_MARKER = "Traceback (most recent call last)"


def _is_leak_placeholder(value: str) -> bool:
    """True when the matched VALUE is a placeholder rather than a credential."""
    try:
        v = value.lstrip(_LEAK_VALUE_LTRIM)
        return bool(v) and _LEAK_PLACEHOLDER_VALUE_RE.match(v) is not None
    except Exception:  # noqa: BLE001
        return False

def _has_real_value(rx: Any, text: str) -> bool:
    """True when this shape has at least one match that is NOT a placeholder.

    Walks successive matches rather than judging the shape by its first one: a
    documented ``Bearer <project-key>`` above a real token must not hide the
    token. Shapes with no capture group have no free-form value to judge, so
    any match of theirs counts. Returns a bool — no matched text escapes."""
    try:
        for i, m in enumerate(rx.finditer(text)):
            if i >= _LEAK_MAX_MATCHES_PER_SHAPE:
                break
            if not m.groups():
                return True
            if not _is_leak_placeholder(m.group(1) or ""):
                return True
        return False
    except Exception:  # noqa: BLE001
        return False
def _classify_leak(text: str) -> Optional[Tuple[str, int]]:
    """Return the ONE category ("secret" / "pii" / "stack") ``text`` leaks paired
    with the closed numeric SHAPE code that decided it, by priority
    secret > pii > stack, or None. ``text`` is a bounded slice that the caller
    drops immediately; nothing derived from it is returned — the category is a
    fixed word and the shape is a code-defined integer."""
    try:
        for rx, shape in _LEAK_SECRET_RES:
            # A placeholder value ends neither the shape nor the classification:
            # the scan walks the shape's LATER matches for a real value, and
            # only then falls through to the remaining shapes. Only the two
            # free-form shapes have a capture group to judge at all.
            if _has_real_value(rx, text):
                return ("secret", shape)
        if _LEAK_EMAIL_RE.search(text):
            return ("pii", LEAK_SHAPE_EMAIL)
        if _LEAK_STACK_MARKER in text:
            return ("stack", LEAK_SHAPE_STACK_FRAME)
    except Exception:  # noqa: BLE001
        return None
    return None


def _note_leak_from_record(record: logging.LogRecord) -> None:
    """Scan a HOST record's formatted message (first ``LEAK_SCAN_MAX_CHARS``
    chars only) for a leak SHAPE and, if found, append ONE (timestamp, category)
    to the bounded ring. Called from inside ``_note_log_record`` on the SAME
    chained factory — never a second hook — and only after the caller has
    already applied the ``_collecting()`` gate and the ``_is_our_record``
    self-exclusion, so the kit's own logging is never scanned or counted.

    PRIVACY: the stringified message and every match are LOCAL to this function
    and dropped on return. Only the timestamp and the one-word category survive.
    Never raises into the host."""
    try:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001
            # A record whose args do not format must never break the host.
            return
        if not text:
            return
        if len(text) > LEAK_SCAN_MAX_CHARS:
            text = text[:LEAK_SCAN_MAX_CHARS]
        classified = _classify_leak(text)
        # Drop the scanned text reference before touching any shared state.
        text = ""
        if classified is None:
            return
        category, shape = classified
        _leak_events.append((_now_ms(), category, LEAK_SURFACE_LOG, shape))
        if len(_leak_events) > LEAK_RING_CAP:
            del _leak_events[0 : len(_leak_events) - LEAK_RING_CAP]
    except Exception:  # noqa: BLE001
        pass


def _is_our_record(record: logging.LogRecord) -> bool:
    """True when this record came out of one of Boosthis's own loggers."""
    try:
        name = record.name or ""
    except Exception:  # noqa: BLE001
        return False
    for root in _OUR_LOGGER_ROOTS:
        if name == root or name.startswith(root + "."):
            return True
    return False


def _note_log_record(record: logging.LogRecord) -> None:
    """Count one ERROR+ record from the HOST app. Records ONLY a timestamp —
    never the message, logger name, or module. Never raises into the host's
    logging call."""
    try:
        # The 2026-08 os_meters batch observes asyncio's slow-callback
        # warnings through this same chained factory (no second hook).
        try:
            from . import os_meters

            os_meters.note_log_record(record)
        except Exception:  # noqa: BLE001
            pass
        # The self-exclusion + live gate are shared by BOTH consumers below so
        # the kit's own logging is never scanned or counted, and both go quiet
        # the moment collection is gated.
        if not _collecting():
            return
        if _is_our_record(record):
            return
        # leakWatch — scan the HOST record's message at EVERY level (a leaked
        # stack/secret/PII can be logged at any severity), piggybacking THIS
        # same chained factory. No second hook.
        _note_leak_from_record(record)
        # swallowedErrors — count only ERROR+ records (degraded-but-alive).
        if record.levelno < logging.ERROR:
            return
        _swallowed_ts.append(_now_ms())
        if len(_swallowed_ts) > SWALLOWED_RING_CAP:
            del _swallowed_ts[0 : len(_swallowed_ts) - SWALLOWED_RING_CAP]
    except Exception:  # noqa: BLE001
        pass


def _install_record_factory() -> None:
    """Observe ERROR records by CHAINING the ``logging`` record factory.

    GUEST-SAFETY: the obvious approach — adding a handler to the root logger —
    silently changes the host app's own logging, because ``logging.basicConfig``
    does nothing at all once the root logger has a handler. Apps very commonly
    call it after their startup wiring, so their formatting/output would quietly
    disappear and they would blame their own code. A chained record factory
    leaves the handler list exactly as the app arranged it."""
    global _orig_record_factory, _our_record_factory, _swallowed_armed
    try:
        orig = logging.getLogRecordFactory()

        def _factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = orig(*args, **kwargs)
            _note_log_record(record)
            return record

        logging.setLogRecordFactory(_factory)
        _orig_record_factory = orig
        _our_record_factory = _factory
        _swallowed_armed = True
    except Exception:  # noqa: BLE001
        _orig_record_factory = None
        _our_record_factory = None
        _swallowed_armed = False


def _restore_record_factory() -> None:
    """Put the host's record factory back. If another library chained ON TOP of
    ours we leave the chain alone — unhooking would drop THEIR factory too. Ours
    is already inert in that case, because ``_collecting()`` is False."""
    global _orig_record_factory, _our_record_factory, _swallowed_armed
    try:
        if _our_record_factory is not None and _orig_record_factory is not None:
            if logging.getLogRecordFactory() is _our_record_factory:
                logging.setLogRecordFactory(_orig_record_factory)
    except Exception:  # noqa: BLE001
        pass
    _orig_record_factory = None
    _our_record_factory = None
    _swallowed_armed = False


def _record_fork() -> None:
    global _fork_total
    try:
        if not _collecting():
            return
        if getattr(_in_popen, "active", False):
            return  # Popen already counted this spawn — don't double-count the fork
        _fork_total += 1
        _fork_ts.append(_now_ms())
        if len(_fork_ts) > FORK_RING_CAP:
            del _fork_ts[0 : len(_fork_ts) - FORK_RING_CAP]
    except Exception:  # noqa: BLE001
        pass


def start_extra_meters() -> None:
    """Arm all seven collectors. Idempotent, no-op under the kill-switch,
    never raises. Called from ``enable_telemetry``."""
    global _started, _started_at, _orig_pool_submit
    global _fork_hook_registered, _orig_popen_init
    # Arming is NOT gated on the entitlement lock: enable_telemetry runs this
    # BEFORE the server handshake, so a locked-at-start kit must still wire the
    # hooks and let the LIVE ``_collecting()`` gate decide whether each one may
    # record. The env kill-switch does keep us from arming at all.
    if is_boosthis_disabled() or _started:
        return
    _started = True
    _started_at = _now_ms()

    # workerRestarts — put this worker's boot on the per-project record (a no-op
    # while the kit is still locked; retried at the next request boundary).
    _maybe_record_worker_boot()
    # ...and retry the moment the entitlement gate opens. enable_telemetry runs
    # BEFORE the server handshake, so the write above is normally still locked
    # out, and an ASGI mount has no runtime-vitals request boundary to retry on
    # (only Flask does) — without this a FastAPI/Starlette worker would never
    # put its boot on record at all. The latch makes whichever fires second a
    # single bool read.
    global _gate_unsub
    try:
        if _gate_unsub is None:
            from boosthis.kill_switch import subscribe_entitlement

            _gate_unsub = subscribe_entitlement(_maybe_record_worker_boot)
    except Exception:  # noqa: BLE001
        _gate_unsub = None

    # swallowedErrors — chained record factory (never a handler; see above).
    _install_record_factory()

    # threadPoolStarvation — wrap ThreadPoolExecutor.submit to time queue wait.
    try:
        from concurrent.futures import ThreadPoolExecutor

        if _orig_pool_submit is None:
            orig = ThreadPoolExecutor.submit

            def _timed_submit(pool: Any, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
                try:
                    submitted = time.perf_counter()

                    def _wrapped(*a: Any, **kw: Any) -> Any:
                        try:
                            if _collecting():
                                wait_ms = (time.perf_counter() - submitted) * 1000.0
                                _pool_waits.append(max(0.0, wait_ms))
                                if len(_pool_waits) > POOL_RING_CAP:
                                    del _pool_waits[0 : len(_pool_waits) - POOL_RING_CAP]
                        except Exception:  # noqa: BLE001
                            pass
                        return fn(*a, **kw)

                    return orig(pool, _wrapped, *args, **kwargs)
                except Exception:  # noqa: BLE001
                    return orig(pool, fn, *args, **kwargs)

            ThreadPoolExecutor.submit = _timed_submit  # type: ignore[method-assign]
            _orig_pool_submit = orig
    except Exception:  # noqa: BLE001
        _orig_pool_submit = None

    # forkChurn — permanent (guarded) fork hook + counting Popen wrapper.
    try:
        if not _fork_hook_registered:
            os.register_at_fork(after_in_parent=_record_fork)
            _fork_hook_registered = True
    except Exception:  # noqa: BLE001
        pass
    try:
        if _orig_popen_init is None:
            orig_init = subprocess.Popen.__init__

            def _counting_init(self: Any, *args: Any, **kwargs: Any) -> None:
                _record_fork()
                # Suppress the at-fork hook for the fork Popen itself performs
                # on POSIX — one subprocess must count exactly once.
                _in_popen.active = True
                try:
                    orig_init(self, *args, **kwargs)
                finally:
                    _in_popen.active = False

            subprocess.Popen.__init__ = _counting_init  # type: ignore[method-assign]
            _orig_popen_init = orig_init
    except Exception:  # noqa: BLE001
        _orig_popen_init = None


def note_request_boundary() -> None:
    """Piggyback probes on a finished request (called from
    ``runtime_vitals.note_request`` — no new call sites in the host). Fires the
    throttled GIL probe and asyncio-backlog sample. Never raises."""
    global _last_gil_probe_at, _last_backlog_probe_at
    if not _collecting():
        return
    # A worker that was still LOCKED when telemetry started has no boot on
    # record yet — the first allowed boundary puts it there (the timestamp is
    # derived from the process age, so recording it late is still correct).
    _maybe_record_worker_boot()
    try:
        ts = _now_ms()
        # GIL probe: only meaningful with several live threads and a GIL.
        if ts - _last_gil_probe_at >= GIL_THROTTLE_MS:
            threads = (
                _thread_count_override
                if _thread_count_override is not None
                else threading.active_count()
            )
            if threads >= GIL_MIN_THREADS and _gil_enabled():
                _last_gil_probe_at = ts
                t0 = time.perf_counter()
                time.sleep(0)  # yield: releases + re-acquires the GIL
                dt_ms = (time.perf_counter() - t0) * 1000.0
                _gil_samples.append(max(0.0, dt_ms))
                if len(_gil_samples) > GIL_RING_CAP:
                    del _gil_samples[0 : len(_gil_samples) - GIL_RING_CAP]
        # asyncio task backlog (async apps only).
        if ts - _last_backlog_probe_at >= BACKLOG_THROTTLE_MS:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                _last_backlog_probe_at = ts
                pending = len(asyncio.all_tasks(loop))
                _backlog_samples.append((ts, pending))
                if len(_backlog_samples) > BACKLOG_RING_CAP:
                    del _backlog_samples[0 : len(_backlog_samples) - BACKLOG_RING_CAP]
    except Exception:  # noqa: BLE001
        pass


def note_loop_lag_sample(lag_ms: float) -> None:
    """Fed by the EXISTING event-loop-lag probe (async apps only). A sample at
    or over BLOCK_MS counts as one blocking event. Never raises."""
    global _block_worst_ms
    if not _collecting():
        return
    try:
        if lag_ms < BLOCK_MS:
            return
        _block_ts.append(_now_ms())
        if len(_block_ts) > BLOCK_RING_CAP:
            del _block_ts[0 : len(_block_ts) - BLOCK_RING_CAP]
        if lag_ms > _block_worst_ms:
            _block_worst_ms = lag_ms
    except Exception:  # noqa: BLE001
        pass


def note_queue_start(headers: Any) -> None:
    """Record one front-door arrival stamp at an existing request hook."""
    if not _collecting():
        return
    try:
        raw: Any = None
        if hasattr(headers, "items"):
            lowered = {str(k).lower(): v for k, v in headers.items()}
            raw = lowered.get("x-request-start") or lowered.get("x-queue-start")
        else:
            for pair in headers or []:
                if not isinstance(pair, (tuple, list)) or len(pair) < 2:
                    continue
                name = bytes(pair[0]).decode("latin-1").lower()
                if name in ("x-request-start", "x-queue-start"):
                    raw = bytes(pair[1]).decode("latin-1")
                    break
        if raw is None:
            return
        text = str(raw).strip()
        if text.lower().startswith("t="):
            text = text[2:].strip()
        stamp = float(text)
        if stamp < 10_000_000_000:
            stamp *= 1000.0
        queued_ms = float(_now_ms()) - stamp
        if queued_ms < 0:
            return
        _queue_time_samples.append(queued_ms)
        if len(_queue_time_samples) > QUEUE_TIME_RING_CAP:
            del _queue_time_samples[0 : len(_queue_time_samples) - QUEUE_TIME_RING_CAP]
    except Exception:  # noqa: BLE001
        pass


def note_worker_completed() -> None:
    """Count one completed worker unit; snapshots read this bounded scalar."""
    global _worker_completed
    if not _collecting() or not _is_worker_server():
        return
    _worker_completed += 1


def _deflect_bucket_index(in_flight: int) -> int:
    """Map an in-flight count onto its fixed bucket slot (highest floor it
    meets). A count of 0 (should not happen — start marks at least 1) clamps to
    the lowest bucket."""
    idx = 0
    for i, floor in enumerate(DEFLECT_BUCKET_FLOORS):
        if in_flight >= floor:
            idx = i
        else:
            break
    return idx


def note_deflection_sample(start_in_flight: int, duration_ms: float) -> None:
    """File one COMPLETED request's duration into the loadDeflection bucket for
    how many requests were in flight when it STARTED. Fed from the existing
    request-timing path (the ASGI trace middleware and the Flask/WSGI
    request-boundary), so there is no new host call site and no new poller.

    Keeps ONLY counts and durations — never a route, status, or any request
    content. Bounded work: one bucket index, one append into a fixed-size ring,
    a handful of running scalars. Never raises into the host."""
    global _deflect_total, _deflect_in_flight_sum, _deflect_peak_in_flight
    global _deflect_solo_count, _deflect_solo_total_ms, _deflect_loaded_count
    global _deflect_ls_n, _deflect_ls_sum_x, _deflect_ls_sum_y
    global _deflect_ls_sum_xy, _deflect_ls_sum_x2
    if not _collecting():
        return
    try:
        n = int(start_in_flight)
        if n < 1:
            n = 1
        dt = float(duration_ms)
        # Reject NaN/inf (dt != dt is the NaN test) rather than let a corrupt
        # duration into a bucket where it would poison every p75. A negative
        # clock delta clamps to 0.
        if dt != dt or dt in (float("inf"), float("-inf")):
            return
        if dt < 0.0:
            dt = 0.0
        ring = _deflect_buckets[_deflect_bucket_index(n)]
        ring.append(dt)  # RAW duration — the p75 rings never see the clamp
        if len(ring) > DEFLECT_RING_CAP:
            del ring[0 : len(ring) - DEFLECT_RING_CAP]
        _deflect_total += 1
        _deflect_in_flight_sum += n
        if n > _deflect_peak_in_flight:
            _deflect_peak_in_flight = n
        # soloMeanMs / deflectionMsPerReq accumulators — fed here (the same point
        # the ring records this sample) from the CLAMPED duration so a rogue
        # value cannot skew the mean or the slope.
        clamped = min(float(DEFLECT_DURATION_MAX_MS), dt)
        if n == 1:
            _deflect_solo_count += 1
            _deflect_solo_total_ms += clamped
        elif n >= 2:
            _deflect_loaded_count += 1
        _deflect_ls_n += 1
        _deflect_ls_sum_x += n
        _deflect_ls_sum_y += clamped
        _deflect_ls_sum_xy += n * clamped
        _deflect_ls_sum_x2 += n * n
    except Exception:  # noqa: BLE001
        pass


def _cookie_saturate(value: int) -> int:
    """Clamp a cumulative cookieExposure counter to its cap so a long-lived
    process can never overflow it."""
    return value if value < _COOKIE_COUNTER_CAP else _COOKIE_COUNTER_CAP


def _iter_set_cookie_values(headers: Any) -> List[bytes]:
    """Pull the outgoing ``Set-Cookie`` values out of an ASGI ``http`` response
    header list — a list of lowercase ``(bytes, bytes)`` tuples, one entry per
    Set-Cookie (multi-valued headers arrive as repeated tuples). Case-insensitive
    header-name match. Returns the RAW value bytes (no decode — Python is the
    only kit that holds the true wire bytes, see ``_cookie_value_is_oversize``),
    at most ``COOKIE_MAX_PER_RESPONSE`` of them; anything past the cap is ignored
    (bounded work). The returned bytes live only in this local parsing scope —
    counted, then discarded, NEVER stored, logged, or placed in any field."""
    out: List[bytes] = []
    try:
        for raw_key, raw_val in headers or []:
            if len(out) >= COOKIE_MAX_PER_RESPONSE:
                break
            try:
                name = bytes(raw_key).decode("latin-1")
            except Exception:  # noqa: BLE001
                continue
            if name.lower() != "set-cookie":
                continue
            # RAW bytes — no decode. The oversize check measures the true wire
            # byte length, and the attribute tail is parsed from a bounded byte
            # slice below (never a decode/re-encode of the whole value).
            try:
                out.append(bytes(raw_val))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return out
    return out


def _cookie_value_is_oversize(raw: bytes) -> bool:
    """True when the WHOLE raw Set-Cookie value exceeds 4096 bytes ON THE WIRE.

    Python holds the ORIGINAL ASGI byte string, so we take its length directly —
    NEVER a decode-then-re-encode, which would resize invalid bytes (4096 raw
    ``0xFF`` bytes would balloon to 8192 as UTF-8 and be falsely marked
    oversize). This is DELIBERATELY the raw wire byte count. The other kits
    measure the UTF-8 byte length of the header STRING, which is identical for
    ASCII and for valid UTF-8 — the divergence only shows on invalid bytes,
    which no real cookie contains. A bare ``len()`` on the bytes is O(1)."""
    return len(raw) > COOKIE_OVERSIZE_BYTES


def _parse_cookie_flags(raw: bytes) -> tuple[bool, bool, bool, bool]:
    """Token-parse ONE raw Set-Cookie byte value into
    ``(has_secure, has_http_only, has_same_site, same_site_none)``.

    NOT substring matching — a cookie NAMED ``insecure_id`` or a VALUE containing
    the literal text ``httponly`` must not make a missing flag look present.

      a. Everything before the FIRST ``;`` is the ``name=value`` pair and is
         NEVER inspected for attributes. No ``;`` → no attributes at all.
      b. The byte remainder AFTER that first ``;`` is capped at
         ``COOKIE_ATTR_SCAN_CHARS`` BYTES BEFORE any copy/decode — this bounds
         the work so an attacker's 100 KB tail is never fully materialised. The
         short slice is decoded latin-1 (1:1 with bytes; ASCII case handling).
      c. Split on ``;``, take at most ``COOKIE_MAX_ATTR_SEGMENTS`` segments, trim
         ASCII whitespace, and lowercase ONLY those short segments.
      d. For each segment, find the FIRST ``=``: the attribute NAME is what
         precedes it (trimmed, lowercased), the VALUE is what follows (trimmed,
         lowercased); a segment with no ``=`` has just a name. Then:
           * name == ``secure``   → Secure present
           * name == ``httponly`` → HttpOnly present
           * name == ``samesite`` AND the segment HAS an ``=`` → SameSite present
             with that value (``none`` is the None case)
         Whitespace around the ``=`` is tolerated (browsers accept
         ``SameSite = Lax``), so calling it missing would be a FALSE finding.
      e. A bare ``samesite`` with NO ``=`` is still ABSENT."""
    semi = raw.find(b";")
    if semi < 0:
        return (False, False, False, False)
    # Slice the BYTES first (bounded), then decode the short tail latin-1.
    tail = raw[semi + 1 : semi + 1 + COOKIE_ATTR_SCAN_CHARS]
    remainder = tail.decode("latin-1", "replace")
    has_secure = False
    has_http_only = False
    has_same_site = False
    same_site_none = False
    for segment in remainder.split(";")[:COOKIE_MAX_ATTR_SEGMENTS]:
        seg = segment.strip()
        if not seg:
            continue
        eq = seg.find("=")
        if eq < 0:
            attr_name = seg.lower()
            has_value = False
            attr_value = ""
        else:
            attr_name = seg[:eq].strip().lower()
            attr_value = seg[eq + 1 :].strip().lower()
            has_value = True
        if attr_name == "secure":
            has_secure = True
        elif attr_name == "httponly":
            has_http_only = True
        elif attr_name == "samesite" and has_value:
            has_same_site = True
            if attr_value == "none":
                same_site_none = True
    return (has_secure, has_http_only, has_same_site, same_site_none)


def note_cookie_headers(headers: Any) -> None:
    """cookieExposure collector — file the safe-flag EXPOSURE counts for one
    response's outgoing ``Set-Cookie`` headers. Fed from the ASGI trace
    middleware's existing ``http.response.start`` hook (the same point the kit
    already reads status / content-type), so there is no second wrapper.

    Attribute matching is TOKEN-based (see ``_parse_cookie_flags``) and
    byte-for-byte identical to the other server kits:

      * a cookie missing ``Secure`` increments ``noSecure``;
      * missing ``HttpOnly`` increments ``noHttpOnly``;
      * no ``SameSite=`` attribute at all increments ``noSameSite``;
      * ``SameSite=None`` WITHOUT ``Secure`` ALSO increments
        ``noneWithoutSecure`` (a cookie can count in both that and ``noSecure`` —
        they are separate columns, by design);
      * a value whose WHOLE raw wire text exceeds 4096 bytes increments
        ``oversize`` (Python measures the true ASGI byte length; see
        ``_cookie_value_is_oversize`` for the deliberate cross-kit divergence).

    At most ``COOKIE_MAX_PER_RESPONSE`` values are inspected per response; the
    rest are ignored. Per-cookie work is bounded (see ``_parse_cookie_flags`` /
    ``_cookie_value_is_oversize`` — no unbounded lowercase/encode/copy of a huge
    header). Counters saturate rather than overflow. Keeps ONLY counts — never a
    cookie name, value, header text, or route. Never raises into the host."""
    global _cookie_set_count, _cookie_responses, _cookie_no_secure
    global _cookie_no_http_only, _cookie_no_same_site
    global _cookie_none_without_secure, _cookie_oversize
    if not _collecting():
        return
    try:
        values = _iter_set_cookie_values(headers)
        if not values:
            return
        _cookie_responses = _cookie_saturate(_cookie_responses + 1)
        for value in values:
            _cookie_set_count = _cookie_saturate(_cookie_set_count + 1)
            (
                has_secure,
                has_http_only,
                has_same_site,
                same_site_none,
            ) = _parse_cookie_flags(value)
            if not has_secure:
                _cookie_no_secure = _cookie_saturate(_cookie_no_secure + 1)
            if not has_http_only:
                _cookie_no_http_only = _cookie_saturate(_cookie_no_http_only + 1)
            if not has_same_site:
                _cookie_no_same_site = _cookie_saturate(_cookie_no_same_site + 1)
            if same_site_none and not has_secure:
                _cookie_none_without_secure = _cookie_saturate(
                    _cookie_none_without_secure + 1
                )
            if _cookie_value_is_oversize(value):
                _cookie_oversize = _cookie_saturate(_cookie_oversize + 1)
    except Exception:  # noqa: BLE001
        pass  # instrumentation must never disturb the response


def _ap_r0(n: float) -> int:
    """Round-half-up to an integer, matching Node's ``Math.round`` (never
    Python's banker's rounding) so scores/percentages are byte-identical."""
    import math

    return int(math.floor(n + 0.5))


def _ap_r1(n: float) -> float:
    """Round-half-up to one decimal, matching Node's ``r1`` (Math.round(n*10)/10)."""
    return _ap_r0(n * 10) / 10.0


def _ap_bump(n: int) -> int:
    """Saturating counter increment — counts stop climbing rather than overflow."""
    return n if n >= _AP_COUNT_MAX else n + 1


def _ap_daily_salt() -> str:
    """Per-UTC-day random salt for the client-address hash. Never derived from
    anything, never persisted, never uploaded — rotating it daily means the
    sketch counts device-DAYS, exactly like the leakWatch reach design."""
    global _ap_salt_day, _ap_salt
    import datetime

    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if day != _ap_salt_day or _ap_salt == "":
        _ap_salt_day = day
        _ap_salt = os.urandom(16).hex()
    return _ap_salt


def _ap_roll_buckets(now: int) -> None:
    """Roll the open minute bucket forward to ``now``, filing completed minutes
    (including empty ones — a quiet minute IS the baseline) into the ring.
    Called ONLY on the WRITE path, never on read, so a read clock can't file
    phantom quiet minutes into the baseline."""
    global _ap_bucket_start, _ap_cur_req, _ap_cur_rej, _ap_req_minutes
    if _ap_bucket_start == 0:
        _ap_bucket_start = now
        return
    gap = (now - _ap_bucket_start) // AP_BUCKET_MS
    if gap <= 0:
        return
    # File the minute that just closed, then any fully-empty minutes between.
    _ap_req_minutes.append(_ap_cur_req)
    _ap_cur_req = 0
    _ap_cur_rej = 0
    zeros = min(gap - 1, AP_MAX_MINUTES)
    for _ in range(zeros):
        _ap_req_minutes.append(0)
    if len(_ap_req_minutes) > AP_MAX_MINUTES:
        _ap_req_minutes = _ap_req_minutes[len(_ap_req_minutes) - AP_MAX_MINUTES :]
    _ap_bucket_start += gap * AP_BUCKET_MS


def note_access_outcome(
    status: Any, client_addr: Any = None, route_matched: Optional[bool] = None
) -> None:
    """accessPressure collector — file ONE finished response's OUTCOME class.
    Fed from the ASGI trace middleware's existing ``http.response.start`` hook
    (the same point the kit reads status / cookies), so there is no second
    wrapper.

    Per finished response with a final integer status ``S`` in 100..599 (else
    ignored): ``total++``; class counters (401→unauth, 403→forbidden,
    429→limited, 404→notFound, S>=500→serverError); a rejection is S in
    {401,403,429,404}. Minute buckets roll here (WRITE path only). Running peaks
    include the open bucket. The reach sketch flips ONE anonymous bit per
    rejection with a known client address.

    The address is used ONLY inside this call's stack to flip one sketch bit — it
    is never stored, logged, or placed in any field or caption. Counters saturate
    rather than overflow. Never raises into the host."""
    global _ap_total, _ap_unauth, _ap_forbidden, _ap_limited, _ap_not_found
    global _ap_server_error, _ap_protocol_answers, _ap_unattributed
    global _ap_cur_req, _ap_cur_rej, _ap_peak_req, _ap_peak_rej
    global _ap_sketch
    if not _collecting():
        return
    try:
        if not isinstance(status, int) or isinstance(status, bool):
            return
        if status < 100 or status > 599:
            return
        now = _now_ms()
        _ap_roll_buckets(now)
        _ap_total = _ap_bump(_ap_total)
        _ap_cur_req += 1
        if _ap_cur_req > _ap_peak_req:
            _ap_peak_req = _ap_cur_req
        # Route matching is deliberately three-state. If this host exposes no
        # route handle, preserve the old pressure reading and say so plainly.
        chosen_by_app = route_matched is True
        cannot_tell = route_matched is not True and route_matched is not False
        rejection = False
        if status == 401:
            _ap_unauth = _ap_bump(_ap_unauth)
            rejection = True
        elif status == 403:
            _ap_forbidden = _ap_bump(_ap_forbidden)
            rejection = not chosen_by_app
        elif status == 429:
            _ap_limited = _ap_bump(_ap_limited)
            rejection = True
        elif status == 404:
            _ap_not_found = _ap_bump(_ap_not_found)
            rejection = not chosen_by_app
        elif status >= 500:
            _ap_server_error = _ap_bump(_ap_server_error)
        if status in (403, 404) and chosen_by_app:
            _ap_protocol_answers = _ap_bump(_ap_protocol_answers)
        if rejection and cannot_tell and status in (403, 404):
            _ap_unattributed = _ap_bump(_ap_unattributed)
        if not rejection:
            return
        _ap_cur_rej += 1
        if _ap_cur_rej > _ap_peak_rej:
            _ap_peak_rej = _ap_cur_rej
        if isinstance(client_addr, str) and client_addr != "":
            # sha256(salt + address) → one of 63 bit positions. The digest's
            # first 32-bit big-endian word mod 63 mirrors the leakWatch reach
            # sketch exactly. The address lives only on this stack.
            import hashlib

            digest = hashlib.sha256(
                (_ap_daily_salt() + client_addr).encode("utf-8")
            ).digest()
            pos = int.from_bytes(digest[:4], "big") % 63
            _ap_sketch |= 1 << pos
    except Exception:  # noqa: BLE001
        pass  # instrumentation must never disturb the response


def _ap_median_rate() -> float:
    """Median of the completed minute buckets (zeros included — quiet is real)."""
    ordered = sorted(_ap_req_minutes)
    n = len(ordered)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return float(ordered[(n - 1) // 2])
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def _ap_reach_estimate() -> int:
    """Popcount of the 63-bit reach sketch."""
    return bin(_ap_sketch).count("1")


def _read_access_pressure() -> Optional[Dict[str, Any]]:
    """accessPressure reader, or None while the session has no baseline yet — a
    rating with nothing to compare against would be a guess, and a red tile on a
    healthy busy app is worse than no tile. HONEST ABSENCE until
    ``total >= 50`` AND ``completed minutes >= 10``.

    Wire shape (byte-identical semantics across all four server kits):
      { score, rating, unauth, forbidden, limited, notFound, serverError, total,
        burstPeak60s, surgeRatio, reachEstimate, caption }

    PRIVACY: the caption carries counts, shares and ratios only — never an
    address, route, header or user identity. serverError rides the wire but
    NEVER feeds the rejection share (the app's own errors are covered
    elsewhere)."""
    if _ap_total <= 0:
        return None
    # Buckets roll on the WRITE path (every finished response) — never here, so
    # a read clock can't file phantom quiet minutes into the baseline.
    if _ap_total < AP_MIN_TOTAL_FOR_RATING:
        return None
    if len(_ap_req_minutes) < AP_MIN_BASELINE_MINUTES:
        return None
    rejections = (
        _ap_unauth + _ap_forbidden + _ap_limited + _ap_not_found
        - _ap_protocol_answers
    )
    rej_share = rejections / _ap_total
    median = _ap_median_rate()
    surge_ratio = _ap_r1(min(AP_SURGE_CAP, _ap_peak_req / max(median, 1)))
    # Banding on SHAPE only: share of rejections, and the peak minute against the
    # session's own median — never raw counts. The surge bands only apply once
    # the peak minute carries real volume.
    surging = _ap_peak_req >= AP_SURGE_MIN_PEAK
    if rej_share >= AP_SHARE_POOR or (surging and surge_ratio >= AP_SURGE_POOR):
        rating = "poor"
    elif rej_share >= AP_SHARE_WARN or (surging and surge_ratio >= AP_SURGE_WARN):
        rating = "needs-work"
    else:
        rating = "good"
    surge_penalty = (
        min(1.0, max(0.0, surge_ratio - 1) / (AP_SURGE_POOR - 1)) if surging else 0.0
    )
    score = max(
        0,
        min(
            100,
            _ap_r0(100 - 70 * min(1.0, rej_share / AP_SHARE_POOR) - 30 * surge_penalty),
        ),
    )
    reach = _ap_reach_estimate()
    parts: List[str] = []
    if rejections > 0:
        parts.append(
            f"{rejections} of {_ap_total} responses were refusals this app's own routing did not choose ({_ap_r0(rej_share * 100)}%)"
        )
        if _ap_peak_rej > 0:
            parts.append(f"peak {_ap_peak_rej} rejections/min")
        if reach > 0:
            if reach >= 10:
                parts.append("\u224810+ sources")
            elif reach >= 2:
                parts.append("\u22482\u20139 sources")
            else:
                parts.append("\u22481 source")
    if _ap_protocol_answers > 0:
        parts.append(
            f"{_ap_protocol_answers} more were answers from routes this app serves \u2014 not counted as pressure"
        )
    if _ap_unattributed > 0:
        parts.append(
            f"{_ap_unattributed} could not be attributed to a route on this host and are counted as pressure"
        )
    if surging and surge_ratio >= AP_SURGE_WARN:
        parts.append(f"traffic peaked at {surge_ratio}\u00d7 the quiet baseline")
    caption = (
        f"no rejection pressure across {_ap_total} responses"
        if not parts
        else ", ".join(parts)
    )
    return {
        "score": int(score),
        "rating": rating,
        "unauth": int(_ap_unauth),
        "forbidden": int(_ap_forbidden),
        "limited": int(_ap_limited),
        "notFound": int(_ap_not_found),
        "serverError": int(_ap_server_error),
        "protocolAnswers": int(_ap_protocol_answers),
        "unattributedRefusals": int(_ap_unattributed),
        "breakInRefusals": int(max(0, rejections)),
        "total": int(_ap_total),
        "burstPeak60s": int(_ap_peak_rej),
        "surgeRatio": surge_ratio,
        "reachEstimate": int(reach),
        "caption": caption,
    }


def _reset_access_pressure() -> None:
    """Drop every access-pressure counter (wired into ``clear_extra_meters``)."""
    global _ap_total, _ap_unauth, _ap_forbidden, _ap_limited, _ap_not_found
    global _ap_server_error, _ap_protocol_answers, _ap_unattributed
    global _ap_bucket_start, _ap_cur_req, _ap_cur_rej
    global _ap_req_minutes, _ap_peak_req, _ap_peak_rej, _ap_sketch
    global _ap_salt_day, _ap_salt
    _ap_total = 0
    _ap_unauth = 0
    _ap_forbidden = 0
    _ap_limited = 0
    _ap_not_found = 0
    _ap_server_error = 0
    _ap_protocol_answers = 0
    _ap_unattributed = 0
    _ap_bucket_start = 0
    _ap_cur_req = 0
    _ap_cur_rej = 0
    _ap_req_minutes = []
    _ap_peak_req = 0
    _ap_peak_rej = 0
    _ap_sketch = 0
    _ap_salt_day = ""
    _ap_salt = ""


def note_refusal_honesty(
    status: Any,
    authorization_present: bool,
    challenge_present: bool,
    measurable: bool = True,
) -> None:
    """File one response using presence booleans only; values never enter."""
    global _refusal_started_at, _refusal_refusals, _refusal_challenged
    global _refusal_credentialed, _refusal_misleading, _refusal_measurable
    if not _collecting():
        return
    try:
        now = _now_ms()
        if _refusal_started_at == 0:
            _refusal_started_at = now
        if not measurable:
            _refusal_measurable = 0
            _refusal_refusals = 0
            _refusal_challenged = 0
            _refusal_credentialed = 0
            _refusal_misleading = 0
            return
        if _refusal_measurable == 0 or status not in (401, 403):
            return
        _refusal_refusals = _ap_bump(_refusal_refusals)
        if challenge_present:
            _refusal_challenged = _ap_bump(_refusal_challenged)
        if authorization_present:
            _refusal_credentialed = _ap_bump(_refusal_credentialed)
        if status == 401 and challenge_present and authorization_present:
            _refusal_misleading = _ap_bump(_refusal_misleading)
    except Exception:  # noqa: BLE001
        pass


def _read_refusal_honesty() -> Optional[Dict[str, Any]]:
    if not _collecting() or _refusal_started_at == 0:
        return None
    elapsed = max(0, _now_ms() - _refusal_started_at)
    if elapsed < REFUSAL_MIN_WINDOW_MS:
        return None
    window_min = elapsed // 60_000
    if _refusal_measurable == 0:
        return {
            "score": None,
            # Not the same silence as the branch below: this host cannot take
            # the reading at all, while that one takes it and declines to grade
            # it. Both used to say "pending", so the two were indistinguishable
            # from the rating even inside this one axis. `measurable: 0` still
            # rides along for a server that does not know this label.
            "rating": "not-available",
            "refusals": 0,
            "challenged": 0,
            "credentialed": 0,
            "credentialedChallenged": 0,
            "sharePct": 0,
            "windowMin": int(window_min),
            "measurable": 0,
            "caption": "not measurable \u2014 this host does not expose response headers",
        }
    share_pct = (
        0
        if _refusal_refusals == 0
        else _ap_r0(_refusal_misleading / _refusal_refusals * 100)
    )
    caption = (
        f"no refusals observed in {window_min}m"
        if _refusal_refusals == 0
        else f"{_refusal_misleading} of {_refusal_refusals} refusals challenged a caller who had already sent a credential ({share_pct}%) \u2014 an observation, not a fault"
    )
    return {
        # No score, deliberately. Header PRESENCE cannot tell a wrongly refused
        # caller from a correctly refused one (an expired or mistyped
        # credential SHOULD get a 401 challenge), so this axis reports what it
        # observed and refuses to grade it.
        #
        # "not-scored" says that permanently and by design. "pending" is the
        # word for a score not earned YET, and a careful reader once filed this
        # correct abstention as a defect because that was the only word we had.
        "score": None,
        "rating": "not-scored",
        "refusals": int(_refusal_refusals),
        "challenged": int(_refusal_challenged),
        "credentialed": int(_refusal_credentialed),
        "credentialedChallenged": int(_refusal_misleading),
        "sharePct": int(share_pct),
        "windowMin": int(window_min),
        "measurable": 1,
        "caption": caption,
    }


def _reset_refusal_honesty() -> None:
    global _refusal_started_at, _refusal_refusals, _refusal_challenged
    global _refusal_credentialed, _refusal_misleading, _refusal_measurable
    _refusal_started_at = 0
    _refusal_refusals = 0
    _refusal_challenged = 0
    _refusal_credentialed = 0
    _refusal_misleading = 0
    _refusal_measurable = 1


def _percentile(sorted_vals: List[float], p: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    rank = int(round((p / 100.0) * (n - 1)))
    return sorted_vals[max(0, min(n - 1, rank))]


def _read_gil_contention() -> Optional[Dict[str, Any]]:
    if len(_gil_samples) < GIL_MIN_SAMPLES:
        return None
    ordered = sorted(_gil_samples)
    p95 = _percentile(ordered, 95)
    score = linear_score(p95, GIL_GOOD_MS, GIL_POOR_MS)
    threads = (
        _thread_count_override
        if _thread_count_override is not None
        else threading.active_count()
    )
    return {
        "score": score,
        "rating": rating_for(score),
        "p95Ms": round(p95, 1),
        "threads": int(threads),
        "sampleCount": len(_gil_samples),
        "caption": f"{round(p95, 1)} ms p95 GIL wait \u00b7 {int(threads)} threads",
    }


def _read_task_backlog() -> Optional[Dict[str, Any]]:
    if len(_backlog_samples) < BACKLOG_MIN_SAMPLES:
        return None
    first_ts, first_n = _backlog_samples[0]
    last_ts, last_n = _backlog_samples[-1]
    minutes = (last_ts - first_ts) / 60_000.0
    growth = (last_n - first_n) / minutes if minutes > 0 else 0.0
    score = linear_score(last_n, BACKLOG_GOOD, BACKLOG_POOR)
    return {
        "score": score,
        "rating": rating_for(score),
        "pending": int(last_n),
        "growthPerMin": round(growth, 1),
        "sampleCount": len(_backlog_samples),
        "caption": (
            f"{int(last_n)} pending tasks"
            + (f" \u00b7 +{round(growth, 1)}/min" if growth > 0 else "")
        ),
    }


def _worker_boots_path() -> str:
    """The per-project boot-history file. Keyed by ``app_scope`` so two Python
    services (or two deployments) under one machine account each keep their own
    history instead of polluting a shared one."""
    if _worker_boots_path_override is not None:
        return _worker_boots_path_override
    return str(app_scope.scoped_path(WORKER_BOOTS_STEM))


def _load_worker_boots() -> List[int]:
    """Read the per-project boot history. ANY failure (missing, unreadable,
    corrupt, wrong shape) returns an empty list, which keeps the meter honestly
    ABSENT rather than reporting a restart rate built on a partial read."""
    if _worker_boots_override is not None:
        return list(_worker_boots_override)
    try:
        import json

        with open(_worker_boots_path(), "r") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return [int(t) for t in data if isinstance(t, (int, float))]
        if isinstance(data, dict) and isinstance(data.get("boots"), list):
            return [
                int(t)
                for t in data["boots"]
                if isinstance(t, (int, float))
            ]
    except Exception:  # noqa: BLE001
        pass
    return []


def _load_worker_counts() -> Dict[str, int]:
    if _worker_counts_override is not None:
        return dict(_worker_counts_override)
    try:
        import json

        with open(_worker_boots_path(), "r") as fh:
            data = json.load(fh)
        workers = data.get("workers") if isinstance(data, dict) else None
        if isinstance(workers, dict):
            return {
                str(key): max(0, int(value))
                for key, value in workers.items()
                if isinstance(value, (int, float))
            }
    except Exception:  # noqa: BLE001
        pass
    return {}


def _maybe_record_worker_boot() -> None:
    """Record this worker's boot once the gate allows it. Latched so the retry
    at each request boundary costs one bool after the first success. Never
    raises."""
    global _worker_boot_recorded
    if _worker_boot_recorded or not _collecting():
        return
    _worker_boot_recorded = True
    _record_worker_boot()


def _record_worker_boot() -> None:
    """Append THIS worker process's boot time to the PER-PROJECT on-disk boot
    history (every worker of one deployment shares that file — real cross-worker
    restart evidence, unlike one process's own age — while a different service
    on the same machine keeps its own). Idempotent per process: a boot within 5s
    of an existing entry is treated as already recorded. Best-effort; never
    raises."""
    try:
        if not _is_worker_server():
            return
        age = _read_process_age_ms()
        if age is None or age < 0:
            return
        boot_at = _now_ms() - age
        boots = _load_worker_boots()
        if any(abs(boot_at - b) < 5_000 for b in boots):
            return  # this boot (or a same-moment sibling) is already on record
        boots.append(boot_at)
        boots.sort()
        if len(boots) > WORKER_BOOTS_CAP:
            boots = boots[-WORKER_BOOTS_CAP:]
        if _worker_boots_override is not None:
            _worker_boots_override[:] = boots
            return
        _write_worker_boots_locked(boot_at)
    except Exception:  # noqa: BLE001
        pass


def _write_worker_boots_locked(boot_at: int) -> None:
    """Append one boot to this project's file under an OS lock, re-reading inside
    the critical section (read-modify-write is otherwise lost-update-prone with
    several workers booting at once) and publishing via atomic os.replace so a
    concurrent reader can never observe a torn JSON file. Owner-only perms.
    Best-effort; any failure (no fcntl on this platform, read-only FS, …)
    leaves the meter honestly absent rather than wrong."""
    import json

    path = _worker_boots_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_path = path + ".lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        import fcntl

        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        boots = _load_worker_boots()
        if any(abs(boot_at - b) < 5_000 for b in boots):
            return  # a sibling recorded this same boot while we waited
        boots.append(boot_at)
        boots.sort()
        if len(boots) > WORKER_BOOTS_CAP:
            boots = boots[-WORKER_BOOTS_CAP:]
        tmp = path + f".tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump({"boots": boots, "workers": _load_worker_counts()}, fh)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:  # noqa: BLE001
                pass
            raise
        os.replace(tmp, path)  # atomic publish — readers never see a torn file
    finally:
        os.close(lock_fd)


def _read_worker_restarts() -> Optional[Dict[str, Any]]:
    if _worker_restarts_forgotten:
        return None  # this host cannot recycle workers -> not ours to report
    if not _is_worker_server():
        return None  # not under gunicorn/uWSGI -> honest N/A
    boots = _load_worker_boots()
    if not boots:
        return None
    now = _now_ms()
    history_ms = now - min(boots)
    if history_ms < WORKER_MIN_HISTORY_MS:
        return None  # too little history to tell recycling from a deploy
    window_ms = min(history_ms, WORKER_WINDOW_MS)
    cutoff = now - window_ms
    in_window = sum(1 for b in boots if b >= cutoff)
    # The first boot of the window is the baseline process — only boots BEYOND
    # it are restarts. One boot an hour ago = zero restarts = honest good.
    restarts = max(0, in_window - 1)
    per_hour = restarts / (window_ms / 3_600_000.0)
    score = linear_score(
        per_hour, WORKER_GOOD_RESTARTS_PER_HOUR, WORKER_POOR_RESTARTS_PER_HOUR
    )
    age_ms = _read_process_age_ms()
    out: Dict[str, Any] = {
        "score": score,
        "rating": rating_for(score),
        "restartsPerHour": round(per_hour, 1),
        "windowMin": round(window_ms / 60_000.0, 1),
    }
    if age_ms is not None and age_ms > 0:
        out["uptimeMin"] = round(age_ms / 60_000.0, 1)
    out["caption"] = (
        f"no restarts \u00b7 {round(window_ms / 60_000.0)} min watched"
        if restarts == 0
        else f"~{round(per_hour, 1)} worker restarts/hour"
    )
    return out


def _write_current_worker_count() -> Dict[str, int]:
    """Publish this process's completed count into the boot ledger atomically."""
    if _worker_counts_override is not None:
        return dict(_worker_counts_override)
    if _worker_completed <= 0:
        return _load_worker_counts()
    global _worker_identity
    import json

    path = _worker_boots_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        import fcntl

        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        boots = _load_worker_boots()
        counts = _load_worker_counts()
        if _worker_identity is None:
            boot_at = _now_ms() - (_read_process_age_ms() or 0)
            # The boot ledger itself deduplicates boots inside five seconds;
            # use the same granularity so repeated snapshots keep one worker.
            boot_bucket = int(round(boot_at / 5_000.0) * 5_000)
            _worker_identity = f"{os.getpid()}:{boot_bucket}"
        counts[_worker_identity] = _worker_completed
        tmp = path + f".tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"boots": boots, "workers": counts}, fh)
        os.replace(tmp, path)
        return counts
    finally:
        os.close(lock_fd)


def _read_worker_imbalance() -> Optional[Dict[str, Any]]:
    if _worker_restarts_forgotten or not _is_worker_server():
        return None
    boots = _load_worker_boots()
    if not boots:
        return None
    now = _now_ms()
    history_ms = now - min(boots)
    if history_ms < WORKER_MIN_HISTORY_MS:
        return None
    try:
        counts = _write_current_worker_count()
    except Exception:  # noqa: BLE001
        return None
    if len(counts) < WORKER_IMBALANCE_MIN_WORKERS:
        return None
    total = sum(counts.values())
    if total <= 0:
        return None
    busiest = max(counts.values())
    mean = total / len(counts)
    ratio = busiest / mean
    score = linear_score(ratio, WORKER_IMBALANCE_GOOD, WORKER_IMBALANCE_POOR)
    window_ms = min(history_ms, WORKER_WINDOW_MS)
    return {
        "score": score,
        "rating": rating_for(score),
        "imbalanceRatio": round(ratio * 100) / 100,
        "workerCount": len(counts),
        "busiestShare": round((100.0 * busiest / total) * 10) / 10,
        "windowMin": round(window_ms / 60_000.0, 1),
        "caption": f"{round(ratio * 100) / 100}x busiest worker",
    }


def _read_queue_time() -> Optional[Dict[str, Any]]:
    count = len(_queue_time_samples)
    if count < QUEUE_TIME_MIN_SAMPLES:
        return None
    ordered = sorted(_queue_time_samples)
    index = min(count - 1, int(round(0.75 * (count - 1))))
    p75 = ordered[index]
    worst = ordered[-1]
    score = linear_score(p75, QUEUE_TIME_GOOD_MS, QUEUE_TIME_POOR_MS)
    return {
        "score": score,
        "rating": rating_for(score),
        "p75Ms": round(p75 * 100) / 100,
        "worstMs": round(worst * 100) / 100,
        "sampleCount": count,
        "caption": f"{round(p75 * 100) / 100} ms p75 queue",
    }


def _read_swallowed_errors() -> Optional[Dict[str, Any]]:
    if not _swallowed_armed:
        return None
    now = _now_ms()
    window_ms = min(now - _started_at, SWALLOWED_WINDOW_MS)
    if window_ms < SWALLOWED_MIN_WINDOW_MS:
        return None  # too early for a clean bill to be honest
    cutoff = now - window_ms
    count = sum(1 for t in _swallowed_ts if t >= cutoff)
    hours = window_ms / 3_600_000.0
    per_hour = count / hours if hours > 0 else 0.0
    score = linear_score(per_hour, SWALLOWED_GOOD_PER_HOUR, SWALLOWED_POOR_PER_HOUR)
    return {
        "score": score,
        "rating": rating_for(score),
        "count": int(count),
        "perHour": round(per_hour, 1),
        "windowMin": round(window_ms / 60_000.0, 1),
        "caption": (
            f"no logged errors \u00b7 {round(window_ms / 60_000.0)} min watched"
            if count == 0
            else f"{int(count)} error{'s' if count != 1 else ''} logged, app survived"
        ),
    }

def _leak_shape_phrase(shape_mask: int) -> str:
    words = [w for bit, w in _LEAK_SHAPE_WORDS if shape_mask & bit]
    return "matched " + ", ".join(words) if words else "shape not recorded"
def _read_leak_watch() -> Optional[Dict[str, Any]]:
    """leakWatch reader — mirrors ``_read_swallowed_errors`` gates exactly:
    armed via the same chained record factory, 5-min minimum observation window
    before reporting, 60-min trailing window. OMITTED (absent key) while warming
    so an early clean bill is never claimed.

    ADDITIVE / display-only: never feeds the Speed score. score is 100 when no
    leak was observed, otherwise max(0, 70 - round(perHour*10)) — any observed
    leak is at best "needs-work". routeClassCount and fromReplyCount are
    intentionally ABSENT: the Python mount serves only the read-only dashboard,
    so there is no customer response path to classify or attribute to, and a
    zero there would read as "we looked and found none".

    PRIVACY: the output is counts, a rate, a window, two closed-vocabulary
    integers (fromLogCount, shapeMask) and two string fields (rating, caption)
    built from counts and code-defined WORDS only — never any scanned
    content."""
    if not _swallowed_armed:
        return None  # same chained factory arms leakWatch and swallowedErrors
    now = _now_ms()
    window_ms = min(now - _started_at, LEAK_WINDOW_MS)
    if window_ms < LEAK_MIN_WINDOW_MS:
        return None  # too early — an absence-of-evidence claim must be earned
    cutoff = now - window_ms
    stack_count = 0
    secret_count = 0
    pii_count = 0
    from_log_count = 0
    shape_mask = 0
    for ts, category, surface, shape in _leak_events:
        if ts < cutoff:
            continue
        if category == "secret":
            secret_count += 1
        elif category == "pii":
            pii_count += 1
        elif category == "stack":
            stack_count += 1
        if surface == LEAK_SURFACE_LOG:
            from_log_count += 1
        shape_mask |= shape
    count = stack_count + secret_count + pii_count
    hours = window_ms / 3_600_000.0
    per_hour = count / hours if hours > 0 else 0.0
    score = 100 if count == 0 else max(0, 70 - round(per_hour * 10))
    if count == 0:
        # ABSENCE OF EVIDENCE — never "you are protected".
        caption = "no leaks observed in this window"
    else:
        parts: List[str] = []
        if secret_count:
            parts.append(f"{secret_count} secret-like")
        if pii_count:
            parts.append(f"{pii_count} personal-detail")
        if stack_count:
            parts.append(f"{stack_count} stack trace")
        detail = ", ".join(parts)
        caption = (
            f"{count} possible leak{'s' if count != 1 else ''} observed"
            + (f" ({detail})" if detail else "")
            + " \u00b7 all in your app's log output"
            + f" \u00b7 {_leak_shape_phrase(shape_mask)}"
        )
    return {
        "score": int(score),
        "rating": rating_for(score),
        "count": int(count),
        "perHour": round(per_hour, 1),
        "windowMin": round(window_ms / 60_000.0, 1),
        "stackCount": int(stack_count),
        "secretCount": int(secret_count),
        "piiCount": int(pii_count),
        "fromLogCount": int(from_log_count),
        "shapeMask": int(shape_mask),
        "caption": caption,
    }


def _read_cookie_exposure() -> Optional[Dict[str, Any]]:
    """cookieExposure reader — reports the safe-flag EXPOSURE observed on the
    responses this process watched set cookies, never safety.

    HONEST ABSENCE: when no cookie has been observed being set (``setCount == 0``)
    this returns None and the axis is simply NOT emitted — an app that sets no
    cookies must never show a green tile, and cookieExposure is never added to
    any EXPECTED-axes / warming set.

    SCORE + RATING (byte-for-byte identical across all four server kits):
      unsafeShare = (noSecure + noneWithoutSecure) / setCount
      weakShare   = (noHttpOnly + noSameSite + oversize) / (setCount * 3)
      score = clamp(round(100 - 70*unsafeShare - 30*weakShare), 0, 100)
      rating = "poor" if noSecure > 0 or noneWithoutSecure > 0
             else "needs-work" if noHttpOnly > 0 or noSameSite > 0 or oversize > 0
             else "good"

    No caption on the wire — the server rebuilds it. Numeric only besides rating.
    PRIVACY: output is COUNTS + rating only; no cookie name/value can reach it."""
    set_count = _cookie_set_count
    if set_count <= 0:
        return None  # HONEST ABSENCE — never a green tile for a cookie-less app
    no_secure = _cookie_no_secure
    no_http_only = _cookie_no_http_only
    no_same_site = _cookie_no_same_site
    none_without_secure = _cookie_none_without_secure
    oversize = _cookie_oversize
    unsafe_share = (no_secure + none_without_secure) / set_count
    weak_share = (no_http_only + no_same_site + oversize) / (set_count * 3)
    score = max(0, min(100, round(100 - 70 * unsafe_share - 30 * weak_share)))
    if no_secure > 0 or none_without_secure > 0:
        rating = "poor"
    elif no_http_only > 0 or no_same_site > 0 or oversize > 0:
        rating = "needs-work"
    else:
        rating = "good"
    return {
        "score": int(score),
        "rating": rating,
        "setCount": int(set_count),
        "responses": int(_cookie_responses),
        "noSecure": int(no_secure),
        "noHttpOnly": int(no_http_only),
        "noSameSite": int(no_same_site),
        "noneWithoutSecure": int(none_without_secure),
        "oversize": int(oversize),
    }

def _probe_dev_debug_flag() -> Optional[int]:
    """The devPosture ``debugFlag`` check: is this app running with a debug /
    non-production build flag on? Reads, in order, whatever is inspectable —
    NEVER triggering Django settings configuration, NEVER an env VALUE into any
    field. Returns 1 (on), 0 (read and off), or None (nothing inspectable —
    which cannot happen here, env vars are always inspectable so this always
    returns 0 or 1).

    Sources (any truthy → 1):
      a. ``django.conf.settings.DEBUG`` is True, but ONLY when django is already
         imported AND its settings are configured — every step guarded so we
         never import django or trigger settings configuration ourselves.
      b. A mounted ASGI/WSGI app object the kit holds (if any) with a truthy
         ``.debug`` attribute (Flask/Starlette/FastAPI).
      c. env ``FLASK_DEBUG`` / ``DJANGO_DEBUG`` in the truthy set.
    """
    found_on = False
    inspectable = False
    # a. Django settings.DEBUG — only if django is ALREADY in sys.modules and its
    #    settings are configured; never configure them ourselves.
    try:
        django_mod = sys.modules.get("django")
        if django_mod is not None:
            conf = sys.modules.get("django.conf")
            if conf is not None:
                settings = getattr(conf, "settings", None)
                if settings is not None and getattr(settings, "configured", False):
                    inspectable = True
                    if bool(getattr(settings, "DEBUG", False)):
                        found_on = True
    except Exception:  # noqa: BLE001
        pass
    # b. A mounted app object with a truthy .debug attribute, if the kit holds
    #    one (Flask/Starlette/FastAPI). Guarded — many apps hold none.
    try:
        app_obj = _mounted_app
        if app_obj is not None and hasattr(app_obj, "debug"):
            inspectable = True
            if bool(getattr(app_obj, "debug", False)):
                found_on = True
    except Exception:  # noqa: BLE001
        pass
    # c. env FLASK_DEBUG / DJANGO_DEBUG — always inspectable, so debugFlag is
    #    always present.
    try:
        for key in ("FLASK_DEBUG", "DJANGO_DEBUG"):
            raw = os.environ.get(key)
            if raw is None:
                continue
            inspectable = True
            if str(raw).strip() in _DEV_DEBUG_TRUTHY:
                found_on = True
    except Exception:  # noqa: BLE001
        pass
    # env is always readable → this is always present; but treat a totally
    # uninspectable world as honest absence.
    if found_on:
        return 1
    if inspectable:
        return 0
    return 0  # env vars are always inspectable — this branch is unreachable


def _read_thread_pool_starvation() -> Optional[Dict[str, Any]]:
    if len(_pool_waits) < POOL_MIN_SAMPLES:
        return None
    ordered = sorted(_pool_waits)
    p95 = _percentile(ordered, 95)
    score = linear_score(p95, POOL_GOOD_MS, POOL_POOR_MS)
    return {
        "score": score,
        "rating": rating_for(score),
        "p95Ms": round(p95),
        "worstMs": round(ordered[-1]),
        "sampleCount": len(_pool_waits),
        "caption": f"{round(p95)} ms p95 queue wait \u00b7 worst {round(ordered[-1])} ms",
    }


def _read_blocking_async() -> Optional[Dict[str, Any]]:
    # Only meaningful for async apps: gate on the loop-lag probe having run
    # (backlog samples prove a running loop) OR block events being recorded.
    if len(_block_ts) == 0 and len(_backlog_samples) == 0:
        return None
    now = _now_ms()
    window_ms = min(now - _started_at, BLOCK_WINDOW_MS)
    if window_ms < BLOCK_MIN_WINDOW_MS:
        return None
    cutoff = now - window_ms
    count = sum(1 for t in _block_ts if t >= cutoff)
    minutes = window_ms / 60_000.0
    per_min = count / minutes if minutes > 0 else 0.0
    score = linear_score(per_min, BLOCK_GOOD_PER_MIN, BLOCK_POOR_PER_MIN)
    return {
        "score": score,
        "rating": rating_for(score),
        "count": int(count),
        "perMin": round(per_min, 2),
        "worstMs": round(_block_worst_ms),
        "caption": (
            "no sync stalls on the event loop"
            if count == 0
            else f"{int(count)} loop stall{'s' if count != 1 else ''} \u00b7 worst {round(_block_worst_ms)} ms"
        ),
    }


def _read_fork_churn() -> Optional[Dict[str, Any]]:
    now = _now_ms()
    if now - _started_at < FORK_MIN_ENABLED_MS:
        return None
    window_ms = min(now - _started_at, FORK_WINDOW_MS)
    cutoff = now - window_ms
    count = sum(1 for t in _fork_ts if t >= cutoff)
    minutes = window_ms / 60_000.0
    per_min = count / minutes if minutes > 0 else 0.0
    score = linear_score(per_min, FORK_GOOD_PER_MIN, FORK_POOR_PER_MIN)
    return {
        "score": score,
        "rating": rating_for(score),
        "spawnsPerMin": round(per_min, 1),
        "total": int(_fork_total),
        "windowMin": round(window_ms / 60_000.0, 1),
        "caption": (
            "no subprocesses spawned"
            if count == 0
            else f"{round(per_min, 1)} subprocess spawns/min"
        ),
    }


def _deflect_p75(durations: List[float]) -> int:
    """Nearest-rank p75 on an ASCENDING sort of ``durations``, IDENTICAL across
    the Node/Go/Java/Python loadDeflection kits so a given set of durations maps
    to the same lowMs/highMs everywhere:

        index = ceil(0.75 * n) - 1, clamped to [0, n-1]     (no interpolation)

    ``ceil(0.75*n)`` is computed with integer arithmetic — ``ceil(3n/4)`` is
    ``(3n + 3) // 4`` — to avoid any floating-point rounding drift between
    runtimes. Returns an int ms (each recorded duration is already ms)."""
    n = len(durations)
    if n == 0:
        return 0
    ordered = sorted(durations)
    idx = (3 * n + 3) // 4 - 1
    if idx < 0:
        idx = 0
    elif idx > n - 1:
        idx = n - 1
    return int(round(ordered[idx]))


def _deflect_poor_latency_budget_ms() -> float:
    """The runtime's EXISTING poor latency budget — the same TTI ``poor`` band
    every per-route duration is already rated against — reused so
    ``strainAtInFlight`` means the same thing the rest of the kit means by
    "poor". Falls back to a fixed 1500 ms only if the threshold table cannot be
    read, so the reader never raises."""
    try:
        from .thresholds import SCORE_THRESHOLDS

        return float(SCORE_THRESHOLDS["tti"]["poor"])
    except Exception:  # noqa: BLE001
        return 1500.0


def _read_load_deflection() -> Optional[Dict[str, Any]]:
    """Deflection ratio + factor-of-safety. OMITTED (honest N/A) until enough
    overlapping traffic exists to measure bending under load — a single-threaded
    trickle can never deflect, so reporting one would be a fabricated number.

    HONEST CLAIM: the ratio is an OBSERVED correlation between concurrency and
    latency (busy moments vs quiet moments in the same app), NOT proof that load
    CAUSED the slowdown — the routes in each bucket are not matched. The server
    renders this as measured-busy-vs-quiet and never as causation.

    Gate (ALL must hold): >= DEFLECT_MIN_SAMPLES completed, >= 5 samples in each
    of the two buckets compared, and a peak in-flight of at least 2."""
    if _deflect_total < DEFLECT_MIN_SAMPLES:
        return None
    if _deflect_peak_in_flight < DEFLECT_MIN_PEAK_IN_FLIGHT:
        return None  # no overlap ever seen — nothing could have deflected

    # Buckets with enough samples to have an honest p75, low → high.
    usable = [
        i
        for i in range(len(DEFLECT_BUCKET_FLOORS))
        if len(_deflect_buckets[i]) >= DEFLECT_MIN_BUCKET_SAMPLES
    ]
    if len(usable) < 2:
        return None  # need a lowest AND a highest loaded bucket to compare
    low_i = usable[0]
    high_i = usable[-1]
    low_ms = _deflect_p75(_deflect_buckets[low_i])
    high_ms = _deflect_p75(_deflect_buckets[high_i])
    if low_ms <= 0:
        return None  # cannot form an honest ratio against a zero floor
    ratio = high_ms / low_ms
    score = linear_score(ratio, DEFLECT_SCORE_GOOD, DEFLECT_SCORE_POOR)
    # Rating comes from the RATIO bands directly (not the score): the "bending"
    # thresholds are what the developer reasons about.
    if ratio <= DEFLECT_RATIO_GOOD:
        rating = "good"
    elif ratio >= DEFLECT_RATIO_POOR:
        rating = "poor"
    else:
        rating = "needs-work"

    # strainAtInFlight — the smallest bucket floor whose p75 first exceeded the
    # runtime's existing poor latency budget, or None when none did.
    budget = _deflect_poor_latency_budget_ms()
    strain_at: Optional[int] = None
    for i in range(len(DEFLECT_BUCKET_FLOORS)):
        # Only a bucket we actually observed with >= 5 samples can strain —
        # never invent one from a bucket we did not see.
        if len(_deflect_buckets[i]) < DEFLECT_MIN_BUCKET_SAMPLES:
            continue
        if _deflect_p75(_deflect_buckets[i]) > budget:
            strain_at = int(DEFLECT_BUCKET_FLOORS[i])
            break

    mean_in_flight = (
        _deflect_in_flight_sum / _deflect_total if _deflect_total > 0 else 0.0
    )

    # soloMeanMs (merged in from requestConcurrency): integer mean of the CLAMPED
    # solo durations, or None (wire null) when nothing ran alone. Key always
    # present. Same built-in round the retired requestConcurrency axis used
    # (parity-audited against Node's Math.round).
    solo_mean: Optional[int] = (
        round(_deflect_solo_total_ms / _deflect_solo_count)
        if _deflect_solo_count > 0
        else None
    )

    # deflectionMsPerReq (merged in from requestConcurrency): least-squares slope
    # of clamped duration on start-in-flight over ALL recorded samples,
    # round(max(0, slope)). Null unless there is enough overlapping load to trust
    # a slope (>= 10 loaded samples AND at least one solo sample) and the
    # least-squares denominator is positive.
    deflection_ms_per_req: Optional[int] = None
    if (
        _deflect_loaded_count >= DEFLECT_MIN_LOADED_FOR_SLOPE
        and _deflect_solo_count > 0
    ):
        denom = _deflect_ls_n * _deflect_ls_sum_x2 - _deflect_ls_sum_x ** 2
        if denom > 0:
            slope = (
                _deflect_ls_n * _deflect_ls_sum_xy
                - _deflect_ls_sum_x * _deflect_ls_sum_y
            ) / denom
            deflection_ms_per_req = round(max(0.0, slope))

    axis: Dict[str, Any] = {
        "score": score,
        "rating": rating,
        "peakInFlight": int(_deflect_peak_in_flight),
        "meanInFlight": round(mean_in_flight, 1),
        "lowMs": low_ms,
        "highMs": high_ms,
        "ratio": round(ratio, 2),
        "strainAtInFlight": strain_at,
        "samples": int(_deflect_total),
        "soloMeanMs": solo_mean,
        "deflectionMsPerReq": deflection_ms_per_req,
    }
    try:
        from boosthis.held_open import get_held_open_stats

        held = get_held_open_stats()
        if held["heldOpenExcluded"] > 0:
            axis.update(held)
    except Exception:  # noqa: BLE001
        pass
    return axis


def read_extra_meters() -> Dict[str, Dict[str, Any]]:
    """Assemble every currently-available batch axis. Warming/N-A axes are
    OMITTED (the server renders an absent expected axis as pending). Pure
    read; never raises."""
    out: Dict[str, Dict[str, Any]] = {}
    if not _collecting():
        return out
    for key, reader in (
        ("gilContention", _read_gil_contention),
        ("taskBacklog", _read_task_backlog),
        ("workerRestarts", _read_worker_restarts),
        ("workerImbalance", _read_worker_imbalance),
        ("queueTime", _read_queue_time),
        ("swallowedErrors", _read_swallowed_errors),
        ("leakWatch", _read_leak_watch),
        ("cookieExposure", _read_cookie_exposure),
        ("devPosture", _read_dev_posture),
        ("accessPressure", _read_access_pressure),
        ("refusalHonesty", _read_refusal_honesty),
        ("threadPoolStarvation", _read_thread_pool_starvation),
        ("blockingAsync", _read_blocking_async),
        ("forkChurn", _read_fork_churn),
        ("loadDeflection", _read_load_deflection),
    ):
        try:
            axis = reader()
        except Exception:  # noqa: BLE001
            axis = None
        if axis:
            out[key] = axis
    return out


def _reset_task_backlog() -> None:
    """Drop the pending-task samples."""
    global _last_backlog_probe_at
    _backlog_samples.clear()
    _last_backlog_probe_at = 0


def _reset_blocking_async() -> None:
    """Drop the recorded event-loop block events."""
    global _block_worst_ms
    _block_ts.clear()
    _block_worst_ms = 0.0


def _reset_cookie_exposure() -> None:
    """Drop every cookie-exposure counter."""
    global _cookie_set_count, _cookie_responses, _cookie_no_secure
    global _cookie_no_http_only, _cookie_no_same_site
    global _cookie_none_without_secure, _cookie_oversize
    _cookie_set_count = 0
    _cookie_responses = 0
    _cookie_no_secure = 0
    _cookie_no_http_only = 0
    _cookie_no_same_site = 0
    _cookie_none_without_secure = 0
    _cookie_oversize = 0


def _reset_worker_restarts() -> None:
    """Stop answering about worker recycling in this process.

    Nothing here is a counter to zero: the boot history is a per-PROJECT file
    every worker of a deployment appends to. A host that serves the app from the
    single process it started itself can never recycle a worker, so a number
    read out of that shared file would not be its reading — abstaining is the
    drop."""
    global _worker_restarts_forgotten, _worker_completed, _worker_identity
    _worker_restarts_forgotten = True


#: Axis key -> how to forget everything that axis has accumulated so far.
#:
#: Used when a DIFFERENT kind of host takes over the process (see
#: ``host_surface.forget_foreign_reading``): a value gathered under another host
#: is not this host's reading, and leaving it in place would let a project show
#: a reading its own host can never produce — the exact tile the "not
#: measurable" naming exists to replace. Only axes that accumulate state need an
#: entry; a reader that computes live from the host has nothing to forget.
AXIS_STATE_RESETS: Dict[str, Any] = {
    "accessPressure": _reset_access_pressure,
    "blockingAsync": _reset_blocking_async,
    "cookieExposure": _reset_cookie_exposure,
    "refusalHonesty": _reset_refusal_honesty,
    "taskBacklog": _reset_task_backlog,
    "workerRestarts": _reset_worker_restarts,
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


def clear_extra_meters() -> None:
    """Detach every hook we can and wipe all state (wired into ``forget()``).
    The at-fork hook cannot be unregistered; it goes inert via ``_started``.
    Idempotent. Never raises."""
    global _started, _started_at, _orig_pool_submit
    global _orig_popen_init, _last_gil_probe_at, _last_backlog_probe_at
    global _block_worst_ms, _fork_total, _worker_boot_recorded, _gate_unsub
    global _worker_restarts_forgotten
    global _deflect_total, _deflect_in_flight_sum, _deflect_peak_in_flight
    global _deflect_solo_count, _deflect_solo_total_ms, _deflect_loaded_count
    global _deflect_ls_n, _deflect_ls_sum_x, _deflect_ls_sum_y
    global _deflect_ls_sum_xy, _deflect_ls_sum_x2
    global _cookie_set_count, _cookie_responses, _cookie_no_secure
    global _cookie_no_http_only, _cookie_no_same_site
    global _cookie_none_without_secure, _cookie_oversize
    global _dev_posture_frozen, _mounted_app
    _started = False
    _started_at = 0
    _restore_record_factory()
    try:
        if _gate_unsub is not None:
            _gate_unsub()
    except Exception:  # noqa: BLE001
        pass
    _gate_unsub = None
    try:
        if _orig_pool_submit is not None:
            from concurrent.futures import ThreadPoolExecutor

            ThreadPoolExecutor.submit = _orig_pool_submit  # type: ignore[method-assign]
    except Exception:  # noqa: BLE001
        pass
    _orig_pool_submit = None
    try:
        if _orig_popen_init is not None:
            subprocess.Popen.__init__ = _orig_popen_init  # type: ignore[method-assign]
    except Exception:  # noqa: BLE001
        pass
    _orig_popen_init = None
    _gil_samples.clear()
    _backlog_samples.clear()
    _swallowed_ts.clear()
    _leak_events.clear()
    _pool_waits.clear()
    _block_ts.clear()
    _fork_ts.clear()
    _queue_time_samples.clear()
    _last_gil_probe_at = 0
    _last_backlog_probe_at = 0
    _block_worst_ms = 0.0
    _fork_total = 0
    _worker_boot_recorded = False
    _worker_restarts_forgotten = False
    _worker_completed = 0
    _worker_identity = None
    for _ring in _deflect_buckets:
        _ring.clear()
    _deflect_total = 0
    _deflect_in_flight_sum = 0
    _deflect_peak_in_flight = 0
    _deflect_solo_count = 0
    _deflect_solo_total_ms = 0.0
    _deflect_loaded_count = 0
    _deflect_ls_n = 0
    _deflect_ls_sum_x = 0.0
    _deflect_ls_sum_y = 0.0
    _deflect_ls_sum_xy = 0.0
    _deflect_ls_sum_x2 = 0.0
    _cookie_set_count = 0
    _cookie_responses = 0
    _cookie_no_secure = 0
    _cookie_no_http_only = 0
    _cookie_no_same_site = 0
    _cookie_none_without_secure = 0
    _cookie_oversize = 0
    # devPosture — drop the frozen one-shot result and the mounted-app reference
    # so the next start re-reads the checks fresh.
    _dev_posture_frozen = None
    _mounted_app = None
    _reset_access_pressure()
    _reset_refusal_honesty()


def erase_extra_meter_files() -> None:
    """Delete every file these meters wrote to disk: the worker-boot history,
    its lock, and any temp file left by an interrupted atomic publish. Wired
    into ``telemetry.forget()`` so erasure leaves nothing behind.
    Best-effort; never raises."""
    targets: List[str] = []

    def _add(path: str) -> None:
        if path and path not in targets:
            targets.append(path)

    # 1. This project's file (or the test override) and the pre-1.0.0a37
    #    machine-wide one, each with its lock.
    for resolve in (
        _worker_boots_path,
        lambda: os.path.expanduser(LEGACY_WORKER_BOOTS_FILE),
    ):
        try:
            base = resolve()
        except Exception:  # noqa: BLE001
            continue
        _add(base)
        _add(base + ".lock")
    # 2. EVERY other project scope under the state dir. Erasure is per-install —
    #    the install id, tokens and config are shared by everything under one
    #    state dir — and ``forget()`` usually runs from a different process and
    #    directory than the app that wrote the history (the CLI, the dashboard),
    #    which would resolve a different scope. Nothing may survive that.
    try:
        state_dir = str(app_scope.STATE_DIR)
        for name in os.listdir(state_dir):
            if name.startswith(WORKER_BOOTS_STEM):
                _add(os.path.join(state_dir, name))
    except Exception:  # noqa: BLE001
        pass
    # 3. ``.tmp.<pid>`` siblings from an interrupted atomic publish.
    for base in list(targets):
        try:
            directory = os.path.dirname(base)
            prefix = os.path.basename(base) + ".tmp."
            for name in os.listdir(directory):
                if name.startswith(prefix):
                    _add(os.path.join(directory, name))
        except Exception:  # noqa: BLE001
            pass
    for path in targets:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            pass
    # The config (and with it the install id) is gone after erasure, so the next
    # enable must resolve a fresh scope rather than reuse the cached one.
    try:
        app_scope.reset_app_scope()
    except Exception:  # noqa: BLE001
        pass


# ── Test hooks (mirroring runtime_vitals' private-override style) ───────────
def _force_start_for_test(started_at: Optional[int] = None) -> None:
    global _started, _started_at
    _started = True
    _started_at = started_at if started_at is not None else _now_ms()


def _set_now_for_test(ts: Optional[int]) -> None:
    global _now_override
    _now_override = ts


def _set_process_age_for_test(age_ms: Optional[int]) -> None:
    global _process_age_override
    _process_age_override = age_ms


def _set_worker_env_for_test(is_worker: Optional[bool]) -> None:
    global _worker_env_override
    _worker_env_override = is_worker


def _set_worker_boots_for_test(boots: Optional[List[int]]) -> None:
    global _worker_boots_override
    _worker_boots_override = boots


def _set_worker_boots_path_for_test(path: Optional[str]) -> None:
    global _worker_boots_path_override
    _worker_boots_path_override = path


def _set_worker_counts_for_test(counts: Optional[Dict[str, int]]) -> None:
    global _worker_counts_override
    _worker_counts_override = counts


def _push_queue_time_for_test(ms: float) -> None:
    _queue_time_samples.append(ms)


def _set_gil_enabled_for_test(enabled: Optional[bool]) -> None:
    global _gil_enabled_override
    _gil_enabled_override = enabled


def _set_thread_count_for_test(count: Optional[int]) -> None:
    global _thread_count_override
    _thread_count_override = count


def _push_gil_sample_for_test(ms: float) -> None:
    _gil_samples.append(ms)


def _push_backlog_sample_for_test(ts: int, pending: int) -> None:
    _backlog_samples.append((ts, pending))


def _add_swallowed_error_for_test(ts: int) -> None:
    _swallowed_ts.append(ts)


def _set_swallowed_armed_for_test(armed: bool) -> None:
    """Pretend the record factory is (not) installed, for reader-gate tests."""
    global _swallowed_armed
    _swallowed_armed = armed


def _add_leak_event_for_test(
    ts: int, category: str, shape: int = LEAK_SHAPE_STACK_FRAME
) -> None:
    """File one leakWatch observation directly into the ring, bypassing the log
    hook. ``category`` must be one of "secret" / "pii" / "stack"; ``shape`` is a
    closed ``LEAK_SHAPE_*`` code."""
    _leak_events.append((ts, category, LEAK_SURFACE_LOG, shape))


def _reset_leak_events_for_test() -> None:
    _leak_events.clear()


def _note_cookie_set_for_test(set_cookie_values: Any) -> None:
    """File one response's cookieExposure observation the way a real response
    would — driving the SAME ``note_cookie_headers`` parser the trace middleware
    fires — from a list of raw Set-Cookie values. Each item is encoded into the
    ``(b"set-cookie", value_bytes)`` ASGI header tuple form so reader/parity
    tests exercise the exact production path without spinning a real server.

    A ``str`` item is UTF-8-encoded (matching the real wire, so a multi-byte
    value's byte length is preserved). A ``bytes`` item is passed THROUGH
    verbatim, so a test can feed invalid UTF-8 / raw wire bytes directly."""
    headers: List[Tuple[bytes, bytes]] = []
    for value in set_cookie_values or []:
        if isinstance(value, (bytes, bytearray)):
            headers.append((b"set-cookie", bytes(value)))
        else:
            text = value if isinstance(value, str) else str(value)
            headers.append((b"set-cookie", text.encode("utf-8", "replace")))
    note_cookie_headers(headers)


def _reset_cookie_exposure_for_test() -> None:
    _reset_cookie_exposure()

def _reset_dev_posture_for_test() -> None:
    """Drop the frozen devPosture result AND any override so the next read
    re-collects the checks fresh — the reset seam for the freeze-at-init
    contract. Mirrors the JS kit's ``_resetDevPostureForTests``."""
    global _dev_posture_frozen, _dev_posture_override, _mounted_app
    _dev_posture_frozen = None
    _dev_posture_override = None
    _mounted_app = None

def _roll_access_buckets_for_test(now: int) -> None:
    """Test seam — deterministic bucket rollover without waiting a minute."""
    _ap_roll_buckets(now)


def _set_access_bucket_start_for_test(ts: int) -> None:
    """Test seam — set the bucket clock origin."""
    global _ap_bucket_start
    _ap_bucket_start = ts


def _reset_access_pressure_for_test() -> None:
    """Test seam — drop all access-pressure state."""
    _reset_access_pressure()


def _reset_refusal_honesty_for_test() -> None:
    _reset_refusal_honesty()


def _push_pool_wait_for_test(ms: float) -> None:
    _pool_waits.append(ms)


def _add_block_event_for_test(ts: int, lag_ms: float) -> None:
    global _block_worst_ms
    _block_ts.append(ts)
    if lag_ms > _block_worst_ms:
        _block_worst_ms = lag_ms


def _add_fork_for_test(ts: int) -> None:
    global _fork_total
    _fork_total += 1
    _fork_ts.append(ts)


def _push_deflection_sample_for_test(in_flight: int, duration_ms: float) -> None:
    """Directly file a loadDeflection sample, bypassing the request-path hook,
    so reader/gate tests do not have to spin real concurrent traffic."""
    note_deflection_sample(in_flight, duration_ms)


def add_deflection_sample_for_test(
    start_in_flight: int, duration_ms: float
) -> None:
    """Seed ONE loadDeflection sample the way a real request would — driving the
    shared in-flight counter through the DEFLECTION request boundary — so a
    cross-runtime parity replay exercises the exact production path.

    Equivalent to the retired ``addConcurrencySampleForTests`` but on the
    deflection path: bump the shared in-flight counter up to ``start_in_flight``
    via repeated ``note_request_start()`` calls (each is the same start hook the
    trace middleware fires), record the completed sample once with
    ``note_deflection_sample(start_in_flight, duration_ms)``, then unwind the
    residual in-flight counter back to 0 with the paired ``note_request_end()``
    so no in-flight leaks across cases. ``start_in_flight`` is clamped to at
    least 1 (a recorded request always counts itself)."""
    from .live_detectors import note_request_end, note_request_start

    n = int(start_in_flight)
    if n < 1:
        n = 1
    for _ in range(n):
        note_request_start()
    note_deflection_sample(n, duration_ms)
    for _ in range(n):
        note_request_end()

def _read_dev_posture() -> Optional[Dict[str, Any]]:
    """devPosture reader — reports the development settings we can READ; a clean
    tile means none of them were on, NOT that the deployment is hardened.

    EXPOSURE, never safety. ADDITIVE / display-only: never feeds the Speed
    score. The four flags (debugFlag / verboseErrors / sourceMaps /
    profilingOpen) are present ONLY when this runtime actually read that check
    this session (Python reads debugFlag + profilingOpen); an unreadable check's
    flag is OMITTED. ``checks`` counts the flags present; ``findings`` counts the
    present flags equal to 1.

    rating: debugFlag present and == 1 → "poor"; else findings > 0 →
    "needs-work"; else "good".
    score: start 100; -60 if debugFlag == 1; -20 per OTHER finding
    (verboseErrors/sourceMaps/profilingOpen == 1); clamp 0..100.
    caption: counts + setting NAMES only, never an env value / path / URL /
    config string.

    If ZERO checks were readable the axis is OMITTED (honest absence, like
    cookieExposure with no cookies) — but Python always reads at least
    debugFlag + profilingOpen, so it emits in practice."""
    flags = _dev_posture_flags()
    checks = len(flags)
    if checks == 0:
        return None  # honest absence — nothing was readable this session
    debug_on = flags.get("debugFlag") == 1
    # Findings = present flags equal to 1.
    findings = sum(1 for v in flags.values() if v == 1)
    # Score: -60 for debug, -20 for each OTHER finding.
    score = 100
    if debug_on:
        score -= 60
    for key in ("verboseErrors", "sourceMaps", "profilingOpen"):
        if flags.get(key) == 1:
            score -= 20
    score = max(0, min(100, score))
    if debug_on:
        rating = "poor"
    elif findings > 0:
        rating = "needs-work"
    else:
        rating = "good"
    # Caption — counts + setting NAMES only. good → the exact "none of the {N}"
    # phrasing; otherwise list only the ON findings by their fixed phrases.
    if findings == 0:
        caption = (
            f"none of the {checks} development settings we can read were on"
        )
    else:
        parts: List[str] = []
        if flags.get("debugFlag") == 1:
            parts.append("debug mode on")
        if flags.get("verboseErrors") == 1:
            parts.append("verbose error pages on")
        if flags.get("sourceMaps") == 1:
            parts.append("source maps served")
        if flags.get("profilingOpen") == 1:
            parts.append("profiling port open")
        caption = ", ".join(parts)
    out: Dict[str, Any] = {
        "score": int(score),
        "rating": rating,
        "caption": caption,
        "findings": int(findings),
        "checks": int(checks),
        "measurable": 1,
    }
    # Each flag is present ONLY when it was actually read this session.
    for key in ("debugFlag", "verboseErrors", "sourceMaps", "profilingOpen"):
        if key in flags:
            out[key] = int(flags[key])
    return out

def _set_dev_posture_for_test(flags: Optional[Dict[str, int]]) -> None:
    """Override the frozen devPosture flag dict with an explicit set of readable
    flags (each value 1/0). ``None`` clears the override. An UNREADABLE check is
    simply LEFT OUT of ``flags`` (honest absence). Mirrors the JS kit's
    ``readDevPostureForTests`` override seam."""
    global _dev_posture_override
    _dev_posture_override = dict(flags) if flags is not None else None

def set_mounted_app(app: Any) -> None:
    """Record the ASGI/WSGI app object ``mount()`` was given so the devPosture
    debugFlag probe can read its ``.debug`` attribute (Flask/Starlette/FastAPI).
    Read-only from the probe's side; the object is never mutated. Called from
    ``boosthis.mount()``. Best-effort — never raises into the host."""
    global _mounted_app
    try:
        _mounted_app = app
    except Exception:  # noqa: BLE001
        pass

def _set_mounted_app_for_test(app: Any) -> None:
    """Point the devPosture debugFlag probe at a fake app object exposing a
    ``.debug`` attribute, without spinning a real framework."""
    global _mounted_app, _dev_posture_frozen
    _mounted_app = app
    _dev_posture_frozen = None  # force a re-read against the new app object

def _collect_dev_posture() -> Dict[str, int]:
    """Read the devPosture checks ONCE and return the frozen flag dict (only the
    flags the runtime could actually read this session). Python reads exactly
    two checks — ``debugFlag`` and ``profilingOpen``; ``verboseErrors`` and
    ``sourceMaps`` are a genuine can't-know for Python and are OMITTED. An
    UNREADABLE check's flag is left OUT entirely (honest absence)."""
    frozen: Dict[str, int] = {}
    debug_flag = _probe_dev_debug_flag()
    if debug_flag is not None:
        frozen["debugFlag"] = int(debug_flag)
    profiling_open = _probe_dev_profiling_open()
    if profiling_open is not None:
        frozen["profilingOpen"] = int(profiling_open)
    return frozen

def _probe_dev_profiling_open() -> Optional[int]:
    """The devPosture ``profilingOpen`` check for Python: is a trace hook /
    debugger / profiler attached? ``sys.gettrace()`` is not None. Always
    readable. Guarded — never raises."""
    try:
        return 1 if sys.gettrace() is not None else 0
    except Exception:  # noqa: BLE001
        return None

def _dev_posture_flags() -> Dict[str, int]:
    """Return the frozen flag dict, reading it ONCE (freeze-at-init) and caching
    it. Every later call returns the same cached result even if the environment
    changes after start — the cold-start-style freeze contract."""
    global _dev_posture_frozen
    if _dev_posture_override is not None:
        return dict(_dev_posture_override)
    if _dev_posture_frozen is None:
        _dev_posture_frozen = _collect_dev_posture()
    return dict(_dev_posture_frozen)
