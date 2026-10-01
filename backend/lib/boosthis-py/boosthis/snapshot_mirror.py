"""Privacy-safe perf-snapshot mirror for the Python runtime.

Python parity for the Node runtime's ``snapshot.ts`` (itself a mirror of RN's
``perfSnapshotUpload.ts``). It captures the whole live meter page as ONE small,
allowlisted JSON object and ships it to ``POST /api/snapshots`` (the same ingest
RN + Node use) so the developer's OWN AI can read the live picture back over the
hosted MCP live-read tools + ``GET /api/snapshot``. No new server surface is
added — the payload maps into the existing allowlisted snapshot fields
(``capturedAt``, ``runtimeVersion``, ``platform``, ``startedAt``,
``totalEvents``, ``rows``, ``crossCutting``, ``axes``) so it passes the server's
``sanitizeSnapshotLabels`` + PII guard unchanged.

PRIVACY: the snapshot carries only code-defined route labels + numeric
timings/counts/rating buckets — never user values, source, or PII. The whole
payload clears ``assert_no_pii`` in :mod:`boosthis.telemetry` before upload, and
per-row / finding labels ride through the same route-label guard as every other
transmit path. Uploading is OFF by default: the mirror only runs once the
developer explicitly opts in (``share_meter_with_ai=True``) or the server
reports the ``shareMeterWithAI`` directive after they connect an AI from the web
dashboard — matching RN's privacy-by-default contract. The kill-switch
(``BOOSTHIS_DISABLED``) and ``forget()`` always win.

UPLOAD MODEL: unlike Node (which drives an unref'd ``setTimeout`` loop), the
Python runtime has no always-running event loop, and Boosthis's own Python rule
book forbids idle-spinning background pollers. So the mirror is **work-gated**:
:func:`maybe_flush_snapshot` is called from the sample-record path
(``tracker._emit``) and, throttled to at most once per :data:`SNAPSHOT_FLUSH_MS`,
spawns a ONE-SHOT daemon thread (the same idiom :mod:`boosthis.community` uses)
to capture + upload — never blocking the host request and never spinning when
the app is idle.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Callable

from boosthis import budgets, samples
from boosthis.live_detectors import peak_retry_burst
from boosthis.health_axes import (
    MIN_SAMPLES_FOR_AXES,
    collect_findings,
    linear_score,
    rating_for,
)
from boosthis.event_loop_lag import read_event_loop_lag
from boosthis.meter_axes import read_meter_axes
from boosthis.runtime_vitals import read_vitals
from boosthis.build_identity import read_build, read_patch_lag, collect_deps, dep_inventory_enabled
from boosthis.candidates import note_recurring_findings
from boosthis.extra_meters import read_extra_meters
from boosthis import host_surface
from boosthis.os_meters import read_os_meters
from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.thresholds import (
    RUNTIME_VERSION,
    SCORE_THRESHOLDS,
    read_resilience_tail,
)

# How often the snapshot mirror re-uploads while sharing is enabled (the steady
# cadence the turbo first-run ramp below settles onto).
SNAPSHOT_FLUSH_MS = 60_000


def next_snapshot_delay_ms(session_age_ms: float) -> int:
    """Turbo first-run ramp: the delay until the NEXT perf-snapshot upload,
    based purely on how long this session/process has been alive.

    A freshly installed kit fills its dashboard tiles in seconds instead of a
    full minute by uploading denser early, then relaxing to the steady
    :data:`SNAPSHOT_FLUSH_MS`::

        age < 30s   -> 10s
        age < 90s   -> 20s
        age < 180s  -> 40s
        otherwise   -> SNAPSHOT_FLUSH_MS (60s)

    Stateless and age-driven ON PURPOSE: a skipped/empty upload (``has_data``
    is False) must NOT burn a ramp step, so the delay is derived from wall-clock
    session age rather than an upload counter. At most 4 uploads land in the
    first 70s (0s, +10s, +20s, +40s -> 70s), well under the server's 60 ingest
    req/min budget. This changes ONLY how soon existing data reaches the server;
    it touches no honesty gate — count-based gates simply clear sooner because
    reporting is denser early while sampling density is unchanged. Parity with
    the Node/RN/Web/Go/Java kits' ``nextSnapshotDelayMs``.
    """
    if session_age_ms < 30_000:
        return 10_000
    if session_age_ms < 90_000:
        return 20_000
    if session_age_ms < 180_000:
        return 40_000
    return SNAPSHOT_FLUSH_MS


def _turbo_disabled() -> bool:
    """``BOOSTHIS_TURBO=0`` (and only that exact value) opts out of the first-run
    ramp and falls straight back to the steady 60s interval. Any other value —
    or an unreadable env — leaves turbo on. Parity with the Node kit."""
    try:
        return os.environ.get("BOOSTHIS_TURBO") == "0"
    except Exception:  # noqa: BLE001
        return False


def _current_flush_delay_ms(now_ms: float) -> int:
    """Minimum interval until the next work-triggered flush, honouring the turbo
    ramp + kill switch. Falls back to the fixed steady interval when turbo is
    disabled. The session clock is anchored at import (:data:`_PROCESS_STARTED_AT_MS`)."""
    if _turbo_disabled():
        return SNAPSHOT_FLUSH_MS
    return next_snapshot_delay_ms(now_ms - _PROCESS_STARTED_AT_MS)

# Route rows are prefixed with "screen:" so the server's live-data MCP tools —
# which filter rows by that prefix (matching the RN + Node convention) — light
# up for Python installs exactly as they do for RN.
SCREEN_PREFIX = "screen:"

# Cap the number of route rows + findings in a single snapshot so a busy app can
# never ship an unbounded payload (the server also caps at ~2MB).
MAX_SNAPSHOT_ROWS = 60
MAX_SNAPSHOT_FINDINGS = 30

# Newest-first sample scan window used to derive per-route p50/p95/max/last.
SAMPLE_WINDOW = 1000

# Approximate process boot time from the boosthis import time so the meter page
# can show "running for N" without collecting any timestamp of user activity.
_PROCESS_STARTED_AT_MS = int(time.time() * 1000)

# Python finding kinds (from ``health_axes.collect_findings``) are not in the
# server's snapshot ``crossCutting.kind`` allowlist (which mirrors the RN
# ``CrossCuttingFinding.kind`` union). Map them onto the nearest allowed kind so
# they survive the sanitizer AND so the live ``budgets`` tool (which keys off
# ``kind == "regression"``) surfaces Python regressions. Anything already
# allowed passes through unchanged.
FINDING_KIND_MAP = {
    "slow-route": "slow-api",
    "budget-regression": "regression",
}

# PEP 440 pre-release ("1.0.0a1") -> semver ("1.0.0-alpha.1") so RUNTIME_VERSION
# satisfies the server's SNAP_RUNTIME_VERSION_RE (semver + optional
# alpha|beta|rc prerelease). A non-matching version is passed through unchanged
# and simply dropped by the server sanitizer (fail-safe).
_PEP440_RE = re.compile(r"^(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?$")
_PEP440_CHANNEL = {"a": "alpha", "b": "beta", "rc": "rc"}


def pep440_to_semver(version: str) -> str:
    m = _PEP440_RE.match(version)
    if not m:
        return version
    base, channel, num = m.group(1), m.group(2), m.group(3)
    if channel is None:
        return base
    return f"{base}-{_PEP440_CHANNEL[channel]}.{num}"


def _percentile(sorted_asc: list[int], p: float) -> int:
    if not sorted_asc:
        return 0
    idx = min(len(sorted_asc) - 1, int(len(sorted_asc) * p))
    return sorted_asc[idx]


def _build_rows() -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Group the recent sample window by route into worst-first rows.

    ``samples.recent()`` returns newest-first, so the FIRST sample seen for a
    route is its latest — that becomes ``last``. Returns the capped, p95-sorted
    row list AND an uncapped ``route -> {p95, count}`` lookup used to enrich the
    cross-cutting findings (whose Python shape lacks p95/count).
    """
    buf = samples.recent(limit=SAMPLE_WINDOW)
    durations_by_route: dict[str, list[int]] = {}
    last_by_route: dict[str, int] = {}
    for s in buf:
        durs = durations_by_route.get(s.name)
        if durs is None:
            durs = []
            durations_by_route[s.name] = durs
            last_by_route[s.name] = s.duration_ms
        durs.append(s.duration_ms)

    rows: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, int]] = {}
    for route, durs in durations_by_route.items():
        asc = sorted(durs)
        p95 = _percentile(asc, 0.95)
        rows.append(
            {
                "key": f"{SCREEN_PREFIX}{route}",
                "count": len(durs),
                "p50": _percentile(asc, 0.50),
                "p95": p95,
                "max": asc[-1],
                "last": last_by_route.get(route, 0),
            }
        )
        lookup[route] = {"p95": p95, "count": len(durs)}
    rows.sort(key=lambda r: r["p95"], reverse=True)
    return rows[:MAX_SNAPSHOT_ROWS], lookup


def _build_cross_cutting(
    findings: list[dict[str, Any]],
    rows_by_route: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """Map live findings into the allowlisted crossCutting shape.

    ``name`` is a raw route label (no "screen:" prefix — the live-data finding
    tools read it raw, matching RN + Node). Python findings carry only
    ``{kind, name}``, so p95/count are enriched from the per-route row lookup
    (0 when the route is outside the recent window).
    """
    out: list[dict[str, Any]] = []
    for f in findings[:MAX_SNAPSHOT_FINDINGS]:
        name = f.get("name", "")
        stats = rows_by_route.get(name, {})
        kind = f.get("kind", "")
        # Live-detector findings (retry-storm / idle-burn) carry their OWN honest
        # p95/count and a non-route name, so prefer those; route findings have no
        # p95/count and fall back to the per-route lookup.
        out.append(
            {
                "kind": FINDING_KIND_MAP.get(kind, kind),
                "name": name,
                "p95": f.get("p95", stats.get("p95", 0)),
                "count": f.get("count", stats.get("count", 0)),
            }
        )
    return out


def _build_axes(
    summary: dict[str, Any],
    all_budgets: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Two honest, display-only meters mapped into the server's axis allowlist:

    - responsiveness: request-latency p75 vs the shared TTI thresholds.
    - budget: share of budgeted (non-learning) routes still on budget.

    The four live-data triage tools read rows + crossCutting, NOT axes, so these
    are for the meter page + ``boosthis.snapshot`` only. Axes that cannot be
    computed honestly are simply omitted — the server renders absent axes as
    pending.
    """
    axes: dict[str, dict[str, Any]] = {}

    total = summary.get("total", 0)
    p75 = summary.get("p75_ms")
    if total >= MIN_SAMPLES_FOR_AXES and p75 is not None:
        score = linear_score(
            p75,
            SCORE_THRESHOLDS["tti"]["good"],
            SCORE_THRESHOLDS["tti"]["poor"],
        )
        axes["responsiveness"] = {
            "score": score,
            "rating": rating_for(score),
            "p75Ms": p75,
            "count": total,
        }

    # Resilience — latency-tail stability: the p99/p50 "tail blowup" ratio
    # blended with the poor-sample rate. Same 2-term wire formula as the
    # RN/Web/Node snapshot axis (tail good ≤3× · poor ≥8× · poor-rate 5%..25%)
    # so the meter reads identically across runtimes. The LOCAL dashboard keeps
    # its richer 3-term version (with the budget-regression term) in
    # health_axes.py — the uploaded shape stays the cross-runtime wire
    # standard. Additive + display-only — never feeds the Speed score.
    # The tail term obeys the cross-kit contract in thresholds.py: an absolute
    # floor (a 22 ms worst case is not a resilience problem however large the
    # ratio), a sample floor (a p99 over five samples is just the slowest of
    # five) and an honest abstention when the median is below the capture
    # resolution — where this axis used to hand out a free perfect score.
    p50 = summary.get("p50_ms")
    p99 = summary.get("p99_ms")
    tail_state, tail_ratio = read_resilience_tail(p50, p99, total)
    if tail_state == "unmeasurable":
        # 0.6 of the score is the tail. Withhold the whole axis rather than
        # publish a verdict whose dominant term was never measured.
        axes["resilience"] = {
            "score": None,
            "rating": "pending",
            "tailRatio": None,
            "p50Ms": p50,
            "p99Ms": p99,
            "sampleCount": total,
        }
    elif tail_state != "insufficient":
        tail_score = 100 if tail_state == "flat" else linear_score(tail_ratio, 3, 8)
        poor_rate = (summary.get("poor", 0) / total) if total > 0 else 0.0
        poor_rate_score = linear_score(poor_rate, 0.05, 0.25)
        score = round(0.6 * tail_score + 0.4 * poor_rate_score)
        axes["resilience"] = {
            "score": score,
            "rating": rating_for(score),
            "tailRatio": None if tail_ratio is None else round(tail_ratio * 10) / 10,
            "p50Ms": p50,
            "p99Ms": p99,
            "sampleCount": total,
        }

    # Only routes that reached a verdict. A route still learning has no
    # baseline, and one the shared ring evicted below the floor never drew one
    # — neither may sit in the denominator of an on-budget percentage.
    budgeted = [b for b in all_budgets if budgets.is_judged(b)]
    if budgeted:
        on_budget = sum(1 for b in budgeted if b.get("state") != "regressed")
        pct = round(100 * on_budget / len(budgeted))
        axes["budget"] = {
            "score": pct,
            "rating": rating_for(pct),
            "onBudget": on_budget,
            "total": len(budgeted),
            "pct": pct,
        }

    # Event-loop lag: the truest single signal of a blocked/overloaded async
    # process. Additive and display-only — never feeds the Speed score. Omitted
    # (rendered pending) until enough probes have landed, and always absent on
    # sync WSGI apps (no running loop to probe).
    lag = read_event_loop_lag()
    if lag:
        axes["eventLoopLag"] = dict(lag)

    # Runtime vitals (memory stability, GC pressure, reliability) — additive,
    # display-only, omitted while warming up. Cross-runtime standard axes.
    for key, axis in read_vitals().items():
        axes[key] = dict(axis)

    # Server-side meter parity with the RN kit: confidence (four scalar keys),
    # baseline, latencyFloor, network, idle, schedulerLatency. Each is additive,
    # display-only (never feeds the Speed score) and OMITTED while warming up
    # (the server renders an absent axis as pending) — the four confidence
    # scalars are always present since "no samples yet" is itself honest info.
    for key, value in read_meter_axes(summary).items():
        axes[key] = dict(value) if isinstance(value, dict) else value

    # The 2026-08 Python meter batch (GIL contention, task backlog, worker
    # recycling, swallowed errors, thread-pool starvation, blocking-async,
    # fork churn). Additive, display-only, each omitted until honestly
    # computable — environment-dependent axes stay absent forever when the
    # signal cannot exist (sync app, no gunicorn, free-threaded build, …).
    for key, axis in read_extra_meters().items():
        axes[key] = dict(axis)

    # The 2026-08 "honest limit" batch (allocator churn, peak RSS, context
    # switching, page faults, I/O pressure, CPU entitlement, descriptor mix,
    # import churn, async slow callbacks, GC occupancy, runtime capability).
    # Additive, display-only; Linux-only sources are ABSENT (never guessed)
    # off-Linux, and the asyncio-debug meter appears only when the host
    # itself enabled debug mode.
    for key, axis in read_os_meters().items():
        axes[key] = dict(axis)

    # Patch Lag ("exposure window"): how stale the running build is. Emitted
    # ONLY when a build TIME is known (honest absence otherwise — never a
    # fake/warming axis). ADDITIVE + display-only — never feeds the Speed score.
    # Reports build lag only; it NEVER implies the app is safe/patched.
    patch_lag = read_patch_lag()
    if patch_lag is not None:
        axes["patchLag"] = patch_lag

    # "Not measurable here", with the reason — never a blank tile. Some readings
    # cannot exist on some hosts: a Streamlit rerun has no HTTP status to judge
    # a refusal by, a Django WSGI request has no event loop to lag. Those axes
    # are named as not measurable, with the reason, instead of sitting on
    # "warming up" forever waiting for a signal that will never come.
    #
    # LAST, and only where the host produced nothing: a reading that DID arrive
    # always wins over a claim it could not, and going last is what makes that
    # true — every producer above has already had its say, so none of them can
    # overwrite a named reason afterwards.
    for key, axis in host_surface.unmeasurable_axes().items():
        if key not in axes:
            axes[key] = dict(axis)

    return axes


def capture_perf_snapshot() -> dict[str, Any]:
    """Build the current privacy-safe snapshot from live in-process state.

    Pure — reads only the sample ring, budgets, and findings; performs no
    network I/O.
    """
    summary = samples.summary()
    all_budgets = budgets.all_statuses()
    regressions = [b for b in all_budgets if b.get("state") == "regressed"]
    findings = collect_findings(summary, regressions)
    # What this app KEEPS doing. The AI anti-patterns above are recomputed
    # from scratch in every process, so without a memory on the device a
    # repeat reads exactly like a first sighting. Remembering them costs no
    # new collection: the readings are already taken, the store already
    # exists, and only counts ever leave. Keyed to the declared release so
    # recurrence can be counted in releases and not only in restarts; the key
    # is hashed locally and never travels.
    try:
        build = read_build() or {}
        commit = build.get("commit")
        build_time = build.get("buildTimeMs")
        release_key = (
            f"c:{commit}"
            if isinstance(commit, str) and commit
            else (f"t:{build_time}" if isinstance(build_time, int) else None)
        )
        note_recurring_findings(findings, release_key=release_key)
    except Exception:  # noqa: BLE001
        # Bookkeeping never disturbs a snapshot.
        pass
    rows, rows_by_route = _build_rows()
    return {
        "capturedAt": int(time.time() * 1000),
        "runtimeVersion": pep440_to_semver(RUNTIME_VERSION),
        # Which Python host is mounted, not just "Python": the dashboard needs
        # it to tell a reading this framework can never take from one that is
        # merely late. Plain "python" when nothing is mounted.
        "platform": host_surface.snapshot_platform(),
        "startedAt": _PROCESS_STARTED_AT_MS,
        "totalEvents": summary.get("total", 0),
        "rows": rows,
        "crossCutting": _build_cross_cutting(findings, rows_by_route),
        "axes": _build_axes(summary, all_budgets),
        # Build identity ({commit?, buildTimeMs?, buildAgeMs?}) — included
        # whenever any component is known; omitted entirely otherwise (never
        # fabricated). Feeds the cross-runtime Patch Lag meter.
        **({"build": build_obj} if (build_obj := read_build()) else {}),
        # Dependency inventory — OPT-IN, OFF BY DEFAULT (BOOSTHIS_DEP_INVENTORY=1).
        # Off ⇒ no "deps" field at all.
        **(
            {"deps": deps}
            if dep_inventory_enabled() and (deps := collect_deps())
            else {}
        ),
        **(
            {"events": {"retryBurstMax10s": burst_peak}}
            if (burst_peak := peak_retry_burst()) > 0
            else {}
        ),
        # The whole route list, asked of the running framework — so the map can
        # show every route the app has, not only the ones traffic has reached.
        # ALWAYS present, whatever the answer was: "no app was handed over" is
        # `unsupported`, not silence. The block is absent only when this module
        # is not there at all, and that absence may mean one thing only — a kit
        # older than the feature.
        **(
            {"routeList": route_list}
            if (route_list := _route_list()) is not None
            else {}
        ),
    }


def _route_list() -> dict[str, Any] | None:
    """The route list block, or None. Never raises into a snapshot."""
    try:
        from boosthis import route_inventory

        return route_inventory.route_list_for_snapshot()
    except Exception:  # noqa: BLE001
        return None


#: The one value that says a developer ASKED for this reading rather than the
#: timer taking it. Named so the build guard can check the declaration in
#: ``lib/on-demand-reading-coverage.json`` against this kit's own bytes in both
#: directions — see ``docs/on-demand-reading-contract.md``.
READING_TRIGGER_ON_DEMAND = "on-demand"


def has_data(snap: dict[str, Any]) -> bool:
    """A snapshot worth uploading has at least one recorded sample or route
    row — an empty snapshot from a just-booted app is skipped so we never ship a
    content-free payload.
    """
    return bool(snap.get("totalEvents", 0) > 0 or snap.get("rows"))


# Has the route-list-only upload already gone? Process-lifetime state: the
# point is to send the list ONCE from an app that has served nothing, not to
# re-send the same list every tick for as long as the app stays quiet.
_route_list_only_sent = False


def route_list_worth_sending_alone(snap: dict[str, Any]) -> bool:
    """A route list is worth ONE upload even from an app with no traffic.

    ``has_data`` exists to stop a content-free payload, and a route table is
    not content-free: it is the only thing that can put the whole app on the
    map before traffic arrives, and the privacy page says in as many words
    that it is the one thing that travels before any traffic does. Without
    this, a Flask app that had registered its routes and served nobody yet
    would send nothing at all, and the page would say "nothing yet" about an
    app whose list we were holding.

    Once, though. A quiet app re-sending an unchanged list every tick would
    be paying for the same sentence for ever; the next upload carries it
    again as soon as there is any traffic to carry it with.
    """
    if _route_list_only_sent:
        return False
    block = snap.get("routeList")
    if not isinstance(block, dict):
        return False
    entries = block.get("entries")
    return isinstance(entries, list) and len(entries) > 0


# --- work-gated upload trigger --------------------------------------------

SnapshotSubmitter = Callable[[dict[str, Any]], int]

_submitter: SnapshotSubmitter | None = None
_last_flush_at_ms: float = 0.0
_in_flight = False
_state_lock = threading.Lock()


def set_snapshot_submitter(fn: SnapshotSubmitter | None) -> None:
    """Register (or clear) the function that actually ships a snapshot.

    :mod:`boosthis.telemetry` sets this to ``_transmit_snapshot`` once effective
    meter-sharing is on and clears it (``None``) when sharing turns off or on
    ``forget()``. With no submitter every trigger is a silent no-op, exactly
    like Node's ``uploadPerfSnapshotNow`` without a submitter registered.
    """
    global _submitter
    with _state_lock:
        _submitter = fn


def has_submitter() -> bool:
    """True when a submitter is registered (i.e. meter-sharing is effectively
    on). Used by the telemetry layer to detect the off→on rising edge so it can
    fire one immediate flush."""
    with _state_lock:
        return _submitter is not None


def _flush_sync() -> int:
    """Capture + submit one snapshot inline (no thread). Never raises —
    instrumentation must stay silent. Returns the count the submitter reports,
    or 0 when there is no submitter / no data / an error."""
    global _in_flight
    with _state_lock:
        submitter = _submitter
        if submitter is None or _in_flight:
            return 0
        _in_flight = True
    global _route_list_only_sent
    try:
        snap = capture_perf_snapshot()
        measured = has_data(snap)
        list_only = not measured and route_list_worth_sending_alone(snap)
        if not measured and not list_only:
            return 0
        shipped = submitter(snap)
        # Spent only on an ACCEPTED upload. The submitter reports how many
        # snapshots the server took, and it returns 0 for every ordinary
        # refusal it handles itself — no consent yet, no token, an HTTP error,
        # an unreachable server — without raising. Marking the one-shot on a
        # plain return would spend it on exactly those, and a refused first
        # upload must leave the app's list still owed.
        if list_only and shipped > 0:
            _route_list_only_sent = True
        return shipped
    except Exception:  # noqa: BLE001
        return 0
    finally:
        with _state_lock:
            _in_flight = False


def read_now() -> dict[str, Any]:
    """TAKE A READING NOW, from an app that may have served nothing at all.

    The ordinary snapshot, captured the ordinary way, stamped as asked-for and
    handed to the SAME submitter the timer uses. Nothing is measured
    differently and nothing is invented: what a process can answer about itself
    — memory, uptime, its container's ceiling, the build it is running, its own
    route table — is genuinely known the instant it starts, and what needs a
    served request is simply absent, which is how every Boosthis surface
    already words it.

    The one thing it does that the timer does not is skip :func:`has_data`.
    That gate exists so a quiet app does not pay for a content-free tick every
    interval; it is precisely wrong for the one upload a developer asked for,
    because the whole point is a reading before there is traffic to justify
    one.

    Inline, in the calling thread, unlike :func:`flush_snapshot_now` — the
    caller is waiting for the answer. It makes no request against the host
    application and never raises.

    Returns ``{"sent": bool, "reason": str | None, "measuredRequests": int}``.
    ``reason`` is one of a closed set: ``not-started`` (the kit was never
    switched on, or sharing is off), ``already-reading``, ``refused`` (the
    server did not take it) or ``failed``.

    See docs/on-demand-reading-contract.md.
    """
    global _in_flight, _route_list_only_sent
    if is_boosthis_disabled():
        return {"sent": False, "reason": "not-started", "measuredRequests": 0}
    with _state_lock:
        submitter = _submitter
        if submitter is None:
            return {"sent": False, "reason": "not-started", "measuredRequests": 0}
        if _in_flight:
            return {"sent": False, "reason": "already-reading", "measuredRequests": 0}
        _in_flight = True
    measured_requests = 0
    try:
        snap = capture_perf_snapshot()
        snap["readingTrigger"] = READING_TRIGGER_ON_DEMAND
        raw = snap.get("totalEvents", 0)
        measured_requests = raw if isinstance(raw, int) else 0
        shipped = submitter(snap)
        # A reading that carried the route list and was ACCEPTED spends the
        # one-shot, exactly as the timer's list-only upload does: the list has
        # arrived, and re-sending it every time somebody presses this would be
        # paying for the same sentence twice.
        if shipped > 0 and not has_data(snap):
            _route_list_only_sent = True
        if shipped > 0:
            return {
                "sent": True,
                "reason": None,
                "measuredRequests": measured_requests,
            }
        return {
            "sent": False,
            "reason": "refused",
            "measuredRequests": measured_requests,
        }
    except Exception:  # noqa: BLE001
        return {
            "sent": False,
            "reason": "failed",
            "measuredRequests": measured_requests,
        }
    finally:
        with _state_lock:
            _in_flight = False


def flush_snapshot_now() -> None:
    """Spawn a one-shot daemon thread to capture + upload immediately.

    Used for the first flush the moment sharing is enabled (so the AI has
    something to read at once) and by the CLI ``connect-ai`` command. Skips
    entirely under the kill-switch or in tests (where the submitter is invoked
    directly). No-op without a registered submitter.
    """
    if is_boosthis_disabled() or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    with _state_lock:
        if _submitter is None:
            return
    threading.Thread(target=_flush_sync, daemon=True).start()


def flush_snapshot_blocking(budget_ms: float = 2_000.0) -> int:
    """Capture and ship one snapshot INLINE, in the calling thread, inside a
    wall-clock budget. Returns what the submitter reported (0 for nothing).

    The shutdown counterpart of :func:`flush_snapshot_now` — no thread, because
    a daemon thread is frozen rather than joined when the interpreter exits.
    One attempt only: unlike the sample and span queues there is nothing
    buffered to drain, just a picture to take, and a second try inside the same
    couple of seconds would photograph the same thing. Never raises. See
    :mod:`boosthis.exit_flush`.
    """
    if is_boosthis_disabled():
        return 0
    try:
        if budget_ms <= 0:
            return 0
        with _state_lock:
            if _submitter is None:
                return 0
        return _flush_sync()
    except Exception:  # noqa: BLE001
        return 0


def maybe_flush_snapshot() -> None:
    """Work-gated, throttled trigger called from the sample-record path.

    Fires at most once per :func:`_current_flush_delay_ms` — the turbo first-run
    ramp (:func:`next_snapshot_delay_ms`) early on, then the steady
    :data:`SNAPSHOT_FLUSH_MS`. Cheap enough to call on every measurement — the
    throttle check is a single timestamp compare under a lock; the actual
    capture + HTTP upload happens on a daemon thread so the host request is
    never blocked. Never raises.
    """
    global _last_flush_at_ms
    if is_boosthis_disabled() or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    now = time.time() * 1000
    with _state_lock:
        if _submitter is None:
            return
        if now - _last_flush_at_ms < _current_flush_delay_ms(now):
            return
        _last_flush_at_ms = now
    try:
        threading.Thread(target=_flush_sync, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def _reset_for_tests() -> None:
    """Clear module state between hermetic test runs."""
    global _submitter, _last_flush_at_ms, _in_flight, _route_list_only_sent
    with _state_lock:
        _submitter = None
        _last_flush_at_ms = 0.0
        _in_flight = False
        _route_list_only_sent = False


__all__ = [
    "SNAPSHOT_FLUSH_MS",
    "READING_TRIGGER_ON_DEMAND",
    "next_snapshot_delay_ms",
    "capture_perf_snapshot",
    "has_data",
    "pep440_to_semver",
    "set_snapshot_submitter",
    "maybe_flush_snapshot",
    "read_now",
    "flush_snapshot_now",
    "flush_snapshot_blocking",
]
