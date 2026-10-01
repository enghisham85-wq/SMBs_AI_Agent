"""Live per-install read helpers (Python sibling of ``liveData.ts``).

The READ side of the two-way channel: it lets the ``boosthis.snapshot`` and
``boosthis.crash_risk`` MCP tools return an install's REAL uploaded picture
(full perf snapshot + the "what is likely to crash my app" risk feed) instead of
the static on-device note — but ONLY when the process has been handed read
credentials for one specific install.

CREDENTIAL RESOLUTION (mirrors RN's inject-not-read contract, adapted to the
single-tenant Python stdio server): env ``BOOSTHIS_INSTALL_ID`` +
``BOOSTHIS_READ_TOKEN`` win; otherwise fall back to the persisted
``~/.boosthis/config.json`` ``install_id`` + ``read_token``. The read token is
the strictly read-only credential the developer copies into their AI — never the
delete token. With no creds every reader returns ``None`` and the tools serve the
on-device note.

FAIL-OPEN + FAIL-SOFT (like ``community.py``): any failure — not configured,
kill-switched, offline, non-200, bad JSON, oversized, PII trip — yields ``None``
so the caller serves the note; nothing ever raises. Returned rows are re-run
through the PII guard (defense in depth) and any row that trips it is dropped
rather than thrown: the data is only code-defined labels + integer timings that
already passed the upload guard, but a false-positive must never brick the tool.
The final whole-response guard pass deliberately EXCLUDES our own shipped
constants (the ``note`` prose and the rule-id pointers), which are not server
data and whose field name/text would otherwise trip the guard.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from boosthis.pii import check_no_pii
from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.symptom_hints import nudge_fields
from boosthis.thresholds import SCORE_THRESHOLDS

DEFAULT_ENDPOINT = "https://www.boosthis.com/api"

# ~2MB belt-and-braces cap on any single read (matches the server-side caps).
_MAX_READ_BYTES = 2_000_000
_TIMEOUT_S = 5.0

NOTE_SNAPSHOT = (
    "Live full snapshot read from your Boosthis server: the whole meter page "
    "your app last uploaded (per-route rows, cross-cutting findings, and the "
    "responsiveness/budget axes). Numbers + code-defined route labels only — "
    "never user values or PII."
)

NOTE_SNAPSHOT_UNAVAILABLE = (
    "No live snapshot available. This tool reads your app's OWN uploaded "
    "snapshot from the Boosthis server, which needs read credentials "
    "(BOOSTHIS_INSTALL_ID + BOOSTHIS_READ_TOKEN, or a ~/.boosthis/config.json "
    "with a read_token from enable_telemetry). Without them, use the on-device "
    "tools (session_summary, recent_samples, budgets)."
)

NOTE_CRASH_RISK = (
    "Potential-crashes risk feed read from your Boosthis server: the crash "
    "classes your app has ALREADY recorded (uncaught exceptions on the main "
    "thread or a worker thread, and caught render near-misses), newest-first, "
    "joined with the event-loop (ANR-style) stability summary from your latest "
    "snapshot. Each crash class carries relatedRules — fetch them with "
    "boosthis.get_rule for the fix. Signatures are code-derived (error name + "
    "redacted frame), never the raw error message, so no user value is exposed."
)

NOTE_CRASH_RISK_UNAVAILABLE = (
    "No live crash-risk feed available. This tool reads your app's OWN recorded "
    "crash classes from the Boosthis server, which needs read credentials "
    "(BOOSTHIS_INSTALL_ID + BOOSTHIS_READ_TOKEN, or a ~/.boosthis/config.json "
    "with a read_token from enable_telemetry). Without them, use "
    "boosthis.list_rules to review the crash-prevention rules."
)

# Static, shipped map from a crash KIND to the Python-pack rule ids most likely
# to prevent it. Deterministic (no server round-trip): the matcher/rule book
# holds the fix text; this only points the AI at the right rules for each crash
# class. These are the nearest Python-pack analogs to RN's crash-prevention rules
# (RN's remote-null-field-read / unsafe-json-response-parse / navigation-param-
# guard have no direct Python equivalent, so we point at the closest crash-
# adjacent rules: resource-exhaustion / OOM leaks for uncaught + render, and
# swallowed-async-error rules for the async analog of an unhandled rejection).
DEFAULT_CRASH_RULES = [
    "read-whole-file-into-memory",
    "unbounded-queryset-no-pagination",
    "asyncio-unbounded-gather-fanout",
    "unclosed-file-or-socket-leak",
]
CRASH_KIND_RULES: dict[str, list[str]] = {
    "uncaught": DEFAULT_CRASH_RULES,
    # No native Python "unhandled rejection" — asyncio is the nearest analog, so
    # point at the rules for exceptions swallowed/lost inside asyncio.
    "unhandledRejection": [
        "asyncio-gather-without-return-exceptions",
        "asyncio-create-task-no-reference",
        "sequential-awaits-not-gathered",
    ],
    "render": [
        "read-whole-file-into-memory",
        "module-level-accumulation-leak",
        "unbounded-lru-cache",
    ],
}

# Rules that address event-loop hangs / ANR-style stability (the snapshot's
# stability axis), independent of any single crash class.
STABILITY_RULES = ["fastapi-sync-route-blocks-event-loop", "gil-bound-cpu-in-asyncio"]

NOTE_FULL_STACK_TRACE = (
    "Latest full-stack trace read from your Boosthis server: one user action "
    "stitched across the layers this install recorded, as a waterfall of spans "
    "(layer, code-defined route label, duration, start offset, rating) plus an "
    "honest full-stack score rated against the shared TTI thresholds. Each "
    "span's slowestLayerRules point at the rules most likely to fix that "
    "layer — fetch them with boosthis.get_rule. NOTE: a per-install read token "
    "is self-scoped, so this shows only THIS install's own spans; pass an "
    "account_token on the hosted MCP to unlock the full stitched "
    "RN \u2192 Node \u2192 Python waterfall across all your apps."
)

NOTE_FULL_STACK_TRACE_UNAVAILABLE = (
    "No full-stack trace available. This tool reads your app's OWN recorded "
    "trace spans from the Boosthis server, which needs read credentials "
    "(BOOSTHIS_INSTALL_ID + BOOSTHIS_READ_TOKEN, or a ~/.boosthis/config.json "
    "with a read_token from enable_telemetry) — and the app must have recorded "
    "at least one traced request. Without them, use boosthis.session_summary "
    "for per-route timings."
)

# Static, shipped map from a span's LAYER to the rule ids most likely to fix a
# slow span in that layer. Deterministic (no server round-trip): the matcher /
# rule book holds the fix text; this only points the AI at the right rules.
# Wire-identical to the RN/Node runtimes' LAYER_RULES.
LAYER_RULES: dict[str, list[str]] = {
    "rn": [
        "per-item-fetch-waterfall",
        "client-refetch-no-cache",
        "screen-load-budget-500ms-p75",
        "oversized-thumbnail-fetch",
    ],
    "node": [
        "sync-fs-in-handler",
        "await-in-loop",
        "n-plus-one-orm-node",
        "no-keepalive-outbound",
    ],
    "py": [
        "n-plus-one-orm-query",
        "sync-io-in-async-handler",
        "fastapi-sync-route-blocks-event-loop",
        "db-statement-timeout-missing",
    ],
}


def _num(v: Any) -> int | float:
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return v
    return 0


def resolve_creds(override: dict[str, Any] | None = None) -> dict[str, str] | None:
    """Resolve read credentials for ONE call. Returns ``{install_id, read_token,
    base_url}`` or ``None`` when either the install id or read token is missing.

    An explicit per-call ``override`` wins (so a caller can read one install
    without touching env/config); otherwise env ``BOOSTHIS_INSTALL_ID`` +
    ``BOOSTHIS_READ_TOKEN`` win, falling back to the persisted telemetry config's
    ``install_id`` + ``read_token``. Never raises."""
    try:
        if override is not None:
            install_id = str(override.get("install_id") or "").strip()
            token = str(
                override.get("read_token") or override.get("token") or ""
            ).strip()
            base = str(override.get("base_url") or "").strip() or DEFAULT_ENDPOINT
            if not install_id or not token:
                return None
            return {
                "install_id": install_id,
                "read_token": token,
                "base_url": base.rstrip("/"),
            }

        install_id = (os.environ.get("BOOSTHIS_INSTALL_ID") or "").strip()
        token = (os.environ.get("BOOSTHIS_READ_TOKEN") or "").strip()
        base = (
            os.environ.get("BOOSTHIS_INGEST_URL")
            or os.environ.get("BOOSTEN_INGEST_URL")
            or ""
        ).strip()
        if not install_id or not token or not base:
            # Lazy import to avoid an import cycle (telemetry imports the crash
            # reporter, which is independent of this reader).
            from boosthis import telemetry

            cfg = telemetry.get_config()
            if cfg is not None:
                install_id = install_id or (cfg.install_id or "")
                token = token or (cfg.read_token or "")
                base = base or (cfg.endpoint or "")
        base = base or DEFAULT_ENDPOINT
        if not install_id or not token:
            return None
        return {
            "install_id": install_id.strip(),
            "read_token": token.strip(),
            "base_url": base.rstrip("/"),
        }
    except Exception:  # noqa: BLE001
        return None


def _fetch_json(path: str, creds: dict[str, str]) -> Any | None:
    """GET a JSON body from the configured server with the install read token.
    Never raises; returns ``None`` on any failure. Honors ``BOOSTHIS_DISABLED``."""
    if is_boosthis_disabled():
        return None
    try:
        url = f"{creds['base_url']}{path}"
        req = urllib.request.Request(
            url,
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {creds['read_token']}",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            body = resp.read(_MAX_READ_BYTES + 1)
            if len(body) > _MAX_READ_BYTES:
                return None
            data = json.loads(body)
            if isinstance(data, (dict, list)):
                return data
            return None
    except Exception:  # noqa: BLE001
        return None


def _pii_safe(row: Any) -> bool:
    """Defense in depth: keep a mapped row only if it passes the PII guard."""
    return check_no_pii(row) is None


def get_live_snapshot(override: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The whole latest perf snapshot for one install (every meter the in-app
    dashboard shows). Fail-open: any failure (not configured, offline, non-200,
    no snapshot yet, PII trip) returns ``None`` so the caller serves the note.
    The snapshot already passed the upload PII guard; we re-check here (defense in
    depth) and drop it rather than throw."""
    cfg = resolve_creds(override)
    if cfg is None:
        return None
    snap = _fetch_json(
        f"/snapshot?installId={urllib.parse.quote(cfg['install_id'])}", cfg
    )
    if not isinstance(snap, dict):
        return None
    if not _pii_safe(snap):
        return None
    return {
        "available": True,
        "source": "boosthis-server",
        "snapshot": snap,
        "note": NOTE_SNAPSHOT,
    }


def get_live_crash_risk(
    override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The install's already-recorded crash classes + stability summary, with
    static rule-id pointers. Fail-open: any failure (not configured, offline,
    non-200, bad JSON, PII trip) returns ``None`` so the caller serves the note.
    Never mutates module state."""
    cfg = resolve_creds(override)
    if cfg is None:
        return None
    data = _fetch_json(
        f"/crashes/risk?installId={urllib.parse.quote(cfg['install_id'])}", cfg
    )
    if not isinstance(data, dict):
        return None

    crashes_in = data.get("crashes")
    crashes_in = crashes_in if isinstance(crashes_in, list) else []
    crashes: list[dict[str, Any]] = []
    for c in crashes_in:
        if not isinstance(c, dict):
            continue
        kind = c.get("kind") if isinstance(c.get("kind"), str) else "uncaught"
        row = {
            "signature": c.get("signature") if isinstance(c.get("signature"), str) else "",
            "errorName": c.get("errorName") if isinstance(c.get("errorName"), str) else "",
            "kind": kind,
            "redactedFrame": c.get("redactedFrame")
            if isinstance(c.get("redactedFrame"), str)
            else "",
            "countBucket": c.get("countBucket")
            if isinstance(c.get("countBucket"), str)
            else "",
            "occurrences": _num(c.get("occurrences")),
            "firstSeenAt": c.get("firstSeenAt")
            if isinstance(c.get("firstSeenAt"), str)
            else None,
            "lastSeenAt": c.get("lastSeenAt")
            if isinstance(c.get("lastSeenAt"), str)
            else None,
            "relatedRules": CRASH_KIND_RULES.get(kind, DEFAULT_CRASH_RULES),
        }
        # Per-row PII re-check (defense in depth) — drop, never throw.
        if _pii_safe(row):
            crashes.append(row)

    stability_raw = data.get("stability")
    stability = (
        stability_raw
        if isinstance(stability_raw, dict) and _pii_safe(stability_raw)
        else None
    )

    # Defense in depth over the SERVER-SUPPLIED data only. note/source/the rule-id
    # pointers are our own shipped constants, not server data — and the field name
    # "note" itself trips the guard — so the final check excludes them.
    if not _pii_safe({"crashes": crashes, "stability": stability}):
        return None

    return {
        "available": True,
        "source": "boosthis-server",
        "crashClasses": _num(data.get("crashClasses")),
        "totalOccurrences": _num(data.get("totalOccurrences")),
        "crashes": crashes,
        "stability": stability,
        "stabilityRules": STABILITY_RULES,
        "note": NOTE_CRASH_RISK,
    }


def _map_trace_span(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "layer": s.get("layer") if isinstance(s.get("layer"), str) else "",
        "routeLabel": s.get("routeLabel")
        if isinstance(s.get("routeLabel"), str)
        else "",
        "durationMs": _num(s.get("durationMs")),
        "startOffsetMs": _num(s.get("startOffsetMs")),
        "rating": s.get("rating") if isinstance(s.get("rating"), str) else "",
        "isRoot": s.get("isRoot") is True,
    }


def get_live_full_stack_trace(
    override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The install's own latest full-stack trace waterfall, with rule pointers
    for the slowest span's layer. Fail-open: any failure (not configured,
    offline, non-200 / no trace yet, bad JSON, PII trip) returns ``None`` so
    the caller serves the note. Never mutates module state."""
    cfg = resolve_creds(override)
    if cfg is None:
        return None
    data = _fetch_json(
        f"/traces/latest?installId={urllib.parse.quote(cfg['install_id'])}", cfg
    )
    if not isinstance(data, dict):
        return None

    spans_in = data.get("spans")
    spans_in = spans_in if isinstance(spans_in, list) else []
    spans: list[dict[str, Any]] = []
    for s in spans_in:
        if not isinstance(s, dict):
            continue
        row = _map_trace_span(s)
        # Per-row PII re-check (defense in depth) — drop, never throw.
        if _pii_safe(row):
            spans.append(row)

    slowest_raw = data.get("slowest")
    slowest = None
    if isinstance(slowest_raw, dict):
        mapped = _map_trace_span(slowest_raw)
        if _pii_safe(mapped):
            slowest = mapped

    layers_raw = data.get("layers")
    layers = [
        item
        for item in (layers_raw if isinstance(layers_raw, list) else [])
        if isinstance(item, str)
    ]

    trace_id = data.get("traceId") if isinstance(data.get("traceId"), str) else ""
    summary = data.get("summary") if isinstance(data.get("summary"), str) else ""

    # Defense in depth over the SERVER-SUPPLIED data only. note/source/the rule-id
    # pointers are our own shipped constants, not server data — and the field name
    # "note" itself trips the guard — so the final check excludes them.
    if not _pii_safe(
        {
            "traceId": trace_id,
            "layers": layers,
            "summary": summary,
            "slowest": slowest,
            "spans": spans,
        }
    ):
        return None

    return {
        "available": True,
        "source": "boosthis-server",
        "traceId": trace_id,
        "spanCount": _num(data.get("spanCount")),
        "layers": layers,
        "rootLayer": data.get("rootLayer")
        if isinstance(data.get("rootLayer"), str)
        else None,
        "rootDurationMs": _num(data.get("rootDurationMs")),
        "score": _num(data.get("score")),
        "scoreRating": data.get("scoreRating")
        if isinstance(data.get("scoreRating"), str)
        else None,
        "summary": summary,
        "slowest": slowest,
        "slowestLayerRules": LAYER_RULES.get(slowest["layer"], [])
        if slowest
        else [],
        "spans": spans,
        "note": NOTE_FULL_STACK_TRACE,
    }


# ── Route-level live readers (session_summary / recent_samples / budgets /
# what_should_i_look_at_next) + snapshot fallback ──────────────────────────
# Faithful port of the RN/Node readers. Wire field names match the other
# runtimes exactly (``screen``, ``screens``, ``routes``, ``p50Ms``, …) so the
# same tool output shape lights up across all three. Python route labels take
# the canonical ``"screen:GET /path"`` shape in the uploaded snapshot rows, so
# stripping the ``"screen:"`` prefix yields the method-path label. Every mapped
# row is re-run through the PII guard (defense in depth) and dropped, never
# thrown, on a trip.

SCREEN_PREFIX = "screen:"

NOTE_LIVE = (
    "Live per-route data read from your Boosthis server (full-details "
    "telemetry). Worst routes first."
)

NOTE_SNAPSHOT_FALLBACK = (
    "Derived from your app's latest uploaded Boosthis SNAPSHOT (the full meter "
    "page). The raw per-sample stream is off (private / issues-only mode), so "
    "these are the snapshot's per-route aggregates, worst-first. For the "
    "complete meter page call boosthis.snapshot."
)


def _rate_ms(ms: int | float) -> str:
    """Classify a route p95 against the shared TTI thresholds."""
    tti = SCORE_THRESHOLDS["tti"]
    if ms <= tti["good"]:
        return "good"
    if ms >= tti["poor"]:
        return "poor"
    return "needs-work"


def _fetch_snapshot_obj(cfg: dict[str, str]) -> dict[str, Any] | None:
    """Fetch + PII-check the install's uploaded snapshot. None on any failure."""
    snap = _fetch_json(
        f"/snapshot?installId={urllib.parse.quote(cfg['install_id'])}", cfg
    )
    if not isinstance(snap, dict):
        return None
    if not _pii_safe(snap):
        return None
    return snap


def _fetch_screens(
    window_days: int, cfg: dict[str, str]
) -> list[Any] | None:
    """GET the per-route summary rows (raw full-details stream). None when the
    stream is off/unavailable so the caller falls back to the snapshot."""
    data = _fetch_json(
        f"/screens/summary?installId={urllib.parse.quote(cfg['install_id'])}"
        f"&windowDays={urllib.parse.quote(str(window_days))}",
        cfg,
    )
    if not isinstance(data, dict):
        return None
    screens = data.get("screens")
    if not isinstance(screens, list):
        return None
    return screens


def _snap_screen_rows(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-route aggregate rows from the snapshot ``rows`` (keys
    ``"screen:GET /x"``), worst-first by p95. Each row is PII-re-checked."""
    rows_in = snap.get("rows")
    rows_in = rows_in if isinstance(rows_in, list) else []
    out: list[dict[str, Any]] = []
    for r in rows_in:
        if not isinstance(r, dict):
            continue
        key = r.get("key") if isinstance(r.get("key"), str) else ""
        if not key.startswith(SCREEN_PREFIX):
            continue
        p95 = _num(r.get("p95"))
        p50 = _num(r.get("p50"))
        p99_raw = r.get("p99")
        p99 = p99_raw if isinstance(p99_raw, (int, float)) and not isinstance(p99_raw, bool) else None
        stdev_raw = r.get("stdev")
        stdev = (
            stdev_raw
            if isinstance(stdev_raw, (int, float)) and not isinstance(stdev_raw, bool)
            else None
        )
        row = {
            "screen": key[len(SCREEN_PREFIX):],
            "p50Ms": p50,
            "p95Ms": p95,
            "sampleCount": _num(r.get("count")),
            "worstRating": _rate_ms(p95),
        }
        if p99 is not None:
            row["p99Ms"] = p99
        if stdev is not None:
            row["stdevMs"] = stdev
        if p99 is not None and p50 > 0:
            row["spikeRatio"] = round(p99 / p50, 1)
        if _pii_safe(row):
            out.append(row)
    out.sort(key=lambda x: x["p95Ms"], reverse=True)
    return out


def _snap_findings(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Cross-cutting findings from the snapshot — structured fields only. The
    ``hint`` prose is intentionally excluded: the server drops it at
    ingest/readback, and copying it into MCP output would re-open the plain-text
    channel that server-side sanitization closes."""
    arr = snap.get("crossCutting")
    arr = arr if isinstance(arr, list) else []
    out: list[dict[str, Any]] = []
    for f in arr:
        if not isinstance(f, dict):
            continue
        item = {
            "kind": f.get("kind") if isinstance(f.get("kind"), str) else "unknown",
            "name": f.get("name") if isinstance(f.get("name"), str) else "",
            "p95Ms": _num(f.get("p95")),
            "count": _num(f.get("count")),
        }
        if _pii_safe(item):
            out.append(item)
    return out


def get_live_session_summary(
    window_days: int = 7, override: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """p50/p75/p95 + per-route worst-rating, worst-first. Falls back to the
    uploaded snapshot's per-route aggregates when the raw stream is off. Returns
    None (caller serves the on-device note) when no creds resolve or nothing is
    available. Never raises."""
    cfg = resolve_creds(override)
    if cfg is None:
        return None
    screens = _fetch_screens(window_days, cfg)
    if screens:
        mapped: list[dict[str, Any]] = []
        for s in screens:
            if not isinstance(s, dict) or not isinstance(s.get("routeLabel"), str):
                continue
            row = {
                "screen": s.get("routeLabel"),
                "sampleCount": _num(s.get("sampleCount")),
                "p50Ms": _num(s.get("p50Ms")),
                "p75Ms": _num(s.get("p75Ms")),
                "p95Ms": _num(s.get("p95Ms")),
                "worstRating": s.get("worstRating")
                if isinstance(s.get("worstRating"), str)
                else "good",
            }
            for extra in ("p99Ms", "stdevMs", "spikeRatio"):
                v = s.get(extra)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    row[extra] = v
            if _pii_safe(row):
                mapped.append(row)
        return {
            "available": True,
            "source": "boosthis-server",
            "windowDays": window_days,
            "screens": mapped,
            "note": NOTE_LIVE,
        }
    snap = _fetch_snapshot_obj(cfg)
    if snap is not None:
        rows = _snap_screen_rows(snap)
        if rows:
            return {
                "available": True,
                "source": "boosthis-snapshot",
                "windowDays": window_days,
                "screens": [
                    {
                        "screen": r["screen"],
                        "sampleCount": r["sampleCount"],
                        "p50Ms": r["p50Ms"],
                        "p95Ms": r["p95Ms"],
                        "worstRating": r["worstRating"],
                        **{
                            k: r[k]
                            for k in ("p99Ms", "stdevMs", "spikeRatio")
                            if k in r
                        },
                    }
                    for r in rows
                ],
                "note": NOTE_SNAPSHOT_FALLBACK,
            }
    return None


def get_live_recent_samples(
    limit: int = 20,
    name: str | None = None,
    override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Most recent raw samples (newest first), optionally filtered by route.
    The snapshot carries NO raw per-sample stream, so on fallback it returns an
    empty list plus a note pointing at the aggregate tools. Never raises."""
    cfg = resolve_creds(override)
    if cfg is None:
        return None
    n = int(limit) if isinstance(limit, (int, float)) else 20
    lim = max(1, min(200, n or 20))
    path = (
        f"/samples/list?installId={urllib.parse.quote(cfg['install_id'])}"
        f"&limit={urllib.parse.quote(str(lim))}"
    )
    if isinstance(name, str) and name:
        path += f"&routeLabel={urllib.parse.quote(name)}"
    data = _fetch_json(path, cfg)
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list) and items:
            out: list[dict[str, Any]] = []
            for s in items:
                if not isinstance(s, dict) or not isinstance(s.get("routeLabel"), str):
                    continue
                row = {
                    "screen": s.get("routeLabel"),
                    "durationMs": _num(s.get("durationMs")),
                    "rating": s.get("rating")
                    if isinstance(s.get("rating"), str)
                    else "good",
                    "ruleId": s.get("ruleId")
                    if isinstance(s.get("ruleId"), str)
                    else None,
                    "at": s.get("createdAt")
                    if isinstance(s.get("createdAt"), str)
                    else None,
                }
                if _pii_safe(row):
                    out.append(row)
            return {
                "available": True,
                "source": "boosthis-server",
                "samples": out,
                "count": len(out),
            }
    snap = _fetch_snapshot_obj(cfg)
    if snap is not None:
        return {
            "available": True,
            "source": "boosthis-snapshot",
            "samples": [],
            "count": 0,
            "note": (
                "No individual perf samples are available for this install. The "
                "raw per-sample stream is uploaded only in full-telemetry mode; "
                "private / issues-only apps upload only the aggregate snapshot. "
                "Use boosthis.session_summary for per-route aggregates or "
                "boosthis.snapshot for the full meter page."
            ),
        }
    return None


def get_live_budgets(
    window_days: int = 7, override: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Auto-learned baselines per route + regressions (recent vs older p95).
    On fallback, derives regressions from the snapshot's cross-cutting findings;
    the snapshot has no per-route recent-vs-older window, so ``all`` carries the
    current p95 with an insufficient-data trend. Never raises."""
    cfg = resolve_creds(override)
    if cfg is None:
        return None
    screens = _fetch_screens(window_days, cfg)
    if screens:
        all_rows: list[dict[str, Any]] = []
        for s in screens:
            if not isinstance(s, dict) or not isinstance(s.get("routeLabel"), str):
                continue
            row = {
                "screen": s.get("routeLabel"),
                "recentP95Ms": s.get("recentP95Ms")
                if isinstance(s.get("recentP95Ms"), (int, float))
                else None,
                "baselineP95Ms": s.get("olderP95Ms")
                if isinstance(s.get("olderP95Ms"), (int, float))
                else None,
                "trend": s.get("trend")
                if isinstance(s.get("trend"), str)
                else "insufficient-data",
            }
            if _pii_safe(row):
                all_rows.append(row)
        regressions = [r for r in all_rows if r["trend"] == "regressed"]
        return {
            "available": True,
            "source": "boosthis-server",
            # A DIFFERENT algorithm from the kit's own learned budget, over
            # different data: Boosthis splits the window's uploaded readings
            # into an older and a recent half per route. Named so a reader
            # never mixes its numbers with the device's frozen-baseline
            # verdicts.
            "algorithm": "server-window",
            "span": (
                f"the last {window_days} days of readings uploaded from every "
                "install of this project"
            ),
            "windowDays": window_days,
            "all": all_rows,
            "regressions": regressions,
        }
    snap = _fetch_snapshot_obj(cfg)
    if snap is not None:
        all_rows = [
            {
                "screen": r["screen"],
                "recentP95Ms": r["p95Ms"],
                "baselineP95Ms": None,
                "trend": "insufficient-data",
            }
            for r in _snap_screen_rows(snap)
        ]
        cc = snap.get("crossCutting")
        cc = cc if isinstance(cc, list) else []
        regressions = []
        for f in cc:
            if not isinstance(f, dict) or f.get("kind") != "regression":
                continue
            base = f.get("baseline")
            if not isinstance(base, dict):
                continue
            reg = {
                "screen": f.get("name") if isinstance(f.get("name"), str) else "",
                "recentP95Ms": _num(f.get("p95")),
                "baselineP95Ms": _num(base.get("p95")),
                "deltaPct": _num(base.get("deltaPct")),
                "trend": "regressed",
            }
            if _pii_safe(reg):
                regressions.append(reg)
        if all_rows or regressions:
            return {
                "available": True,
                "source": "boosthis-snapshot",
                # A third answer again: the snapshot's Baseline axis, computed
                # by the reporting install itself over its own session. Not
                # comparable with either of the other two.
                "algorithm": "snapshot-baseline-axis",
                "span": "the reporting install's most recent snapshot",
                "windowDays": window_days,
                "all": all_rows,
                "regressions": regressions,
                "note": NOTE_SNAPSHOT_FALLBACK,
            }
    return None


def get_live_what_next(
    limit: int = 5, override: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Proactive triage: worst-rated routes first. Falls back to the snapshot's
    worst routes + cross-cutting findings. Never raises."""
    cfg = resolve_creds(override)
    if cfg is None:
        return None
    n = int(limit) if isinstance(limit, (int, float)) else 5
    lim = max(1, min(50, n or 5))
    screens = _fetch_screens(7, cfg)
    if screens:
        routes: list[dict[str, Any]] = []
        for s in screens:
            if not isinstance(s, dict) or not isinstance(s.get("routeLabel"), str):
                continue
            p95 = _num(s.get("p95Ms"))
            rating = (
                s.get("worstRating") if isinstance(s.get("worstRating"), str) else "good"
            )
            trend = s.get("trend") if isinstance(s.get("trend"), str) else "insufficient-data"
            reason = f"p95 {p95}ms · rated {rating}" + (
                " · regressing" if trend == "regressed" else ""
            )
            row = {
                "screen": s.get("routeLabel"),
                "p95Ms": p95,
                "worstRating": rating,
                "trend": trend,
                "reason": reason,
            }
            if _pii_safe(row):
                # Attach the symptom nudge (shipped constants) AFTER the PII
                # filter so the guard never drops a row over it.
                row.update(
                    nudge_fields(
                        {
                            "worstRating": rating,
                            "p50Ms": s.get("p50Ms"),
                            "p99Ms": s.get("p99Ms"),
                            "spikeRatio": s.get("spikeRatio"),
                        }
                    )
                )
                routes.append(row)
        return {"available": True, "source": "boosthis-server", "routes": routes[:lim]}
    snap = _fetch_snapshot_obj(cfg)
    if snap is not None:
        rows = _snap_screen_rows(snap)
        findings = _snap_findings(snap)
        if rows or findings:
            snap_routes: list[dict[str, Any]] = []
            for r in rows[:lim]:
                route = {
                    "screen": r["screen"],
                    "p95Ms": r["p95Ms"],
                    "worstRating": r["worstRating"],
                    "trend": "insufficient-data",
                    "reason": f"p95 {r['p95Ms']}ms · rated {r['worstRating']}",
                }
                # Symptom nudge (shipped constants) — the snapshot rows already
                # passed the PII guard in _snap_screen_rows.
                route.update(
                    nudge_fields(
                        {
                            "worstRating": r["worstRating"],
                            "p50Ms": r.get("p50Ms"),
                            "p99Ms": r.get("p99Ms"),
                            "spikeRatio": r.get("spikeRatio"),
                        }
                    )
                )
                snap_routes.append(route)
            return {
                "available": True,
                "source": "boosthis-snapshot",
                "routes": snap_routes,
                "findings": findings[:lim],
                "note": NOTE_SNAPSHOT_FALLBACK,
            }
    return None


__all__ = [
    "resolve_creds",
    "get_live_snapshot",
    "get_live_crash_risk",
    "get_live_full_stack_trace",
    "get_live_session_summary",
    "get_live_recent_samples",
    "get_live_budgets",
    "get_live_what_next",
    "NOTE_SNAPSHOT",
    "NOTE_SNAPSHOT_UNAVAILABLE",
    "NOTE_CRASH_RISK",
    "NOTE_CRASH_RISK_UNAVAILABLE",
    "NOTE_FULL_STACK_TRACE",
    "NOTE_FULL_STACK_TRACE_UNAVAILABLE",
    "NOTE_LIVE",
    "NOTE_SNAPSHOT_FALLBACK",
    "SCREEN_PREFIX",
    "CRASH_KIND_RULES",
    "DEFAULT_CRASH_RULES",
    "STABILITY_RULES",
    "LAYER_RULES",
]
