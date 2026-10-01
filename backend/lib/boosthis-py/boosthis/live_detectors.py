"""Boosthis: live cross-cutting detectors (Python).

Two always-on, observe-only detectors that surface performance anti-patterns the
per-route timer cannot see, emitted as ordinary cross-cutting findings so they
ride the EXISTING candidate-rule + snapshot pipeline (no new server route, no new
table):

* ``retry-storm`` -- the same outbound host is hammered many times in a short
  window with no backoff (a classic thundering-herd / missing-retry-budget bug).
  Fed explicitly via :func:`record_outbound_attempt` (the default, zero-overhead
  path), or -- opt-in -- from an ``sys.addaudithook`` socket observer armed by
  :func:`arm_outbound_audit_hook` (fed primarily from ``socket.getaddrinfo``,
  which carries the ORIGINAL hostname, so the ignore-own-host guard matches).
* ``idle-burn`` -- the process keeps burning CPU while NO request is in flight (a
  stranded timer / busy-poll leak). Measured only at request boundaries via
  ``time.process_time()`` vs wall clock -- there is NO background poller
  (Boosthis's own rule book forbids idle spinners), so the idle-burn detector
  never itself burns idle.

PRIVACY (identical to every other finding):
    * The ``name`` we keep is a bare outbound HOSTNAME (retry-storm) or a fixed
      non-route label (idle-burn). It never carries a path, query, userinfo, or
      port, and it is PII-filtered again before any upload.
    * ``signature_for`` drops ``name`` entirely, so the candidate fingerprint is
      kind + severity bucket + count bucket only -- never a host, value, or code.
    * ``p95`` / ``count`` are honest measurements (burst span in ms + attempt
      count; CPU-active ms during an idle window + occurrence count), NOT
      synthetic severity numbers.
    * Everything is in-memory only. Nothing leaves the process here -- findings
      only travel through the already-gated snapshot/candidate pipeline, which is
      off for unregistered installs and silenced by ``BOOSTHIS_DISABLED``.

Node<->Python parity: the wire OUTPUT (finding kind + field shape) is
byte-identical to ``lib/boosthis-runtime-node/src/liveDetectors.ts``; only the
runtime-specific plumbing to FEED the detectors differs (diagnostics_channel +
ELU there, explicit API / audit hook + process_time here).
"""

from __future__ import annotations

import ipaddress
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .runtime_flags import is_boosthis_disabled
from .trace_propagation import headers_for_next_hop
from .trace_propagation import host_of as propagation_host_of

# ── Tunables (mirror liveDetectors.ts) ──────────────────────────────────────
RETRY_WINDOW_MS = 10_000
RETRY_STORM_THRESHOLD = 5
RETRY_BACKOFF_MS = 1_000
MAX_TRACKED_HOSTS = 200

# Fan-out (thundering-herd) tunables — mirror liveDetectors.ts byte-for-byte.
# A burst of many DISTINCT outbound hosts all contacted inside a very short
# window (one cache miss firing a wave of parallel downstream calls).
CLUSTER_WINDOW_MS = 50
# Distinct hosts within CLUSTER_WINDOW_MS at/above which we flag a fan-out.
CLUSTER_HOST_THRESHOLD = 3

IDLE_GAP_MS = 3_000
IDLE_ACTIVE_MS = 1_000
IDLE_UTIL_THRESHOLD = 0.5

# ── Retry-storm state ───────────────────────────────────────────────────────
_attempts_by_host: Dict[str, List[float]] = {}

# Hosts we deliberately never count toward a retry-storm. Boosthis's OWN
# telemetry endpoint is registered here (via ``ignore_host``) so its periodic
# snapshot/candidate flushes — which the opt-in socket audit hook would also
# observe — can never self-trigger a retry-storm finding naming our API host.
# Holds only our own endpoint host, so it carries no PII.
_ignored_hosts: set[str] = set()

# Session-peak same-host attempt count observed inside the rolling window while
# NO backoff was present — the anonymous "how hard did the worst retry storm
# hammer one destination" number mirrored to the snapshot as
# ``events.retryBurstMax10s``. A bare count only — never a host or URL.
_retry_burst_peak = 0
# Outbound attempts observed since this process started, whether the rolling
# retry window still holds them or not. Read only as READ-BACK EVIDENCE that
# outbound observation is really attached in this app (see
# ``boosthis.coverage_inventory``): "we armed it" is a claim, "we have seen a
# call through it" is a fact. A count and nothing else.
_outbound_attempts_seen = 0


def ignore_host(target: Any) -> None:
    """Exclude a host from retry-storm accounting (e.g. Boosthis's own endpoint).

    Idempotent. Wired from ``enable_telemetry`` with the configured endpoint so
    the runtime never flags its own upload host; the value passes through the
    same :func:`host_of` normalizer, so only a bare hostname is ever stored.
    """
    host = host_of(target)
    if host:
        _ignored_hosts.add(host)


def host_of(target: Any) -> str:
    """Extract a bare, non-PII hostname from a URL/origin/host string.

    Returns ``""`` when nothing host-shaped is present. Never raises. Path,
    query, userinfo, and port are all discarded.
    """
    if not isinstance(target, str) or not target:
        return ""
    try:
        if "://" in target:
            # HTTP observers retain the method in their repeated-work identity
            # ("GET https://host/path"). Strip only that fixed marker before
            # extracting the retry detector's bare-host identity.
            candidate = target.split(None, 1)[-1]
            return (urlsplit(candidate).hostname or "").lower()
    except Exception:
        pass
    s = target.strip()
    at = s.rfind("@")
    if at >= 0:
        s = s[at + 1 :]  # strip userinfo
    s = s.split("/")[0].split("?")[0]  # strip path/query
    s = s.split(":")[0]  # strip port
    return s.lower()


def _db_scope_now() -> Any:
    """The in-flight request's database tally, read on the ISSUING stack.

    Handed to :func:`boosthis.db_work.note_hosted_db_call` when the call ends,
    so a hosted-database call that finishes after its request moved on is still
    credited to the request that made it. Never raises; None simply means the
    call is not attributed to any request.
    """
    try:
        from boosthis.db_work import current_db_scope

        return current_db_scope()
    except Exception:  # noqa: BLE001
        return None


def _pending_db_scope(pending: Any) -> Any:
    """The database scope stored beside a pending outbound marker.

    Tolerates a two-element marker: an in-flight call that was stamped by an
    older wrapper (a module reload mid-flight) is simply not attributed, rather
    than raising into a host's request teardown.
    """
    try:
        return pending[2] if len(pending) > 2 else None
    except Exception:  # noqa: BLE001
        return None


def record_outbound_attempt(
    target: Any,
    duration_ms: float | None = None,
    outcome: str | None = None,
) -> None:
    """Record one outbound attempt to ``target``. No-op under the kill-switch.

    This is the kit's SINGLE outbound-observation point. When the caller also
    knows the call's ``duration_ms`` and a coarse ``outcome`` bucket
    (``"ok" | "error" | "timeout" | "stall"``), those are forwarded to the
    additive ``network`` meter here — so the meter reuses this exact
    observation point instead of adding a second interception layer. Callers
    that only know the target (e.g. the audit-hook socket feed, which fires at
    call START) pass neither; those calls still feed retry-storm, and the
    network meter is fed from the stdlib ``http.client`` hook below, which
    knows how each call ENDED. Counts + durations only; never a host/URL."""
    if is_boosthis_disabled():
        return
    host = host_of(target)
    if host and host in _ignored_hosts:
        # An ignored host is Boosthis's OWN endpoint, and it is refused HERE,
        # above every counter — not after some of them. Excluding it only from
        # the identity-forming readings below would have left the network meter
        # counting our own registrations, check-ins and snapshot flushes and
        # reporting them to the developer as their app's outbound traffic, on
        # an axis whose whole point is that an app making no outbound calls
        # shows no row at all. It would also have tied a customer-facing meter
        # to OUR ingest: a slow or refusing Boosthis would degrade their
        # outbound reliability and point them at their own dependencies.
        #
        # The kit's own hooks already refuse our endpoint further upstream
        # (``_rw_target`` and ``outbound_target_from_url`` both return None for
        # it, so no pending marker is ever created), so in practice this guards
        # the EXPLICIT path: a host app that wires this call into the shared
        # HTTP client it owns.
        return
    if outcome is not None:
        try:
            from boosthis.meter_axes import record_network_outcome

            record_network_outcome(duration_ms, outcome)
        except Exception:  # noqa: BLE001
            pass
    if not host:
        # Nothing host-shaped to attribute: the network outcome above is still
        # a real call the caller told us about, but nothing below can be keyed
        # without a host.
        return
    # Repeated-work meter: reuse this SAME observation point (no second
    # interception layer) to notice the identical downstream call the fan-out
    # rule warns about. It counts only when ``target`` is a full URL — the
    # audit-hook socket feed below passes a bare hostname, and two connections
    # to one host are not necessarily the same call. The URL is hashed inside
    # the detector and dropped; only a count ever leaves.
    try:
        from boosthis.repeated_work import note_outbound_call

        note_outbound_call(target, duration_ms)
    except Exception:  # noqa: BLE001
        pass
    # Read-back evidence that this app's outbound calls really reach the
    # observer. The coverage inventory only ever asks whether it is above zero.
    global _outbound_attempts_seen
    _outbound_attempts_seen += 1
    now = time.monotonic() * 1000.0
    arr = _attempts_by_host.get(host)
    if arr is None:
        if len(_attempts_by_host) >= MAX_TRACKED_HOSTS:
            return  # bounded
        arr = []
        _attempts_by_host[host] = arr
    arr.append(now)
    cutoff = now - RETRY_WINDOW_MS
    while arr and arr[0] < cutoff:
        arr.pop(0)
    # Update the session-peak burst counter (anonymous number for the snapshot's
    # ``events.retryBurstMax10s``): only qualifying storms count — enough
    # attempts in the window AND no real backoff between them.
    global _retry_burst_peak
    if (
        len(arr) >= RETRY_STORM_THRESHOLD
        and _median_gap(arr) < RETRY_BACKOFF_MS
        and len(arr) > _retry_burst_peak
    ):
        _retry_burst_peak = len(arr)

def outbound_attempts_observed() -> int:
    """Outbound calls this observer has actually seen since the process began.

    Read-back evidence, not a claim: arming installs a hook, and a hook nothing
    ever calls looks exactly like an app that makes no outbound calls. Only a
    call that arrived here says the surface is genuinely watched. A count, and
    nothing else — no host, no URL. 0 under the kill-switch."""
    if is_boosthis_disabled():
        return 0
    return _outbound_attempts_seen
def peak_retry_burst() -> int:
    """Session-peak same-destination retry-burst strength (attempts inside one
    rolling window with no backoff). 0 when no storm was observed. Read by the
    snapshot builder — ships as the anonymous ``events.retryBurstMax10s`` number
    (count only, never a host/URL), mirroring the web kit's
    ``events.requestBurstMax1s`` posture."""
    if is_boosthis_disabled():
        return 0
    return _retry_burst_peak


def _median_gap(sorted_ts: List[float]) -> float:
    if len(sorted_ts) < 2:
        return 0.0
    gaps = sorted(sorted_ts[i] - sorted_ts[i - 1] for i in range(1, len(sorted_ts)))
    mid = len(gaps) // 2
    if len(gaps) % 2 == 0:
        return (gaps[mid - 1] + gaps[mid]) / 2.0
    return gaps[mid]


def _collect_retry_storms() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    now = time.monotonic() * 1000.0
    cutoff = now - RETRY_WINDOW_MS
    for host, arr in _attempts_by_host.items():
        while arr and arr[0] < cutoff:
            arr.pop(0)
        if len(arr) < RETRY_STORM_THRESHOLD:
            continue
        if _median_gap(arr) >= RETRY_BACKOFF_MS:
            continue  # backoff present → fine
        span_ms = int(round(arr[-1] - arr[0]))
        out.append(
            {
                "kind": "retry-storm",
                "name": host,
                "p95": span_ms,
                "count": len(arr),
                "hint": "The same outbound host was retried many times with no backoff.",
            }
        )
    return out


def _collect_concurrent_clusters() -> List[Dict[str, Any]]:
    """Detect an outbound fan-out burst: many DISTINCT hosts all contacted within
    a very short window (a thundering-herd — e.g. one cache miss firing a wave of
    parallel downstream calls). Derived from the SAME ``_attempts_by_host`` buffer
    as retry-storm, so it adds NO new collection. Honest measurements: the burst
    span (ms across the widest cluster) and the distinct-host count. Reuses the
    server-allowlisted ``api-thundering-herd`` finding kind.

    Algorithm (byte-identical to the Node sibling): flatten every in-window
    (ts, host) pair, sort by ts, then slide a ``CLUSTER_WINDOW_MS`` window and
    track the largest set of DISTINCT hosts co-occurring inside it.
    """
    now = time.monotonic() * 1000.0
    cutoff = now - RETRY_WINDOW_MS
    events: List[Dict[str, Any]] = []
    for host, arr in _attempts_by_host.items():
        for ts in arr:
            if ts >= cutoff:
                events.append({"ts": ts, "host": host})
    if len(events) < CLUSTER_HOST_THRESHOLD:
        return []
    events.sort(key=lambda e: e["ts"])
    best_hosts = 0
    best_span = 0.0
    left = 0
    window_hosts: Dict[str, int] = {}
    for right in range(len(events)):
        h = events[right]["host"]
        window_hosts[h] = window_hosts.get(h, 0) + 1
        while events[right]["ts"] - events[left]["ts"] > CLUSTER_WINDOW_MS:
            lh = events[left]["host"]
            c = window_hosts.get(lh, 0) - 1
            if c <= 0:
                window_hosts.pop(lh, None)
            else:
                window_hosts[lh] = c
            left += 1
        distinct = len(window_hosts)
        if distinct > best_hosts:
            best_hosts = distinct
            best_span = events[right]["ts"] - events[left]["ts"]
    if best_hosts < CLUSTER_HOST_THRESHOLD:
        return []
    return [
        {
            "kind": "api-thundering-herd",
            "name": "outbound-fan-out",
            "p95": int(round(best_span)),
            "count": best_hosts,
            "hint": "Many distinct outbound hosts were contacted in a single burst (a thundering-herd fan-out).",
        }
    ]


# ── Idle-burn state ─────────────────────────────────────────────────────────
_in_flight = 0
_idle_since_wall = 0.0
_idle_since_cpu = 0.0
_idle_burn_count = 0
_worst_idle_active_ms = 0


def note_request_start() -> None:
    """Call at the START of every request (mirrors liveDetectors.ts).

    The in-flight increment is UNCONDITIONAL — it must NOT sit behind the
    kill-switch, because ``note_request_end`` is its guaranteed pair on the
    exit path and the switch could flip between the two. If we skipped the
    increment while disabled but the matching ``note_request_end`` ran while
    enabled, the shared counter would be driven negative (and vice-versa: a
    counted start whose end is skipped leaks forever). Under the GIL a bare
    ``+= 1`` is lock-free and atomic, so this stays mutex-free. The kill-switch
    gate belongs ONLY around the idle-burn RECORDING below, never around the
    counter itself.
    """
    global _in_flight
    # Retry attachment here as well as at telemetry enable. Queue libraries are
    # often imported lazily; the adapter throttles its sys.modules-only scan.
    try:
        from boosthis.job_adapters import arm_job_systems

        arm_job_systems()
    except Exception:  # noqa: BLE001
        pass
    # Host-suspend sensor: a request has arrived — refresh the traffic-time
    # marks and detect a wake after a suspicious idle gap. Reuses THIS existing
    # boundary (no new timer). Fully guarded; no-op under the kill-switch.
    try:
        from .suspend_sensor import note_suspend_activity

        note_suspend_activity()
    except Exception:  # noqa: BLE001
        pass
    if not is_boosthis_disabled() and _in_flight == 0 and _idle_since_wall > 0.0:
        wall_gap_ms = (time.monotonic() - _idle_since_wall) * 1000.0
        cpu_gap_ms = (time.process_time() - _idle_since_cpu) * 1000.0
        # Feed the idle-efficiency meter EVERY idle window (busy CPU vs idle
        # wall), not just the ones that trip the burn detector — reusing this
        # same request-boundary so no new poller is added. Gated on a real idle
        # gap so a rapid-fire request stream never floods it with noise windows.
        if wall_gap_ms >= IDLE_GAP_MS:
            try:
                from boosthis.meter_axes import record_idle_window

                record_idle_window(wall_gap_ms, cpu_gap_ms)
            except Exception:  # noqa: BLE001
                pass
        if wall_gap_ms >= IDLE_GAP_MS and cpu_gap_ms >= IDLE_ACTIVE_MS:
            util = cpu_gap_ms / wall_gap_ms if wall_gap_ms > 0 else 0.0
            if util >= IDLE_UTIL_THRESHOLD:
                _mark_idle_burn(cpu_gap_ms)
    # Unconditional — paired with note_request_end's unconditional decrement.
    _in_flight += 1


def current_in_flight() -> int:
    """How many requests are in flight RIGHT NOW (the same counter the idle-burn
    boundary maintains). Read by the loadDeflection meter at a request start to
    bucket that request's duration by concurrency. A bare int read — never
    raises, never mutates."""
    return _in_flight


def note_request_end() -> None:
    """Call when a request FINISHES (mirrors liveDetectors.ts).

    The in-flight decrement is UNCONDITIONAL and clamped at 0 — it must run
    even under the kill-switch so a request counted at start (while enabled)
    can always be un-counted at end, no matter when the switch flips. The
    ``max(0, ...)`` clamp means a ``clear_detectors``/reset mid-request can
    never drive the counter negative. The kill-switch gate belongs ONLY around
    the idle-timestamp bookkeeping, never around the counter itself.
    """
    global _in_flight, _idle_since_wall, _idle_since_cpu
    # Unconditional, clamped — paired with note_request_start's increment.
    if _in_flight > 0:
        _in_flight -= 1
    if is_boosthis_disabled():
        return
    if _in_flight == 0:
        _idle_since_wall = time.monotonic()
        _idle_since_cpu = time.process_time()
        # Host-suspend sensor: the loop just went idle — let consumers snapshot
        # their clean traffic-time baselines. Reuses THIS existing boundary (no
        # new timer). Fully guarded; no-op under the kill-switch.
        try:
            from .suspend_sensor import note_suspend_idle_start

            note_suspend_idle_start()
        except Exception:  # noqa: BLE001
            pass


def _mark_idle_burn(active_ms: float) -> None:
    global _idle_burn_count, _worst_idle_active_ms
    _idle_burn_count += 1
    _worst_idle_active_ms = max(_worst_idle_active_ms, int(round(active_ms)))


def _collect_idle_burn() -> List[Dict[str, Any]]:
    if _idle_burn_count == 0:
        return []
    return [
        {
            "kind": "idle-burn",
            "name": "cpu-during-idle",
            "p95": _worst_idle_active_ms,
            "count": _idle_burn_count,
            "hint": "The process kept burning CPU while no request was in flight.",
        }
    ]


# ── Opt-in socket audit hook (auto retry-storm observation) ──────────────────
# Off by default: ``sys.addaudithook`` is IRREVERSIBLE for process life and fires
# for every audited event, so a drop-in perf library must not install it without
# consent. When armed, the body early-returns for non-socket events (two string
# compares) and no-ops entirely once ``_hook_active`` is cleared by forget().
#
# Event choice matters for the ignore-own-host guard: by the time
# ``socket.connect`` fires, the stdlib HTTP stack (urllib.request → http.client →
# socket.create_connection) has already resolved the name via getaddrinfo, so the
# connect address is a NUMERIC IP that can never match an ignored hostname (and
# would let Boosthis's own flushes self-count under an IP). The
# ``socket.getaddrinfo`` audit event fires BEFORE resolution and carries the
# ORIGINAL hostname, so it is the primary feed; ``socket.connect`` is kept only
# for the direct ``sock.connect(("hostname", port))`` path (which skips the
# Python-level getaddrinfo event) and deliberately ignores IP-literal addresses
# so one logical attempt is never counted twice. Trade-off: a raw socket
# connecting straight to an IP literal is no longer observed — acceptable, since
# an IP-shaped ``name`` was PII-redacted before upload anyway.
_hook_installed = False
_hook_active = False


def _is_ip_literal(host: str) -> bool:
    """True when ``host`` is a v4/v6 IP literal (brackets tolerated). Never raises."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except Exception:
        return False


def _on_audit_event(event: str, args: tuple) -> None:
    """Body of the opt-in audit hook.

    Module-level (not a closure) so tests can exercise the exact shipped logic
    without installing an irreversible process-wide ``sys.addaudithook``.
    """
    # Cheapest possible fast path: bail on the common non-socket events.
    if event != "socket.getaddrinfo" and event != "socket.connect":
        return
    if not _hook_active or is_boosthis_disabled():
        return
    try:
        if event == "socket.getaddrinfo":
            # args = (host, port, family, type, protocol); host is the ORIGINAL
            # pre-resolution name, so ``ignore_host`` matches here.
            host = args[0] if args else None
            if isinstance(host, bytes):
                host = host.decode("ascii", "ignore")
            if isinstance(host, str) and host:
                record_outbound_attempt(host)
            return
        # socket.connect: args = (socket, address). Skip resolved IP literals —
        # they were already counted (by name) via socket.getaddrinfo, and an IP
        # can never match the ignored hostname.
        addr = args[1] if len(args) > 1 else None
        if isinstance(addr, tuple) and addr and isinstance(addr[0], str):
            if addr[0] and not _is_ip_literal(addr[0]):
                record_outbound_attempt(addr[0])
    except Exception:
        pass  # never let the audit hook disturb the host app


def arm_outbound_audit_hook() -> None:
    """Opt-in: observe outbound name lookups + connects to auto-feed retry-storm.

    Installs the hook at most once per process (idempotent). No-op under the
    kill-switch. The host CLI / integrator calls this deliberately.
    """
    global _hook_installed, _hook_active
    if is_boosthis_disabled():
        return
    _hook_active = True
    if _hook_installed:
        return
    _hook_installed = True
    try:
        import sys

        sys.addaudithook(_on_audit_event)
    except Exception:
        # Some interpreters forbid audit hooks — explicit API still works.
        _hook_installed = False


# ── Opt-in stdlib HTTP observation (repeated-work identity) ─────────────────
# The socket audit hook above learns a bare HOSTNAME, which is not a call
# identity: two connections to one host are usually two different calls. The
# repeated-work meter refuses a hostname for exactly that reason, so without
# this hook a Python app could only ever contribute WRAPPED calls to it, and
# the one case the fan-out rule actually names — "identical downstream GETs
# within one request should share one promise" — would never be observed.
#
# ``http.client.HTTPConnection`` is the stdlib chokepoint under urllib,
# requests and urllib3, so patching its two ends yields method + origin + path
# and a real duration, attributed to the request in scope. Unlike an audit
# hook this is REVERSIBLE (disarm restores the originals), but it is still
# opt-in: a drop-in perf library must not reach into a customer's HTTP stack
# without being asked.
#
# This is ALSO the only place in the kit that learns how an outbound call
# ENDED, so it is what feeds the additive ``network`` meter (duration + one of
# ok / error / timeout / stall — never a host, URL or status). It deliberately
# does NOT go through ``record_outbound_attempt``: that would file a second
# retry-storm attempt for a call the audit-hook feed has already counted.
#
# PRIVACY: the composed target is hashed on this stack by the repeated-work
# meter and dropped. Neither the URL nor the hash ever leaves the process —
# only the count of how many times one identity recurred. Hosts registered via
# ``ignore_host`` (Boosthis's own telemetry endpoint) are skipped so our own
# flushes can never be reported as the app repeating itself.
_http_hook_installed = False
# Set by the PUBLIC disarm and never cleared by the automatic arming path.
# An operator who switches the hook off must stay switched off: the request
# boundary arms this hook, so a non-sticky disarm would be undone by the very
# next request the app served.
_http_hook_opted_out = False
_http_hook_active = False
_http_originals: Dict[str, Any] = {}
# The wrappers we installed, kept so unpatching can tell OUR method from one a
# host library patched on top of ours afterwards. Restoring blindly would tear
# out somebody else's instrumentation.
_http_wrappers: Dict[str, Any] = {}
# Arming replaces the request methods plus HTTP/HTTPS connect and must not
# interleave with itself:
# two first requests arriving together could each capture the other's wrapper
# as "the original", making the patch impossible to undo.
_http_hook_lock = threading.Lock()


def _net_outcome_for_error(exc: BaseException) -> str:
    """Coarse ``network``-meter bucket for an outbound call that RAISED.

    ``"timeout"`` for the timeout class — those feed the axis's silent-drop
    rate, because a call that ran out of patience is the developer's problem,
    not a downstream answer — and ``"error"`` for every other loud failure
    (connection refused, TLS, a dropped socket). Recognised by TYPE first
    (``socket.timeout`` IS the builtin ``TimeoutError`` on every version this
    kit supports), then by class NAME, because clients layered on
    ``http.client`` (urllib3, requests) raise their own read/connect-timeout
    classes that share no base with it. Never raises; never reads a message,
    a host or a URL off the exception.
    """
    try:
        chain = (exc, getattr(exc, "__cause__", None))
        for err in chain:
            if err is None:
                continue
            if isinstance(err, TimeoutError):
                return "timeout"
            if "timeout" in type(err).__name__.lower():
                return "timeout"
    except Exception:  # noqa: BLE001
        pass
    return "error"


def _buffered_header_names(conn: Any) -> list[str]:
    """The lowercased names of the headers already queued on ``conn``.

    ``http.client`` buffers the request line and each ``putheader`` as a byte
    line in ``conn._buffer`` until ``endheaders`` flushes them, so this is the
    only way to see what the CALLER has set before the block goes out. Reads
    names only — no value is decoded, kept or compared. Never raises; a buffer
    it cannot read yields an empty list, which is treated as "the host set
    nothing" by the one caller and simply means the kit attaches its own.
    """
    out: list[str] = []
    try:
        for index, line in enumerate(getattr(conn, "_buffer", ()) or ()):
            if isinstance(line, (bytes, bytearray)):
                text = bytes(line).decode("latin-1", "replace")
            elif isinstance(line, str):
                text = line
            else:
                continue
            # The first line is the request line, not a header. It normally has
            # no colon and falls out below anyway — but through a forward proxy
            # it is absolute-form ("GET http://example.com/ HTTP/1.1"), whose
            # scheme colon would otherwise be read as a header named "get http".
            if index == 0 and " HTTP/" in text:
                continue
            colon = text.find(":")
            if colon > 0:
                out.append(text[:colon].strip().lower())
    except Exception:
        pass
    return out


def _request_line_host(conn: Any) -> str:
    """The host of an ABSOLUTE-FORM request line, or ``""`` for an ordinary one.

    A client speaking through a forward proxy opens the socket to the proxy and
    writes ``GET http://example.com/path HTTP/1.1`` on it. ``conn.host`` is then
    the proxy — usually loopback, usually permitted — while the request is on
    its way to a company we have never judged. The request line is the only
    place the real destination appears, and at ``endheaders`` it is already
    sitting in the buffer we are about to add to.

    Never raises: an unreadable line simply means no proxy was detected.
    """
    try:
        buffer = getattr(conn, "_buffer", None)
        if not buffer:
            return ""
        line = buffer[0]
        if isinstance(line, (bytes, bytearray)):
            line = bytes(line).decode("latin-1", "replace")
        if not isinstance(line, str):
            return ""
        parts = line.split(" ")
        if len(parts) < 2:
            return ""
        target = parts[1]
        lowered = target.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            return ""
        return propagation_host_of(target)
    except Exception:
        return ""


def _destination_host(conn: Any) -> str:
    """WHERE THIS REQUEST IS REALLY GOING, as the propagation rule must judge it.

    Three answers, most specific first:

      1. A CONNECT tunnel. ``conn.host`` is the proxy that opened it;
         ``_tunnel_host`` is the service on the other side, which is the one
         receiving the headers.
      2. An absolute-form request line — a plain-HTTP forward proxy.
      3. The connection's own host, the ordinary case.

    Parsed by the propagation module's own host reader rather than this file's
    ``host_of``. That one splits on the first colon to drop a port, which turns
    the unbracketed address a connection carries — ``fe80::1``,
    ``2001:4860:4860::8888`` — into the single-label names ``fe80`` and
    ``2001``. Single-label names are internal by default, so every IPv6
    destination in the world read as permitted.

    Never raises. A host it cannot read is ``""``, which attaches nothing.
    """
    try:
        tunnel = getattr(conn, "_tunnel_host", None)
        if isinstance(tunnel, str) and tunnel:
            return propagation_host_of(tunnel)
        proxied = _request_line_host(conn)
        if proxied:
            return proxied
        host = getattr(conn, "host", None)
        if not isinstance(host, str) or not host:
            return ""
        return propagation_host_of(host)
    except Exception:
        return ""


def _attach_trace_headers(conn: Any) -> None:
    """FORWARD THIS HOP'S TRACE TO THE NEXT SERVICE.

    The kit is already standing inside the host's outgoing call to measure it.
    Attaching the trace headers here is what makes a two-service install
    produce ONE trace instead of two, with nothing wired by hand at any call
    site.

    Three refusals, in the order they are cheapest to decide: a connection with
    no readable host; Boosthis's own upload endpoint, because our flushes are
    not the customer's next hop and tagging them would put a customer's trace
    id on our own wire for no reason; and a destination the customer's
    configuration does not permit — which, by default, is every destination
    that could be an outside company (see ``trace_propagation.py`` for the rule
    and why the default is what it is).

    CANNOT BREAK THE CALL, AND CANNOT HALF-CHANGE IT. Every value is built and
    checked before the first one is attached, so the loop runs only over
    validated strings — but ``putheader`` is the host object's own method and
    pre-validation makes a raise unlikely, not impossible. A raise on the
    second write would otherwise send the request carrying part of the set, so
    the length of the connection's pending header buffer is noted first and the
    buffer is truncated back to it if the loop does not finish. Whatever
    happens, the request that goes out is either exactly what the host built or
    exactly that plus the whole set, and nothing surfaces: at worst the trace
    stops at this service, as it always used to.
    """
    try:
        bare = _destination_host(conn)
        if not bare or bare in _ignored_hosts:
            return
        items = headers_for_next_hop(bare, _buffered_header_names(conn))
        if not items:
            return
        # ALL OF THEM OR NONE. ``putheader`` appends one entry per header to
        # the connection's pending buffer, so its length before the loop is the
        # whole undo. A connection that keeps its buffer somewhere else gets no
        # undo, and what is left on it is a strict PREFIX of our own set — the
        # trace id, possibly the elapsed offset — which the inbound half reads
        # as an ordinary hop with no parent. Degraded, never malformed.
        buffer = getattr(conn, "_buffer", None)
        mark = len(buffer) if isinstance(buffer, list) else None
        try:
            for name, value in items:
                conn.putheader(name, value)
        except Exception:
            if mark is not None and len(buffer) > mark:
                del buffer[mark:]
            raise
    except Exception:
        pass


def _rw_target(conn: Any, method: Any, url: Any) -> Optional[str]:
    """Compose ``METHOD scheme://host[:port]path`` for one connection, or None.

    Never raises — a target we cannot compose is simply not observed.
    """
    try:
        import http.client as _hc

        host = getattr(conn, "host", None)
        if not isinstance(host, str) or not host:
            return None
        # Normalise before comparing: ``ignore_host`` stores the lowercased
        # bare host, while a connection's ``host`` keeps the caller's casing.
        if host_of(host) in _ignored_hosts:
            return None
        https = getattr(_hc, "HTTPSConnection", None)
        secure = bool(https) and isinstance(conn, https)
        scheme = "https" if secure else "http"
        port = getattr(conn, "port", None)
        default = 443 if secure else 80
        origin = f"{scheme}://{host}" if port in (None, default) else f"{scheme}://{host}:{port}"
        path = url if isinstance(url, str) and url else "/"
        return f"{str(method).upper()} {origin}{path}"
    except Exception:
        return None

def outbound_target_from_url(method: Any, url: Any) -> Optional[str]:
    """Compose ``METHOD scheme://host[:port]path[?query]`` from a FULL URL, or
    None when there is nothing to attribute the call to.

    The companion of :func:`_rw_target` for clients that hand the kit a whole
    URL instead of a connection object (``httpx``, ``aiohttp``). Same rules, so
    the same call reads the same however it was made: default ports are
    dropped, and a host registered via :func:`ignore_host` — Boosthis's own
    endpoint — returns None so our uploads are never the app's traffic.

    Userinfo is dropped by construction: ``urlsplit().hostname`` is the bare
    host, so a credential embedded in a URL never reaches even the in-process
    hash. Never raises — a target we cannot compose is simply not observed.
    """
    try:
        parts = urlsplit(str(url))
        host = parts.hostname
        if not host:
            return None
        if host_of(host) in _ignored_hosts:
            return None
        scheme = (parts.scheme or "http").lower()
        default = 443 if scheme == "https" else 80
        try:
            port = parts.port
        except ValueError:
            port = None
        # An IPv6 literal has to keep its brackets or the origin is unreadable.
        shown = f"[{host}]" if ":" in host else host
        origin = (
            f"{scheme}://{shown}"
            if port in (None, default)
            else f"{scheme}://{shown}:{port}"
        )
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return f"{str(method).upper()} {origin}{path}"
    except Exception:
        return None
def _auto_arm_outbound_http_hook() -> None:
    """Arm at a request boundary — unless the app has opted out.

    The request boundary is what arms this hook in a real app, so it must go
    through here and NOT through the public :func:`arm_outbound_http_hook`.
    Otherwise ``disarm_outbound_http_hook()`` would last exactly until the
    next request, which is no opt-out at all.
    """
    if _http_hook_opted_out:
        return
    arm_outbound_http_hook()


def arm_outbound_http_hook() -> None:
    """Opt-in: watch this app's outbound HTTP calls.

    Covers Python's stdlib ``http.client`` chokepoint — which ``urllib``,
    ``requests`` and ``urllib3`` all go through — AND the two popular modern
    clients that bypass it and open their own sockets, ``httpx`` and
    ``aiohttp`` (see :mod:`boosthis.outbound_clients`). One lever arms them
    all, and :func:`disarm_outbound_http_hook` switches them all back off:
    outbound observation is a single thing to a developer, so it must be a
    single switch.

    Feeds the repeated-work meter and the ``network`` meter — the retry-storm
    detector keeps its own feeds, so arming this never double-counts an
    attempt. Idempotent, reversible, and a no-op under the kill-switch. Never
    raises.
    """
    global _http_hook_active, _http_hook_opted_out
    # An explicit call is a deliberate opt-IN, so it undoes an earlier opt-out.
    _http_hook_opted_out = False
    if is_boosthis_disabled():
        return
    _http_hook_active = True
    # Armed on EVERY call, before the stdlib short-circuit below: a client the
    # app imports lazily (the first time a handler actually calls out) is not
    # in sys.modules when the first request arms this, and would otherwise stay
    # unwatched for the life of the process. The re-arm is a dict lookup per
    # already-watched library.
    try:
        from boosthis.outbound_clients import arm_client_hooks

        arm_client_hooks()
    except Exception:
        pass  # a client we cannot watch is reported, never raised
    if _http_hook_installed:
        return
    # Warm the imports the installer needs BEFORE taking the lock. Import
    # machinery is host-controlled (a custom importer, a slow finder), and no
    # host code may run while an app's request thread is waiting on us.
    try:
        import http.client  # noqa: F401
        import boosthis.meter_axes  # noqa: F401
        import boosthis.repeated_work  # noqa: F401
        import boosthis.db_work  # noqa: F401
    except Exception:
        pass
    # The request boundary arms this hook, so two first requests arriving
    # together both reach this line. Installing twice would capture OUR OWN
    # wrapper as "the original": every call counted twice, and a patch that can
    # never be undone. One installer at a time; the loser leaves IMMEDIATELY —
    # the winner is already installing, and the loser's very next request sees
    # the installed flag and skips this whole path. Waiting here would put our
    # bookkeeping in front of the app's request, which is never worth it.
    try:
        acquired = _http_hook_lock.acquire(timeout=0.2)
    except Exception:
        return
    if not acquired:
        return
    try:
        _install_http_hook_locked()
    finally:
        try:
            _http_hook_lock.release()
        except Exception:
            pass


def _install_http_hook_locked() -> None:
    """Patch the stdlib HTTP methods. The caller holds ``_http_hook_lock``."""
    global _http_hook_installed
    if _http_hook_installed:
        return
    try:
        import http.client as _hc
        from time import perf_counter

        from boosthis.meter_axes import NETWORK_STALL_MS, record_network_outcome
        from boosthis.repeated_work import note_outbound_call
        from boosthis.dependency_work import note_dependency_call
        from boosthis.db_work import note_hosted_db_call

        conn_cls = _hc.HTTPConnection
        real_put = conn_cls.putrequest
        real_get = conn_cls.getresponse
        real_close = conn_cls.close
        real_endheaders = conn_cls.endheaders
        https_cls = getattr(_hc, "HTTPSConnection", None)
        real_connect = conn_cls.connect
        real_https_connect = getattr(https_cls, "connect", None) if https_cls else None
        if any(
            getattr(fn, "_boosthis_http_hook", False)
            for fn in (real_put, real_get, real_close, real_endheaders, real_connect, real_https_connect)
            if fn is not None
        ):
            # Already ours, from an arming whose bookkeeping was lost (a module
            # reload, an erasure that could not restore). Wrapping our own
            # wrapper would count every call twice and could never be undone.
            _http_hook_installed = True
            return
        _http_originals["putrequest"] = real_put
        _http_originals["getresponse"] = real_get
        _http_originals["close"] = real_close
        _http_originals["endheaders"] = real_endheaders
        _http_originals["connect"] = real_connect
        if real_https_connect is not None:
            _http_originals["https_connect"] = real_https_connect

        def connect(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            started = perf_counter()
            try:
                return real_connect(self, *args, **kwargs)
            finally:
                try:
                    # HTTPSConnection.connect invokes HTTPConnection.connect
                    # internally; its outer wrapper owns TCP+TLS together.
                    if (
                        _http_hook_active
                        and not is_boosthis_disabled()
                        and not getattr(self, "_boosthis_secure_connect", False)
                    ):
                        from boosthis.dependency_distance import record_connection_for_host
                        record_connection_for_host(
                            getattr(self, "host", None),
                            (perf_counter() - started) * 1000,
                        )
                        self._boosthis_connection_opened = True
                except Exception:
                    pass

        def https_connect(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            started = perf_counter()
            try:
                self._boosthis_secure_connect = True
                return real_https_connect(self, *args, **kwargs)
            finally:
                try:
                    self._boosthis_secure_connect = False
                    if _http_hook_active and not is_boosthis_disabled():
                        from boosthis.dependency_distance import record_connection_for_host
                        record_connection_for_host(
                            getattr(self, "host", None),
                            (perf_counter() - started) * 1000,
                        )
                        self._boosthis_connection_opened = True
                except Exception:
                    pass

        def putrequest(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
            # Observe FIRST-fail-safe: the host's call must run even if we throw.
            try:
                if _http_hook_active and not is_boosthis_disabled():
                    target = _rw_target(self, method, url)
                    # The database scope is captured HERE, on the issuing
                    # stack. A keep-alive connection answers on a socket opened
                    # during an EARLIER request, so reading the ambient scope
                    # when the response lands would credit the query to a
                    # request that already ended.
                    self._boosthis_rw = (
                        (target, perf_counter(), _db_scope_now()) if target else None
                    )
                else:
                    self._boosthis_rw = None
            except Exception:
                pass
            return real_put(self, method, url, *args, **kwargs)

        def getresponse(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            # The response IS the end of the call — and so is a raised timeout
            # or a dropped connection. A failed call still HAPPENED: three
            # identical GETs that all time out are three identical GETs, and
            # they are usually the most expensive repeat an app can make. So
            # the observation is filed in a ``finally``, which also guarantees
            # the pending marker is cleared before this connection can be
            # reused (a stale marker would misdate the next call on it).
            # The host's own exception passes through untouched.
            outcome = "ok"
            # WHY the flag: for an answer that ends the connection (HTTP/1.0, or
            # "Connection: close"), CPython calls ``self.close()`` from INSIDE
            # this method, before it returns. That reaches the wrapper below
            # while the marker is still pending — so a perfectly ordinary
            # SUCCESS was being filed as a failed call. Every such response was
            # reported as an error with no duration. The flag says "a response
            # is being read on this connection; its outcome is mine to file".
            try:
                self._boosthis_rw_reading = True
            except Exception:
                pass
            response = None
            try:
                response = real_get(self, *args, **kwargs)
                try:
                    from boosthis.request_error_timer_meters import note_upstream_cache
                    note_upstream_cache(response.getheaders())
                except Exception:
                    pass
                return response
            except BaseException as exc:  # noqa: BLE001
                # Bucket the failure for the network meter (from its TYPE, never
                # its message), then re-raise the host's own exception unchanged.
                outcome = _net_outcome_for_error(exc)
                raise
            finally:
                try:
                    pending = getattr(self, "_boosthis_rw", None)
                    self._boosthis_rw = None
                    # Hand the connection back. A keep-alive connection is
                    # REUSED, and a flag left standing would make the wrapper
                    # below stand aside for the rest of its life — the next
                    # call on it that died before its answer would go
                    # uncounted. Cleared after the marker above is taken, so
                    # the close inside this method still finds it set.
                    self._boosthis_rw_reading = False
                    if pending and pending[0]:
                        elapsed_ms = (perf_counter() - pending[1]) * 1000.0
                        # A hosted database is DATABASE work: it is filed into
                        # the request that issued it and dropped by the two
                        # meters below, which refuse it at their own doors.
                        # The network meter still sees it — that measures the
                        # wire, which is the same wire.
                        note_hosted_db_call(_pending_db_scope(pending),
                                            pending[0], elapsed_ms)
                        note_outbound_call(pending[0], elapsed_ms)
                        note_dependency_call(
                            pending[0],
                            elapsed_ms,
                            {"ok": "ok", "error": "failed", "timeout": "timedout",
                             "stall": "failed"}.get(outcome, "failed"),
                        )
                        # Network meter: EVERY call that ended banks its
                        # duration, whatever the outcome. Discarding a
                        # failure's elapsed time made a call that failed five
                        # times and then worked report a single measurement —
                        # the success, in isolation, with nothing to show what
                        # getting there cost. A retried call's real cost is all
                        # of its attempts. Mirrors the Node sampler and every
                        # other kit, all of which bank a failed call's
                        # duration; the failure is separately visible in its
                        # own bucket, so a fast refusal is never mistaken for a
                        # healthy call.
                        record_network_outcome(elapsed_ms, outcome)
                        try:
                            from boosthis.request_error_timer_meters import (
                                note_outbound_connection,
                            )
                            opened = bool(
                                getattr(self, "_boosthis_connection_opened", False)
                            )
                            self._boosthis_connection_opened = False
                            note_outbound_connection(outcome == "ok", opened)
                        except Exception:
                            pass
                except Exception:
                    pass

        def close(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            # A call can die BEFORE ``getresponse()`` is ever reached — the send
            # itself breaks on a dropped connection, and some clients abandon a
            # request without asking for a response. That call still happened
            # and still cost the app time, so a marker left pending when the
            # connection closes is filed here. ``getresponse`` clears the
            # marker, so an ordinary call is never counted twice.
            #
            # …but a close that comes from INSIDE ``getresponse()`` is not a
            # death at all: CPython closes the socket itself as soon as the
            # answer says the connection will not be reused (HTTP/1.0, or
            # "Connection: close"). Filing here would report those perfectly
            # good responses as failures — every one of them, on servers that
            # close by default. The reader owns the outcome; stand aside.
            reading = False
            try:
                reading = bool(getattr(self, "_boosthis_rw_reading", False))
            except Exception:
                pass
            try:
                pending = None if reading else getattr(self, "_boosthis_rw", None)
                if not reading:
                    self._boosthis_rw = None
                if pending and pending[0]:
                    elapsed_ms = (perf_counter() - pending[1]) * 1000.0
                    note_hosted_db_call(_pending_db_scope(pending),
                                        pending[0], elapsed_ms)
                    note_outbound_call(pending[0], elapsed_ms)
                    # No answer ever arrived. Node treats this as a failed
                    # dependency call; elapsed time still records what it cost.
                    note_dependency_call(pending[0], elapsed_ms, "failed")
                    # No answer was ever read on this connection. Waited past
                    # the stall horizon, that is the silent-drop class the
                    # network axis exists for; sooner, it is a loud failure —
                    # a send that broke, or a client that gave up early. Either
                    # way the call ENDED here and its elapsed time is a real
                    # measurement of what the app waited, so it is banked like
                    # any other. (Node's own stall bucket has no duration to
                    # bank: there, a stall is a horizon expiring with no end
                    # event at all, not a connection closing.)
                    record_network_outcome(
                        elapsed_ms,
                        "stall" if elapsed_ms >= NETWORK_STALL_MS else "error",
                    )
            except Exception:
                pass
            return real_close(self, *args, **kwargs)

        def endheaders(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            # TRACE PROPAGATION. The last moment the header block is still ours
            # to add to, and the FIRST at which the caller's own headers are all
            # present — which is why the hook is here and not beside the timing
            # one in ``putrequest``. At ``putrequest`` the caller has not set
            # its headers yet, so a developer who already forwards the trace by
            # hand would get a second copy of it on the wire; here they are
            # visible in the buffer and are left exactly as they are.
            #
            # Observe-first-fail-safe like every wrapper above: the host's call
            # runs whatever happens in here, and a failure leaves the request
            # byte-for-byte as the host built it.
            try:
                if _http_hook_active and not is_boosthis_disabled():
                    _attach_trace_headers(self)
            except Exception:
                pass
            return real_endheaders(self, *args, **kwargs)

        connect._boosthis_http_hook = True  # type: ignore[attr-defined]
        if real_https_connect is not None:
            https_connect._boosthis_http_hook = True  # type: ignore[attr-defined]
        for fn in (putrequest, getresponse, close, endheaders):
            # A wrapper says so about itself, so a later arming can recognise
            # our own work even if the module-level bookkeeping was lost.
            fn._boosthis_http_hook = True  # type: ignore[attr-defined]
        conn_cls.putrequest = putrequest  # type: ignore[method-assign]
        conn_cls.getresponse = getresponse  # type: ignore[method-assign]
        conn_cls.close = close  # type: ignore[method-assign]
        conn_cls.endheaders = endheaders  # type: ignore[method-assign]
        conn_cls.connect = connect  # type: ignore[method-assign]
        _http_wrappers["connect"] = connect
        if https_cls is not None and real_https_connect is not None:
            https_cls.connect = https_connect  # type: ignore[method-assign]
            _http_wrappers["https_connect"] = https_connect
        _http_wrappers["endheaders"] = endheaders
        _http_wrappers["putrequest"] = putrequest
        _http_wrappers["getresponse"] = getresponse
        _http_wrappers["close"] = close
        _http_hook_installed = True
    except Exception:
        # A locked-down or non-CPython stack simply goes unobserved; the
        # explicit API and the wrapper feed still work.
        _http_hook_installed = False


def _unpatch_http_hook() -> None:
    """Restore the stdlib HTTP methods this kit patched.

    Undoes the patch WITHOUT recording an opt-out, so erasure (``forget()``)
    leaves a running kit free to observe again. The public opt-out is
    :func:`disarm_outbound_http_hook`.

    ALL OR NOTHING. If any one of the four methods is no longer the wrapper we
    installed, a host library has patched on top of us since — and putting back
    what we captured at arming time would silently delete THEIR work. In that
    case our wrappers stay exactly where they are, inert (the hook is inactive
    from the line above), and a later arming switches them back on instead of
    stacking a second layer.
    """
    global _http_hook_installed, _http_hook_active
    _http_hook_active = False
    # httpx/aiohttp first, and unconditionally: they can be watched even when
    # the stdlib patch never took, so an early return below must not strand
    # them still patched and still feeding.
    try:
        from boosthis.outbound_clients import unpatch_client_hooks

        unpatch_client_hooks()
    except Exception:
        pass
    if not _http_hook_installed:
        return
    try:
        acquired = _http_hook_lock.acquire(timeout=5.0)
    except Exception:
        return
    if not acquired:
        return
    try:
        import http.client as _hc

        names = ("putrequest", "getresponse", "close", "endheaders", "connect")
        ours_on_top = all(
            _http_wrappers.get(name) is not None
            and getattr(_hc.HTTPConnection, name, None) is _http_wrappers[name]
            and name in _http_originals
            for name in names
        )
        https_wrapper = _http_wrappers.get("https_connect")
        https_original = _http_originals.get("https_connect")
        https_cls = getattr(_hc, "HTTPSConnection", None)
        https_ok = (
            https_wrapper is None
            or (https_cls is not None and getattr(https_cls, "connect", None) is https_wrapper
                and https_original is not None)
        )
        if ours_on_top and https_ok:
            for name in names:
                setattr(_hc.HTTPConnection, name, _http_originals[name])
            if https_wrapper is not None:
                setattr(https_cls, "connect", https_original)
            _http_originals.clear()
            _http_wrappers.clear()
            _http_hook_installed = False
    except Exception:
        pass
    finally:
        try:
            _http_hook_lock.release()
        except Exception:
            pass


def disarm_outbound_http_hook() -> None:
    """Stop watching outbound HTTP, for good. Never raises.

    Restores the stdlib methods AND every httpx/aiohttp method this kit
    replaced, then records the opt-out, so the next request the app serves does
    not quietly re-arm anything. Only an explicit call to
    :func:`arm_outbound_http_hook` turns it back on.
    """
    global _http_hook_opted_out
    _http_hook_opted_out = True
    _unpatch_http_hook()


# ── Public collector + lifecycle ────────────────────────────────────────────
def collect_detector_findings() -> List[Dict[str, Any]]:
    """Drain the current live-detector findings. [] under the kill-switch."""
    if is_boosthis_disabled():
        return []
    out = (
        _collect_retry_storms()
        + _collect_concurrent_clusters()
        + _collect_idle_burn()
    )
    try:
        from .ai_calls import collect_ai_findings
        out += collect_ai_findings()
    except Exception:  # noqa: BLE001
        pass
    return out


def clear_detectors() -> None:
    """Wipe all detector state (wired into ``forget()``)."""
    global _in_flight, _idle_since_wall, _idle_since_cpu
    global _idle_burn_count, _worst_idle_active_ms, _hook_active
    global _retry_burst_peak, _http_hook_opted_out, _outbound_attempts_seen
    _attempts_by_host.clear()
    _ignored_hosts.clear()
    _retry_burst_peak = 0
    _outbound_attempts_seen = 0
    _in_flight = 0
    _idle_since_wall = 0.0
    _idle_since_cpu = 0.0
    _idle_burn_count = 0
    _worst_idle_active_ms = 0
    _hook_active = False  # hook (if installed) stays but no-ops
    # Erasure, not an opt-out: unpatch and forget the data, but leave a
    # running kit free to observe again on the next request. A deliberate
    # opt-out goes through disarm_outbound_http_hook() and survives this.
    _http_hook_opted_out = False
    _unpatch_http_hook()
    # Same for the httpx/aiohttp observation: drop the patches AND the record
    # of having ever armed, so the honesty report goes quiet again instead of
    # reporting a blind spot the kit is no longer claiming to cover.
    try:
        from .outbound_clients import clear_outbound_clients

        clear_outbound_clients()
    except Exception:  # noqa: BLE001
        pass
    try:
        from .ai_calls import clear_ai_calls
        clear_ai_calls()
    except Exception:  # noqa: BLE001
        pass
    # Wipe the host-suspend sensor's state too (its listener wiring survives —
    # module wiring, not data). Guarded so forget() never fails over it.
    try:
        from .suspend_sensor import clear_suspend_sensor

        clear_suspend_sensor()
    except Exception:  # noqa: BLE001
        pass


def _mark_idle_burn_for_tests(active_ms: float) -> None:
    """@internal test hook."""
    _mark_idle_burn(active_ms)
