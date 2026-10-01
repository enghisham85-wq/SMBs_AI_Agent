"""Floating Boosthis bubble for Python web apps — the live perf badge.

``boosthis.mount(app)`` injects a small self-contained ``<script>`` into
outgoing HTML pages (everywhere, by default — development AND production) that
draws a movable badge showing the process's live page pulse: one score, one
rating, one sample count. The badge polls the closed ``<prefix>/pulse``
endpoint (served by ``mount()``), so it works on the developer's own pages
without any extra wiring.

Visibility precedence (first match wins):
  1. ``BOOSTHIS_DISABLED`` — the global kill-switch always wins (hidden).
  2. ``BOOSTHIS_BUBBLE`` env — "1"/"true"/"yes"/"on" forces on,
     "0"/"false"/"no"/"off" forces off; anything else falls through.
  3. Legacy aliases: truthy ``BOOSTHIS_NO_BUBBLE`` hides, then truthy
     ``BOOSTHIS_FORCE_BUBBLE`` shows.
  4. The ``bubble`` option passed to ``boosthis.mount(app, bubble=...)``.
  5. Default: VISIBLE. The bubble shows on every deployment — live/published
     sites included — so an install never looks broken in production. Opt out
     explicitly with ``BOOSTHIS_BUBBLE=0`` or ``mount(app, bubble=False)``.

Privacy: display-only. The injected script reads ONLY the closed pulse shape
(``{score, rating, sampleCount}``) from the app's own origin and never
transmits anything anywhere else. Injection is fail-open: any doubt (non-HTML,
compressed, oversized, missing ``</body>``, any thrown error) serves the
original bytes untouched. Mirrors the Node/Web kit bubbles exactly.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from boosthis import samples
from boosthis.budgets import BUDGET_BASELINE_N
from boosthis.meter_axes import BASELINE_ALGORITHM, BASELINE_RECENT_N
from boosthis.runtime_vitals import COLD_START_MEASURED_TO_SERVING
from boosthis.kill_switch import (
    force_entitlement_check,
    get_entitlement_gate_kind,
    get_entitlement_message,
)
from boosthis.health_axes import linear_score
from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.project_identity import (
    INSTALL_ID_LABEL,
    INSTALL_ID_UNKNOWN_TEXT,
    PROJECT_LABEL,
    PROJECT_UNKNOWN_TEXT,
    SCORE_CAPTION,
    project_display,
)
from boosthis.thresholds import (
    SCORE_THRESHOLDS,
    RESILIENCE_MIN_MEDIAN_MS,
    RESILIENCE_MIN_TAIL_SAMPLES,
    RESILIENCE_TAIL_FLOOR_MS,
)

# The pulse endpoint path suffix, appended to the mount prefix. So the default
# prefix "/_boosthis" serves the pulse at "/_boosthis/pulse", matching Node.
PULSE_SUFFIX = "/pulse"

# The panel endpoint path suffix — the closed, coarse dashboard read the
# bubble's full panel polls. Same visibility policy as the pulse.
PANEL_SUFFIX = "/panel"

# The closed account auth-context endpoint path suffix, appended to the mount
# prefix. Served by mount() behind the SAME strict loopback guard as /connect.
ACCOUNT_SUFFIX = "/account"

# Boosthis brand icon — the signature navy "B" with an orange lightning bolt,
# the SAME mark shipped as the RN kit's ``src/ui/brandIcon.ts`` (which resizes
# artifacts/mobile/assets/images/icon.png). Here it is the 96x96 variant,
# embedded as a PNG data URI so the injected floating bubble shows the real
# brand mark without the kit shipping any asset files — a plain ``<img src>``
# renders it in every browser. Do NOT regenerate or substitute it here; keep
# it byte-identical to the shared mark.
#
# Regenerate (keep it byte-for-byte the app icon, just smaller):
#   magick artifacts/mobile/assets/images/icon.png -resize 96x96 /tmp/b.png
#   base64 -w0 /tmp/b.png
# paste below, then repack the kit.
BOOSTHIS_ICON_URI = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAMAAADVRocKAAACK1BMVEUZIDoYHzoXHjgWIDkVGzQZGS4aGzMeIDY1OEpIR1dEQ1M/QVE9O0yxkFzjsmPcql7drWHdsGZHODcHES9LSllISFdLSVleVE3/w2T/xWX/x2j/yGj/0m/CnWUmKT1BQE80Nkmxi1n/xWLzuGP6v2X8wWVKRUklJTkSFzD8vmSOclEmK0ESFi39u2PcqGJtXlAuMUQ/QE9APk92Y1I+P1AuLUAOEynJk1f8tmD1sl5ZSEX8sl6Mb006OEanfVPapWA0MkTvp1wrKj0BByHRlVX7q1osJCtBP1BzW0gDDSk5N0mod0z4qFhIPkGfcEv+qVc5MzzpmFIpJjY5NkaFXkb3o1R3Uz8xLj7RjFLKhU4NECZgR0D7pFX6nFFIMzMjIja0c0n2m1EiHi6UXUBmRjk0KzXijE37mU8SEyb5k0vnkE/biE3ahkzgjlTJd0VbQDr3kkqta0EwL0H2ikb3jkj4jUgrKDfegEb6i0UIDih3RzgKDCP1hENYPTuUVzupYj7uhUM+LjJALzI5KSw3KCvDaDpHJyj9g0LdeEEWGC8hHzS4WzT2fkD0ej1ULSoGCiEoJzsFBxwICh+lVjVpNSsGCB0sGh+LSDGTRSv+ejwoGSbtdT30dDr/gD/ZZzcZEBzqaTX8dDkzHCJ4Oij1cTeJQSzzazSqTjD/cjXIWjL9bTQXDxvxZjEwGR64UC+GOib4ZzClSSzsYi/ZXTAiFiP2Zy7+aS4bFSaDQTBq3a+TAAAK3ElEQVRo3rWZi3vb1BnGFWM5l2pslLVLYWvNWiqFqhCyWSsYM1UhLUm7ZbiwDK2tSAuLs44uywUilI1baeasqSIRDwJdga6ENsvWC13Zn7fvOxfpSLZz4Xn6xrHko3Pe33c5slNXktJq4ceWlsyaSly/jx3IQlzb0iI1ErpuyB39W5oMg/d99b78uK7xBtSSaWlclrXcs9nsZjGJBNYLPZvdPIHUa73AE4DNEzKZjbQzwYnOUsemgA25xsHjCX0hOK8FkdZwTb7OJAey9ckkZjcFkGjjirMTNhBfS6l5IlKdezbGZOL1MWEdpdOQEvbpCmSbEjIbhIgAsarJBNZzzGSaEyTRv36jiwBZziXUKsFvswiicCUMNOWfbWvviLRli0LU9p37v0v1Pa4HHrh/64ONU4pjlRKt5IDvb9tOtA3VjjjlB507Ojs7dxA99DDRjh/+aKe0Zt3gISXH6EYEAHWGB7Hv2JV/5Me79zA9uudRos7dexUlrzYD0IgbRaC1bW+nAO7f0bZ3d1dCkM2ehx/bpyhwLaOvsZsaAzB4UhvejPxjnfuThK49Ox7PtynKFri6T2teqIY1pID2dqHPj3ftR1Fvcrb/oSe6FQJo396tNyU0AcCq9gjwZE/+J537Y1FW5yPor/wUp24vGJsBaAzA/RWl+2ddB/Yn1fXU3jz4P0lntqvGZgGxYKc8XSweAAn+Bzqf2aeUYkBHVtf0DQPyMWAXxK90P1t8ihCQQY9dPzfbSgDYxeZtMw19wwBDBIB/ae/BYrHICCh40fVEt+gPyjUuUgzQowiMPC//rp4eRTGlZ7qKKXU9q2KBBEC7ZayZgaYzgKYBgK9C/57W3ueKfcU+UAw4eMiwwL9HaFVJJxbQCvjhMVOAhrYMAMcog8Por/RkthL3WMW+ruf7s4aaqFBHR8YgACpiSwA0bhzTIHgNt0IEwH0+oGSOFI+mCM/hIqOwS0wBdiqacDcMGl8gQOMAHYd4Bj0k/lK+8Iu+Om3tx0XZUk/PYaoOeOwEgIY2GLmGQo6kc9EBvG6Y7ay/Sqlkqr8sDlLbo4Pk5GjxSD/JXi90KBDGYYrpKBjMSMdCa+y8DoAlMkkDiH++8KsXBkUB4WD5QTJPV3vohxFClI5uI6p/ZJrVJAYSBYDDBFAqHTPVFwdfShAGi7/upyU1JIUBcHKHaSRcNNIRTdKT3hzACpQfsn4TA+jJyzZ10owMB+DsqERx9FgTKelugBBA/Qcggd8eP/FSQn0n+8k0mMcBlGLhGLoY9ISKAdgYW9jdQYpbOpYfcl45PnxCQAwPHmH+hmG3Kvh2wQGqrRsGv2bwekjRSETQjcJh6g8JbB0eBgATAI6/cMpmceiGNQCKCDnbSIhOk0R73oMCL9BQ5vTx4WGBMdz3aq9hoyABzTTNYxGiTTNSIvFKsblu8GohAPzzpvXa70ZGhgUdf7kSy8IcEVGCz2bFNOoARgIQZ2UUoLboPyQ9PzI6IurE78+c+QOIPJ95/fWnC2YeACCl1TYaSeKtFcGWMoD+plU+OzKaFOMQjZ540YIqAWGgpOQNoxnASBNsAOTBf6j1jyNjKcAoGRjD57GR046JyrdBH3J2M0D9GADQ37RyfxodS3vHr0ZecayhcULIK5ZtNClRo0GrhP5D6smJyUnmOoYiiCk6MDl69g3LssYJYaCAcdkJBO4zuzHAtmBzmONW5vQoEFBjY5Njk0xTRJNTb76mAmAICXmLbd1kQ8lQc4ClvvHm9MTUpKgpZj81OT1xstWyWAqOa1MCSSK5ayQhJZqUbbtq3hwfH8q8NT0RKQp+Ah4T8PRqzooAVqvh2p5tx5CYkgDYNC1XBX/Lcs5Oz0wICHaklLey5TIBFEgTLNll5p6Yik4ANq8YK5vtqebQkCX9eXoGBJb4OyXYT81M/QX8OQBlmq2unRZJRKLGhngBMoCVrW9PvzMDP1xRKjMz02/LDvO3CoXCOEJMx7M9z40wLo3blhjKqANY77733rvvv3/uHDzOnTv0AckHMgL/s6ecsgBAWZaZc8Hf9UBxBjYHQF3IMF63PWcclzoO/FNSprLPz0Sa/euhXDkJsArY7qpHCcTEZclIYmc8KtchS8HDcebmToGcyt9mL0SEk3LsH2dgjVvEHRlgTo4xgMafAqgAAMmVint+Zv4CaGZmfn72ol+p6BU5p6YA1njOd10GYCceBfCyeSxHB+cvlFVKkIMw+HB2nhAuzM8uBmEN5XtVtRAXCQFlYs6iZASJBU56wDLweIlUVXWcshwGf6fu6P9R4Pm+T6b5NBKKIM+6V4eQBP8UYGEBEJiBF3y8NE81+yHx9/lM1VqwYhWgRkLwAiD2FgAWAoAg937y6SXq/68PoDJ+GHKCby/s3CkAVJ/F7rHdBAB4StrD8BzPAGukysFHs5cIYenyZ5BAKKTg5yyRUPai3FxWKMl2k+ZwAVYt0AJhAo7+2edfXELNf3olCEOM3/eYk6/DxIUFayd5thZsX/QiAC8tAigzfwT0/3PpKgWcJxuIEKgAoy4Q4dyFsqXz9kd5SGl3kjdzR/+53KnLXyDg6tIi8w99L0Y4xJ4AVAAwOq2QH2XgevwWwCMA1Mh/znhrifp/SW8AIQGUo6p8tqoCgPp7vs92s8SbHrcCAFH8kIBMElhe+iqoUSUJjiqIATyeYcMeeF6YI9sT7SGBxWvLy1eXr10PABBQCK4Nabu9BEC1uS/vMc1AMCdrYwC8m15fWV5evnb5X+wdIgXQHYfmSgH0LhT3sSTUxqPLIIMo/py9uLK6vLzy+b/Bn9cI+8zaUZNxIplOOB5xFioolIi8DvE2DWXSXbDPyfp1AsANGsSAKBfPcVqdSGouvQV8XxLcKQJmyMw+J9sXV1ZXV6/BBg3iBCJOUMNvN53cXCulqDJJjkkA+OT2jy8QAH41WnX/A4CVG+CPAtOgxkHwOkx8k+rMOXZNJIQMIEZPQFBYukR2z9+E+G8FDRXaubRI+cKwxuzxV2LWsT8C5uj8qvvVyu2V6yG/Iu7lSkUwztCDHtAdwDcCMiShNqxqYY2trfafX169+fUdGz4hK5VqtZprIuafC+MCkp2AZlJINk6smh8GFCBXem+t3Lx9xSX+hIAQ+BsjRcqICcRbmTzzEvHYafv0nEwKdOX2f1cu9ld0ESBnq3Jjf7nWQCIg2t0AyGWz2Ur/x3fvLvbjnxB65A8E+MEUYEYS5Af1/nEG0HkoWQTIQqDuJ7fv3ugFf130R19Nk5k/PjGSFzTOAHtA+hwPIkCrVvq/vHYL/wTSK4J/Ff2rGv3OLAJkm/hDyFINAWF0+wc1nwCyFfeb1esVu1IR/TWSgcYykak5+mc9fiemCRLuoPQwAfTeuH0n5Y8IYghNAFe2mSD8aijcgEkvqVYPCHSwcO98fQUanAYkvrJk3c3akDi3ThOkWn0CAKjqvf8jGwh3EN07NAGZ7FQSNgNUbXJzNS4RAPz6MehB1buzaLP9U6nK6f8filTVvRr9qAsavN3SDBoAjKrtf2Pb0bceDFRh3zHxo+2JHxINfPCilHgVvWAx1eLOJQ5iQ5tEzqpWk3iPg7ViSa8ix5oQQhRXOsIog2A9QBxxtFuC+EJqbo3nygFRwnWQIA0IGgOCxgEFFBCXeJ3yNM2uwSxGkDawfpOAZBpS7R5LBPjf2mWDgHuewb0FBM3ut43df+sA4nsx3tzf3jMQT/4PiY93LR3+sbEAAAAASUVORK5CYII="
)

# The accepted-terms revision the bubble's first-open gate records/checks.
# Byte-equal to the canonical TERMS_VERSION constants (RN
# ``lib/boosthis-runtime-rn/src/ui/consent.ts``, web
# ``lib/boosthis-runtime-web/src/terms.ts``, server
# ``artifacts/api-server/src/lib/accounts.ts``) that the ``scripts``
# legal-consistency test keeps in agreement. Python is a server kit with no
# consent module of its own, so the value is embedded here as a literal; do NOT
# edit it in isolation — bump it in lockstep with the others.
TERMS_VERSION_LITERAL = "2026-07-21"

# Max response size we will buffer while deciding whether to inject.
MAX_INJECT_BYTES = 2 * 1024 * 1024

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _read_bubble_override(env: dict[str, str] | None = None) -> bool | None:
    """Read the ``BOOSTHIS_BUBBLE`` override. None when unset/unrecognised."""
    e = env if env is not None else os.environ
    raw = e.get("BOOSTHIS_BUBBLE")
    if raw is None:
        return None
    v = str(raw).strip().lower()
    if v in _TRUE_VALUES:
        return True
    if v in _FALSE_VALUES:
        return False
    return None


def resolve_bubble_visibility(
    option: bool | None = None, env: dict[str, str] | None = None
) -> bool:
    """Resolve whether the bubble should be injected. See precedence above."""
    # The absolute stop gets its own fail-CLOSED guard. Nothing below may turn
    # the bubble back on when this read succeeds or fails.
    try:
        if is_boosthis_disabled():
            return False
    except Exception:  # noqa: BLE001
        return False

    # Every ordinary configuration failure is fail-open. A broken environment
    # accessor must never become an accidental second kill switch.
    try:
        e = env if env is not None else os.environ
        override = _read_bubble_override(env)
        if override is not None:
            return override
        if str(e.get("BOOSTHIS_NO_BUBBLE") or "").strip().lower() in _TRUE_VALUES:
            return False
        if str(e.get("BOOSTHIS_FORCE_BUBBLE") or "").strip().lower() in _TRUE_VALUES:
            return True
        if option is not None:
            return option
        # Default TRUE — visible on live/published sites too. An install that
        # shows nothing in production reads as broken; hiding is an explicit
        # opt-out.
        return True
    except Exception:  # noqa: BLE001
        return True


def _linear_score(duration_ms: float) -> int:
    """Map a p95 duration onto 0–100 against the TTI budget (100 at/below the
    good line, 0 at/above the poor line, linear between). Mirrors Node."""
    good = SCORE_THRESHOLDS["tti"]["good"]
    poor = SCORE_THRESHOLDS["tti"]["poor"]
    # Delegates to the kit's one checked scorer. This was a private copy of the
    # interpolation with no band check in it, so an inverted TTI band would
    # have been refused everywhere EXCEPT here, where it would have gone on
    # publishing a score.
    return linear_score(float(duration_ms), float(good), float(poor))


def _rating_for_score(score: int) -> str:
    if score >= 85:
        return "good"
    if score >= 60:
        return "needs-work"
    return "poor"


def compute_pulse() -> dict[str, Any]:
    """Compute the closed pulse shape from the in-process sample buffer. Never
    includes route labels or durations — one score, one rating, one count."""
    try:
        s = samples.summary()
        total = s.get("total") or 0
        p95 = s.get("p95_ms")
        if total == 0 or p95 is None:
            return {"score": None, "rating": None, "sampleCount": total}
        score = _linear_score(p95)
        return {"score": score, "rating": _rating_for_score(score), "sampleCount": total}
    except Exception:  # noqa: BLE001
        return {"score": None, "rating": None, "sampleCount": 0}


# Display order + labels for the panel's meter rows. Core axes always render
# (as "measuring…" while absent); environment-dependent axes (container
# pressure, GC meters, …) appear only when honestly computable — mirroring the
# honest-N/A doctrine of the meter page.
PANEL_AXIS_LABELS: dict[str, str] = {
    "responsiveness": "Responsiveness",
    "resilience": "Resilience",
    "budget": "Budgets",
    "eventLoopLag": "Event-loop lag",
    "memoryStability": "Memory stability",
    "residentGrowth": "Memory growth",
    "heapHeadroom": "Heap headroom",
    "gcPressure": "GC pressure",
    "gcTax": "GC tax",
    "gcPauseTail": "GC pause tail",
    "gcGenerationBalance": "GC generation balance",
    "finalizerBacklog": "Finalizer backlog",
    "containerPressure": "Container pressure",
    "crashFree": "Crash-free",
    "reliability": "Reliability",
    "confidence": "Confidence",
    "baseline": "Baseline",
    "network": "Network",
    "idle": "Idle efficiency",
    "latencyFloor": "Latency floor",
    "schedulerLatency": "Scheduler Latency",
    "gilContention": "GIL contention",
    "taskBacklog": "Task backlog",
    "workerRestarts": "Worker recycling",
    "startupImport": "Startup import cost",
    "coldStart": "Cold start",
    "swallowedErrors": "Swallowed errors",
    "leakWatch": "Leak Watch",
    "cookieExposure": "Cookie Exposure",
    "devPosture": "Dev Posture",
    "accessPressure": "Access Pressure",
    "refusalHonesty": "Refusal honesty",
    "threadPoolStarvation": "Thread pool starvation",
    "blockingAsync": "Blocking calls in async",
    "loadDeflection": "Load deflection",
    "memoryPerRequest": "Memory per request",
    "forkChurn": "Process churn",
    # 2026-08 "honest limit" batch (11 axes to the Python ceiling of 37).
    "allocChurn": "Allocation churn",
    "peakRss": "Peak memory",
    "contextSwitching": "Context switching",
    "pageFaults": "Page faults",
    "ioPressure": "Disk I/O pressure",
    "cpuEntitlement": "CPU entitlement",
    "cpuConsumption": "CPU consumption",
    "threadPoolSaturation": "Thread pool saturation",
    "threadFootprint": "Thread footprint",
    "queueTime": "Queue time",
    "worstFreeze": "Worst freeze",
    "workerImbalance": "Worker imbalance",
    "descriptorMix": "Descriptor mix",
    "importChurn": "Import churn",
    "asyncSlowCallbacks": "Slow async callbacks",
    "gcGenOccupancy": "GC generation occupancy",
    "runtimeCapability": "Runtime capability",
    # Patch Lag ("exposure window") — appears ONLY when the axis is present (a
    # build time is known); never a warming-up row. The caption reports build
    # AGE only and never implies the app is safe/patched.
    "patchLag": "Patch lag",
    # Repeated Work — the same call made more than once inside ONE request.
    # NOT in _PANEL_EXPECTED_AXES: it needs calls whose identity the kit can
    # see (@track_perf-decorated work, explicit outbound URLs). An app that
    # wraps none can honestly be "cannot tell" forever, and a permanently
    # warming row would be a fake meter.
    "repeatedWork": "Repeated Work",
    # Database Work — how much of a request was spent waiting on the database,
    # and whether one request ran the same statement over and over. NOT in
    # _PANEL_EXPECTED_AXES for the same reason as Repeated Work: an app with no
    # database, or one whose driver this kit cannot watch, honestly has no
    # reading, and a permanently warming row would be a fake meter.
    "dbWork": "Database Work",
    "backpressure": "Backpressure",
    "connectionSetup": "Connection setup",
    "exceptionChurn": "Exception churn",
    "failureContainment": "Failure containment",
    "routeFailures": "Route failures",
    "timeoutHeadroom": "Timeout headroom",
    "timerHealth": "Timer health",
    "unhandledErrors": "Unhandled errors",
    "upstreamCache": "Upstream cache",
    "uptimeStability": "Uptime stability",
}

_PANEL_CORE_AXES = ("responsiveness", "resilience", "budget")
_PANEL_RATINGS = {"good", "needs-work", "poor"}
# The two no-score labels an axis may declare for itself. "pending" is NOT in
# here on purpose: it is the fallback, not a claim, and an axis that means one
# of these two must say so rather than inherit the waiting word.
_PANEL_NO_SCORE_RATINGS = {"not-available", "not-scored"}
_PANEL_CAPTION_MAX = 120

# The additive axes this kit reliably produces once the app is running, keyed by
# their WIRE keys (the keys ``read_vitals()``/``_build_axes()`` upload). Each one
# missing from the live snapshot is appended as a muted "warming up" row so the
# in-app panel shows the SAME meter set the web dashboard renders for a Python
# project (``EXPECTED_AXES_BY_KIND.python`` in the api-server's snapshotView).
# Environment-dependent axes (container pressure, CPU throttling, fd saturation,
# cold start, the asyncio loop meters) are deliberately ABSENT: they can be
# honestly N/A forever, and a permanently-warming row would be a fake meter.
PANEL_EXPECTED_AXES = (
    "cpuConsumption",
    "memoryStability",
    # residentGrowth and peakRss are NOT here: the interpreter answers for its
    # own heap on every platform, but whole-process resident memory comes from
    # /proc/self/statm and its high-water mark from getrusage, so both are
    # honestly absent off Linux/POSIX — a row warming for ever on Windows
    # would be a fake meter (same posture as the other /proc readings).
    "gcPressure",
    "gcTax",
    "gcPauseTail",
    "gcGenerationBalance",
    "finalizerBacklog",
    "reliability",
    "crashFree",
    "threadHealth",
    "schedulerLatency",
    "worstFreeze",
    "threadFootprint",
    "startupImport",
    "swallowedErrors",
    "leakWatch",
    "accessPressure",
    "refusalHonesty",
    "memoryPerRequest",
    "forkChurn",
    # 2026-08 honest-limit batch: only its ALWAYS-AVAILABLE warm-up-gated axes.
    # The Linux-gated (peakRss/contextSwitching/pageFaults/ioPressure/
    # descriptorMix), platform-gated (cpuEntitlement, runtimeCapability) and
    # opt-in (asyncSlowCallbacks) axes stay out: honestly N/A forever is not
    # "warming up".
    "allocChurn",
    "importChurn",
    "gcGenOccupancy",
    # Capacity of the volume holding the kit's state is readable immediately,
    # so this resolves on the first snapshot rather than warming forever.
    "diskPressure",
    # These three always warm toward a real process reading.
    "timerHealth",
    "unhandledErrors",
    "uptimeStability",
)

# Confidence rides the uploaded ``axes`` map as FOUR scalar top-level keys
# (confidence/confidenceMounts/confidenceRating/confidenceCaption), not an axis
# object, so the main axis loop (which only renders dict axes) skips it. We
# project those scalars into the single "Confidence" tile here.
_PANEL_CONFIDENCE_SCALARS = (
    "confidence",
    "confidenceMounts",
    "confidenceRating",
    "confidenceCaption",
)

# Caption shown on an expected-but-not-yet-measurable axis row.
_PANEL_WARMING_CAPTION = "warming up"

# A few expected axes have a real, quotable warm-up gate the generic "warming
# up" caption hides. leakWatch will not report until a 5-minute observation
# window has elapsed (it must never claim an early clean bill), so its warming
# row states that gate honestly.
_PANEL_WARMING_CAPTION_BY_KEY = {
    "leakWatch": "warming up \u00b7 needs 5 min of observation",
    # accessPressure will not rate until BOTH gates pass (a rating with no
    # baseline would be a guess), so its warming row states that gate honestly.
    "accessPressure": "warming up \u00b7 needs 10 min + 50 responses",
    "refusalHonesty": "warming up \u00b7 10 min minimum window",
    "uptimeStability": "process clock drift",
}


def _no_score_rating(axis: Any) -> str:
    """WHICH silence a no-score row is: ``pending`` (warming up — a score is
    coming), ``not-scored`` (a real reading deliberately never graded) or
    ``not-available`` (this host cannot take the reading at all).

    One decision shared by every no-score row this panel builds, so the
    object-axis loop and the warming backfill can never give one reading two
    different words.

    The order is the order of trust: the axis's own label first, then positive
    evidence beside a zero. ``measurable: 0`` alone is NOT evidence — axes here
    ship a zero while still gathering (database work below its minimum watched
    requests, a baseline whose routes were all too fast to divide by), and
    telling that reader nothing is coming is the same lie in the other
    direction. Every reading this kit cannot take on its host carries a
    ``reasonCode`` beside its zero; without one the row stays ``pending``.
    """
    if not isinstance(axis, dict):
        return "pending"
    declared = axis.get("rating")
    if declared in _PANEL_NO_SCORE_RATINGS:
        return str(declared)
    if axis.get("measurable") != 0:
        return "pending"
    return "not-available" if isinstance(axis.get("reasonCode"), int) else "pending"


def _blind_clients(axis: Any) -> str:
    """"N HTTP clients in this app can't be watched", or "" when the picture is
    complete. A COUNT only — which library it is never leaves the app."""
    try:
        n = axis.get("unwatchedClients")
        if not isinstance(n, (int, float)) or n <= 0:
            return ""
        n = int(n)
        return f"{n} HTTP client{'' if n == 1 else 's'} in this app can't be watched"
    except Exception:  # noqa: BLE001
        return ""


def _blind_db_clients(axis: Any) -> str:
    """"N database libraries in this app can't be watched", or "" when the
    picture is complete. A COUNT only — which library it is never leaves the
    app. Separate wording from :func:`_blind_clients` on purpose: telling a
    developer their HTTP client is unwatched when it is really their database
    driver sends them to the wrong place."""
    try:
        n = axis.get("unwatchedClients")
        if not isinstance(n, (int, float)) or n <= 0:
            return ""
        n = int(n)
        return (
            f"{n} database librar{'y' if n == 1 else 'ies'} in this app "
            "can't be watched"
        )
    except Exception:  # noqa: BLE001
        return ""


def _panel_label(key: str) -> str:
    label = PANEL_AXIS_LABELS.get(key)
    if label:
        return label
    # Humanize an unknown camelCase key rather than dropping the meter.
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key)
    return words[:1].upper() + words[1:]


def _panel_caption(key: str, axis: dict[str, Any]) -> str:
    """Honest numeric caption for one axis. Prefers the axis's own caption
    (vitals ship one); falls back to per-key formats mirroring the web panel,
    then to a bare score. Display-only; length-capped closed channel."""
    try:
        cap = axis.get("caption")
        if isinstance(cap, str) and cap.strip():
            out = cap
        elif key == "coldStart" and isinstance(axis.get("startupMs"), (int, float)):
            seconds = axis["startupMs"] / 1000
            # Named by the interval that was actually measured. The whole one
            # says what a developer expects the tile to mean; the partial one
            # says plainly where it stops, and never pretends to be the whole.
            # The code is read from the module that emits it, never retyped.
            if axis.get("measuredTo") == COLD_START_MEASURED_TO_SERVING:
                out = f"ready to serve in {seconds:.1f}s"
            else:
                out = (
                    f"{seconds:.1f}s to Boosthis starting \u00b7 "
                    "the app may have kept starting after that"
                )
        elif key == "responsiveness" and isinstance(axis.get("p75Ms"), (int, float)):
            count = axis.get("count")
            out = f"{round(axis['p75Ms'])} ms p75"
            if isinstance(count, int):
                out += f" \u00b7 {count} reqs"
        elif key == "resilience" and isinstance(axis.get("sampleCount"), int):
            # Four visibly distinct readings, never a blank or a bare zero: a
            # ratio · a tail too small to have one · a median too small to take
            # one from · not enough traffic yet.
            n = axis["sampleCount"]
            samples = f" \u00b7 {n} samples"
            tail = axis.get("tailRatio")
            if isinstance(tail, (int, float)):
                out = f"{tail}\u00d7 tail{samples}"
            elif isinstance(axis.get("score"), (int, float)):
                out = f"every request under {RESILIENCE_TAIL_FLOOR_MS}ms{samples}"
            elif n >= RESILIENCE_MIN_TAIL_SAMPLES:
                out = (
                    f"tail not judged \u2014 median under "
                    f"{RESILIENCE_MIN_MEDIAN_MS}ms{samples}"
                )
            else:
                out = f"warming up{samples}"
        elif key == "budget" and isinstance(axis.get("total"), int):
            out = f"{axis.get('onBudget', 0)}/{axis['total']} on budget"
        elif key == "network" and isinstance(axis.get("p75Ms"), (int, float)):
            out = f"{round(axis['p75Ms'])} ms p75 \u00b7 {axis.get('stallPct', 0)}% stalled"
            blind = _blind_clients(axis)
            if blind:
                out += f" \u00b7 {blind}"
        elif key == "baseline" and isinstance(axis.get("scoredRoutes"), int):
            # A route whose earlier window is all sub-millisecond has no
            # denominator to divide by, so it was NOT examined: an abstention
            # must never read as steadiness.
            unscored = axis.get("unscoredRoutes")
            skipped = unscored if isinstance(unscored, int) else 0
            wr = axis.get("worstRatio")
            if axis["scoredRoutes"] == 0:
                out = (
                    f"not judged \u00b7 {skipped} "
                    f"route{'' if skipped == 1 else 's'} too fast to compare"
                )
            else:
                if isinstance(wr, (int, float)) and axis.get("anomalyCount", 0):
                    out = f"worst {wr}\u00d7 \u00b7 {axis.get('anomalyCount', 0)} anomalous"
                else:
                    out = f"steady \u00b7 {axis['scoredRoutes']} routes"
                if skipped:
                    out += f" \u00b7 {skipped} too fast to compare"
            # WHICH derivation answered, and over WHICH two spans. The same
            # question is answered by a server-side window for the phone kit,
            # in the same words, so a tile that does not name its algorithm
            # leaves a reader guessing which one replied. Both spans are
            # constants of this algorithm — see docs/learned-budget-contract.md.
            out += (
                f" \u00b7 {BUDGET_BASELINE_N} post-warm-up vs newest "
                f"{BASELINE_RECENT_N} \u00b7 {BASELINE_ALGORITHM}"
            )
        elif key == "latencyFloor" and isinstance(axis.get("worstMs"), (int, float)):
            out = f"worst {round(axis['worstMs'])} ms p95 \u00b7 {axis.get('windowCount', 0)} windows"
        elif key == "idle" and isinstance(axis.get("idleBusyPct"), (int, float)):
            out = f"{axis['idleBusyPct']}% busy while idle"
        elif key == "schedulerLatency" and isinstance(axis.get("p99Ms"), (int, float)):
            out = f"{axis['p99Ms']} ms p99 scheduler wait"
        elif key == "repeatedWork" and isinstance(
            axis.get("worstRepeats"), (int, float)
        ):
            hits = int(axis.get("requestsWithRepeat", 0))
            seen = int(axis.get("watchedRequests", 0))
            if hits > 0:
                pct = axis.get("repeatTimePct", 0)
                out = (
                    f"worst {int(axis['worstRepeats'])}\u00d7 same call "
                    f"\u00b7 {hits}/{seen} requests \u00b7 {pct}% of time"
                )
            else:
                out = f"no repeats \u00b7 {seen} request{'' if seen == 1 else 's'} watched"
            blind = _blind_clients(axis)
            if blind:
                out += f" \u00b7 {blind}"
        elif key == "dbWork" and axis.get("measurable") == 1:
            # Real reading only. measurable 0 means "a driver we cannot watch is
            # the reason there is no number", which has no score and falls
            # through to the blind-client caption below.
            calls = int(axis.get("callCount", 0))
            seen = int(axis.get("watchedRequests", 0))
            out = (
                f"{axis.get('waitPct', 0)}% waiting on the database "
                f"\u00b7 {calls} quer{'y' if calls == 1 else 'ies'} "
                f"\u00b7 {seen} request{'' if seen == 1 else 's'}"
            )
            worst = axis.get("repeatWorst")
            if isinstance(worst, int) and worst > 1:
                out += f" \u00b7 worst {worst}\u00d7 same statement"
            rows = axis.get("rowsWorst")
            if isinstance(rows, int):
                out += f" \u00b7 {rows} rows in one result"
            blind = _blind_db_clients(axis)
            if blind:
                out += f" \u00b7 {blind}"
        elif key == "dbWork" and axis.get("measurable") == 0:
            # No score, and the blind spot IS the story: say it plainly rather
            # than rendering a bare "None/100".
            blind = _blind_db_clients(axis)
            out = blind or "cannot tell yet"
        elif key == "patchLag" and isinstance(axis.get("buildAgeMs"), (int, float)):
            # Build AGE only — the meter reports lag, never that the app is
            # safe/patched. e.g. "build ~12d old".
            days = int(axis["buildAgeMs"] // 86_400_000)
            out = f"build ~{days}d old"
        elif key == "cookieExposure" and isinstance(axis.get("setCount"), int):
            # EXPOSURE only — never "your cookies are safe". A clean count reads
            # as "no unsafe flags observed on the responses we watched".
            responses = axis.get("responses")
            unsafe = int(axis.get("noSecure", 0)) + int(
                axis.get("noneWithoutSecure", 0)
            )
            weak = (
                int(axis.get("noHttpOnly", 0))
                + int(axis.get("noSameSite", 0))
                + int(axis.get("oversize", 0))
            )
            watched = f"{axis['setCount']} cookie{'s' if axis['setCount'] != 1 else ''}"
            if isinstance(responses, int):
                watched += (
                    f" across {responses} response{'s' if responses != 1 else ''}"
                )
            if unsafe:
                out = f"{unsafe} unsafe of {watched}"
            elif weak:
                out = f"{weak} weak flag{'s' if weak != 1 else ''} of {watched}"
            else:
                out = f"no unsafe flags observed \u00b7 {watched}"
        else:
            out = f"{axis.get('score')}/100"
        # Host-suspend honesty note: when an axis discounted suspend artifacts
        # (a scale-to-zero host sleeping between requests), say so in the caption.
        # Mirrors the Node bubble.ts suffix.
        discounts = axis.get("suspendDiscounts")
        if isinstance(discounts, (int, float)) and discounts > 0:
            out = f"{out} \u00b7 host-suspend gaps discounted ({int(discounts)})"
        out = "".join(ch for ch in str(out) if ch >= " ")
        return out[:_PANEL_CAPTION_MAX]
    except Exception:  # noqa: BLE001
        return ""


#: WHERE SCHEDULED JOBS LIVE, as a CLOSED token. The page holds the sentences;
#: the wire carries only this word, exactly like the drop causes and the
#: promise standings. ``reported`` means this kit HAS a job-reporting call
#: (``track_job`` / ``begin_job`` / ``report_job_run``), so a job's runs, rhythm
#: and lateness are on the project's dashboard rather than on the kit's own
#: page. Never omitted and never derived from what this process has run: an
#: install with no jobs and an install with sixty send the same token, because
#: the point is that a page which cannot show jobs says so either way. The
#: cross-runtime guard derives the expected token from
#: lib/background-work-coverage.json.
#: See docs/decisions/kit-page-says-where-jobs-live.md.
PANEL_JOBS: dict[str, Any] = {"reporting": "reported"}


def compute_panel() -> dict[str, Any]:
    """Compute the closed panel shape for the bubble's full dashboard panel:
    the pulse trio plus an ordered list of meter rows, each projected onto the
    fixed ``{key, label, score, rating, caption}`` allowlist. Never includes
    route labels, URLs, or raw sample data. Fail-open: any error degrades to
    the pulse-only shape (the panel then renders hero + "measuring\u2026")."""
    pulse = compute_pulse()
    rows: list[dict[str, Any]] = []
    axes: dict[str, Any] = {}
    try:
        from boosthis import budgets
        from boosthis.snapshot_mirror import _build_axes

        axes = _build_axes(samples.summary(), budgets.all_statuses())
    except Exception:  # noqa: BLE001
        axes = {}
    try:
        ordered = list(_PANEL_CORE_AXES) + [
            k for k in axes if k not in _PANEL_CORE_AXES
        ]
        for key in ordered:
            axis = axes.get(key)
            if not isinstance(axis, dict) or not isinstance(
                axis.get("score"), (int, float)
            ):
                if (
                    key == "coldStart"
                    and isinstance(axis, dict)
                    and axis.get("rating") == "pending"
                    and isinstance(axis.get("startupMs"), (int, float))
                ):
                    rows.append(
                        {
                            "key": key,
                            "label": _panel_label(key),
                            "score": None,
                            "rating": _no_score_rating(axis),
                            "caption": _panel_caption(key, axis),
                        }
                    )
                    continue
                # An axis that uploaded an explicit can't-tell (measurable 0)
                # knows WHY it is empty, and a reason the app can act on is
                # worth a row: silence here would leave the panel showing the
                # same nothing as an app that genuinely makes no such calls,
                # which is the failure the honesty signal exists to prevent.
                blind = _blind_clients(axis) if isinstance(axis, dict) else ""
                if blind and isinstance(axis, dict) and axis.get("measurable") == 0:
                    rows.append(
                        {
                            "key": key,
                            "label": _panel_label(key),
                            "score": None,
                            "rating": _no_score_rating(axis),
                            "caption": f"can't tell \u00b7 {blind}",
                        }
                    )
                    continue
                # BASELINE abstaining is PRESENT and honest with no score ever
                # coming: every route's earlier window was too fast to divide
                # by. It is not in PANEL_EXPECTED_AXES (it needs a route's own
                # history, so a fresh app has no warming row for it), which
                # means without this the reading would vanish off the panel
                # altogether — the same silence the axis exists to break.
                if (
                    key == "baseline"
                    and isinstance(axis, dict)
                    and axis.get("measurable") == 0
                ):
                    rows.append(
                        {
                            "key": key,
                            "label": _panel_label(key),
                            "score": None,
                            "rating": _no_score_rating(axis),
                            "caption": _panel_caption(key, axis)[:_PANEL_CAPTION_MAX],
                        }
                    )
                    continue
                if key in _PANEL_CORE_AXES:
                    rows.append(
                        {
                            "key": key,
                            "label": _panel_label(key),
                            "score": None,
                            # WHICH silence this is, not just "no score yet": a
                            # reading that declared itself never-graded, or
                            # unreadable on this host, said so on the axis, and
                            # hardcoding "pending" threw that word away.
                            "rating": _no_score_rating(axis),
                            "caption": "measuring\u2026",
                        }
                    )
                continue
            score = max(0, min(100, round(float(axis["score"]))))
            rating = axis.get("rating")
            if rating not in _PANEL_RATINGS:
                rating = _rating_for_score(score)
            rows.append(
                {
                    "key": str(key)[:40],
                    "label": _panel_label(str(key)),
                    "score": score,
                    "rating": rating,
                    "caption": _panel_caption(str(key), axis),
                }
            )
    except Exception:  # noqa: BLE001
        rows = []
    # Confidence tile: projected from the four scalar confidence keys the axes
    # map carries (not a dict axis, so the loop above skipped it). Its "score"
    # stays None — confidence is a data-coverage gauge, not a 0-100 meter — and
    # the rating/caption come straight from the pre-derived scalars. Guarded so
    # a throw here never blanks the panel.
    try:
        rating = axes.get("confidenceRating")
        if rating in _PANEL_RATINGS or rating == "pending":
            mounts = axes.get("confidenceMounts")
            caption = axes.get("confidenceCaption")
            if not isinstance(caption, str) or not caption.strip():
                caption = "no samples yet"
            if isinstance(mounts, int) and mounts > 0:
                caption = f"{caption} \u00b7 {mounts} samples"
            rows.append(
                {
                    "key": "confidence",
                    "label": _panel_label("confidence"),
                    "score": None,
                    "rating": rating,
                    "caption": caption[:_PANEL_CAPTION_MAX],
                }
            )
    except Exception:  # noqa: BLE001
        pass  # keep whatever rows were assembled
    # Append a muted "warming up" row for every expected axis that has no honest
    # numeric data yet, so a fresh app shows the SAME meter set here as on the
    # web dashboard. Measured axes keep their order above; warming rows follow in
    # PANEL_EXPECTED_AXES order. Guarded — a throw here must never blank the panel.
    try:
        seen = {r["key"] for r in rows}
        for key in PANEL_EXPECTED_AXES:
            if key in seen:
                continue
            # An axis can be PRESENT and honest with no score at all: the host
            # cannot measure it, or the axis deliberately refuses to grade what
            # it saw (refusalHonesty reports an observation, never a verdict).
            # Those arrive with their own caption, and "warming up" over real
            # data would be exactly the lie this panel exists to avoid.
            own = axes.get(key)
            own_caption = own.get("caption") if isinstance(own, dict) else None
            # BASELINE abstaining is the same kind of reading, arriving with no
            # caption of its own: PRESENT, honest, and no score is ever coming —
            # every route's earlier window was too fast to divide by. Left to
            # the generic line it would say "warming up" for ever, which is the
            # steadiness claim this axis is not entitled to make.
            abstains = (
                key == "baseline"
                and isinstance(own, dict)
                and own.get("measurable") == 0
            )
            if abstains:
                caption = _panel_caption(key, own)[:_PANEL_CAPTION_MAX]
            else:
                caption = (
                    own_caption.strip()[:_PANEL_CAPTION_MAX]
                    if isinstance(own_caption, str) and own_caption.strip()
                    else _PANEL_WARMING_CAPTION_BY_KEY.get(key, _PANEL_WARMING_CAPTION)
                )
            # …and the ROW says WHICH silence it is, not only the caption. The
            # caption already told the truth; the RATING said "pending" for all
            # three, so anything that sorts, colours or counts these rows still
            # filed a permanent observation under "still measuring". One shared
            # decision with the object-axis loop above, so the two can never
            # give one reading two different words.
            rating = _no_score_rating(own)
            rows.append(
                {
                    "key": key,
                    "label": _panel_label(key),
                    "score": None,
                    "rating": rating,
                    "caption": caption,
                }
            )
    except Exception:  # noqa: BLE001
        pass  # keep whatever rows were assembled
    out = dict(pulse)
    out["axes"] = rows
    # The page consumes the project key fact from this closed payload. Read the
    # answer frozen at enable time; never resolve again here.
    try:
        from boosthis import telemetry
        from boosthis.project_key import describe_project_key_source

        # Reading the config restores the project identity through the same path
        # as the persisted credentials before this first panel payload is built.
        telemetry.get_config()
        cfg = telemetry.get_config()
        project_key = telemetry.get_active_project_key()
        out["projectKey"] = {
            "display": project_key.display,
            "source": describe_project_key_source(project_key.source),
        }
        out["installId"] = cfg.install_id if cfg is not None else None
    except Exception:  # noqa: BLE001
        out["projectKey"] = {
            "display": None,
            "source": "no project key configured",
        }
        out["installId"] = None
    # WHERE JOBS LIVE. Always sent, so the page can say that jobs are not shown
    # there whether or not this app has any — an absent section is how a
    # healthy job got read as missing.
    out["jobs"] = dict(PANEL_JOBS)
    # Always carry the already-rendered project display. The browser does not
    # receive name/code parts and therefore cannot invent a different fallback.
    try:
        out["project"] = project_display()
    except Exception:  # noqa: BLE001
        out["project"] = PROJECT_UNKNOWN_TEXT
    # A registration the server rejected, an install that never registered, or a
    # registered install uploading nothing must never read as connected/normal.
    # The key is OMITTED entirely when there is nothing to say.
    notice = compute_panel_notice()
    if notice is not None:
        out["notice"] = {"kind": notice}
        inactive_body = telemetry.get_inactive_notice()
        if inactive_body and notice == "not-registered":
            out["notice"]["body"] = inactive_body
        if notice == "uploads-failing":
            failure = telemetry.get_last_upload_failure()
            if failure is not None:
                out["notice"]["reason"] = failure[1]
            out["notice"]["lost"] = telemetry.get_dropped_upload_count()
    # Rows the server accepted the batch for and then refused. The key is
    # OMITTED entirely when nothing has been dropped, so a healthy app shows
    # nothing at all. The read carries ONLY a count + code-defined cause markers
    # — the words are literals in the injected snippet, exactly like the notice
    # banner — so no server-provided text can ever be rendered.
    drops = compute_panel_drops()
    if drops is not None:
        out["drops"] = drops
    # WHAT THE DEVELOPER SAID MUST STAY TRUE. Their own words and a closed
    # standing token; the page spells the standing out from its own literals.
    # The key is OMITTED entirely when this project holds none (or nothing has
    # arrived), so a project with no promises sees no change at all — and a
    # read that cannot answer shows nothing rather than an error.
    try:
        from boosthis.project_identity import get_kit_promises

        held = get_kit_promises()
        if held.get("items"):
            out["promises"] = held
    except Exception:  # noqa: BLE001
        pass
    return out


def compute_panel_drops() -> dict[str, Any] | None:
    """The bubble's drops line: ``{"count": int, "causes": [marker, …]}``, or
    ``None`` when the server has never reported a dropped row in this process.
    Fail-safe: any error degrades to ``None`` (no line) rather than a throw."""
    try:
        from boosthis import drop_report

        count = drop_report.get_dropped_row_count()
        if count <= 0:
            return None
        return {"count": count, "causes": drop_report.get_dropped_row_causes()}
    except Exception:  # noqa: BLE001
        return None


def compute_panel_lock() -> dict[str, Any] | None:
    """Lock descriptor served alongside (or instead of) the panel body when the
    server-authority kill-switch has rendered the kit inert. Consumed by the
    injected bubble snippet's ``paint()``:

     - ``kind: "hidden"`` → silently hide the whole bubble (env-kill / tampered /
       grace-expired — no readable state). NOT the never-checked-in case.
     - ``None`` for the never-checked-in gate ("unregistered") → no overlay, so
       the ordinary panel renders and ``compute_panel_notice()``'s "Not
       registered yet" is what the developer reads.
     - ``kind: "revoked" | "unpaid" | "paused"`` → render the BLOCKING overlay
       with the paired ``title`` + ``body`` copy (owner-facing, no action
       button, no in-kit payment). Copy is byte-equal to the RN/Node overlay.

    Returns ``None`` when the gate is "none" (active / within grace) so the
    panel serves its normal meter body. Never raises — a failure fails OPEN
    (None) so a bug in the entitlement client can never brick a paying app."""
    try:
        gate = get_entitlement_gate_kind()
        if gate == "none":
            return None
        # Never checked in is NOT a lock — see the docstring above.
        if gate == "unregistered":
            return None
        if gate == "hidden":
            return {"kind": "hidden", "title": "", "body": ""}
        detail = get_entitlement_message()
        if gate == "revoked":
            return {
                "kind": "revoked",
                "title": "Access revoked",
                "body": (
                    "This project's Boosthis access was revoked by the account "
                    "owner. Contact the owner if you think this is a mistake."
                ),
            }
        if gate == "unpaid":
            return {
                "kind": "unpaid",
                "title": "Payment required",
                "body": (
                    "The Boosthis subscription for this account is unpaid. Ask "
                    "the account owner to renew it at boosthis.com to restore "
                    "access."
                ),
            }
        # paused — calmer, reversible-pause notice; prefer the server-supplied
        # message when present (parity with the RN/Node overlay's ``detail ?? …``).
        return {
            "kind": "paused",
            "title": "Boosthis is paused",
            "body": (
                detail
                if detail
                else (
                    "The owner has paused this app from the Boosthis dashboard. "
                    "Nothing was deleted — press Reconnect there to resume."
                )
            ),
        }
    except Exception:  # noqa: BLE001
        # Fail OPEN: never brick a paying app because the gate readout raised.
        return None


def compute_panel_notice() -> str | None:
    """The panel's notice kind, or ``None`` when there is nothing to say. Ordered
    by how completely each state blocks the developer:

      ``install-id-not-uuid``  the server rejected the id outright
      ``not-registered``       the kit never registered (no key, or no reach)
      ``sharing-off``          registered, but nothing is being uploaded

    The last one is the quiet failure this notice exists to end. An install with
    sharing off still runs, still renders live local meters, and still looks
    completely healthy — while the dashboard it is supposed to feed stays empty.
    Nothing anywhere told the developer that, so the panel does.

    Fail-open by construction: any error renders the panel with NO notice rather
    than no panel — the ``notice`` key is simply omitted from the panel shape."""
    try:
        from boosthis import telemetry

        if telemetry.registration_rejected_reason() == "invalid_install_id":
            return "install-id-not-uuid"
        cfg = telemetry.get_config()
        telemetry.ask_registration()
        from boosthis.registration import get_registration_verdict

        verdict = get_registration_verdict()
        if verdict == "unregistered":
            return "not-registered"
        if verdict == "unknown" and not (cfg and (cfg.delete_token or cfg.read_token)):
            return "registration-unknown"
        # Sharing on := the code opt-in OR a dashboard switch (server_share /
        # server_full) — the SAME gate the snapshot/span uploaders ride. Off
        # means the meters stay on this screen and the dashboard stays empty.
        if not telemetry._effective_share(cfg):
            return "sharing-off"
        if telemetry.is_last_upload_attempt_failed():
            return "uploads-failing"
        return None
    except Exception:  # noqa: BLE001
        return None


def panel_body() -> dict[str, Any]:
    """The body the ``/panel`` endpoint returns. VAULT contract on-open
    enforcement edge: the bubble/panel poll IS the "dashboard opened" signal, so
    this triggers a FRESH (throttled ≤1/60s) server entitlement check, then —
    when the kit is VISIBLY LOCKED (revoked / unpaid / paused) or silently inert
    (env-kill / tampered / grace-expired / never-activated) — returns ONLY the
    lock descriptor, never the meters, so the browser snippet renders the
    blocking overlay (or hides the bubble) with nothing readable behind it."""
    _on_open_enforcement_edge()
    lock = compute_panel_lock()
    if lock is not None:
        return {"lock": lock}
    return compute_panel()


def pulse_body() -> dict[str, Any]:
    """The body the ``/pulse`` endpoint returns. Same on-open enforcement edge +
    lock gating as :func:`panel_body`; the pulse trio still rides alongside the
    lock so the badge colour reflects the locked state on the very first poll."""
    _on_open_enforcement_edge()
    lock = compute_panel_lock()
    pulse = compute_pulse()
    if lock is not None:
        out = dict(pulse)
        out["lock"] = lock
        return out
    return pulse


def _on_open_enforcement_edge() -> None:
    """Trigger the throttled on-open server entitlement check. Fully
    self-guarded — the enforcement edge must never break the panel read."""
    try:
        force_entitlement_check(False)
    except Exception:  # noqa: BLE001
        pass


def bubble_snippet(
    pulse_path: str = "/_boosthis" + PULSE_SUFFIX,
    panel_path: str | None = None,
) -> str:
    """The injected bubble script. Self-contained IIFE: closed shadow DOM, no
    globals, textContent-only writes, every step try/catch-guarded. The badge
    polls the closed pulse; tapping it opens a GATE on first ever open (connect
    card + terms box + checkbox + Agree/Decline) and, after a one-time accept,
    a FULL dashboard panel — hero score, rating badge, meter rows with bars —
    polling the closed ``<prefix>/panel`` shape. Mirrors the Node/RN kit's
    gate-first bubble panel (dark theme, orange accent); display + localStorage
    only — nothing here transmits anywhere."""
    if panel_path is None:
        panel_path = (
            pulse_path[: -len(PULSE_SUFFIX)] + PANEL_SUFFIX
            if pulse_path.endswith(PULSE_SUFFIX)
            else pulse_path
        )
    # The account auth-context endpoint sits next to /panel (…/panel →
    # …/account). Falls open to None when the panel path does not end in /panel;
    # the account card then renders the loopback-only note instead of a form.
    account_path = (
        panel_path[: -len(PANEL_SUFFIX)] + ACCOUNT_SUFFIX
        if panel_path.endswith(PANEL_SUFFIX)
        else None
    )
    # Escape the paths for embedding inside a double-quoted JS string.
    safe_panel = panel_path.replace("\\", "\\\\").replace('"', '\\"')
    safe_account = (
        None
        if account_path is None
        else account_path.replace("\\", "\\\\").replace('"', '\\"')
    )
    # The accepted-terms revision embedded into the gate. Kept byte-equal to the
    # canonical TERMS_VERSION constants (RN lib/boosthis-runtime-rn/src/ui/
    # consent.ts, web lib/boosthis-runtime-web/src/terms.ts, server
    # artifacts/api-server/src/lib/accounts.ts) that the `scripts`
    # legal-consistency test enforces. Do NOT change it here without bumping
    # those together. Python is a server kit with no consent module of its own,
    # so the literal is embedded directly into the injected snippet.
    safe_terms_version = TERMS_VERSION_LITERAL.replace("\\", "\\\\").replace(
        '"', '\\"'
    )
    # The panel mirrors the RN kit dashboard end-to-end (same DARK_THEME palette,
    # card system, hero score, rating pill, per-axis bars, legend) so the web
    # surface looks EXACTLY like the in-app performance kit. Display-only.
    return (
        "\n<script>(function(){try{"
        "if(window.__boosthisBubble)return;window.__boosthisBubble=1;"
        # ---- Terms gate state (port of the RN kit's BoosthisTermsGate) ----
        # The panel opens to a GATE on first ever open (connect card + terms box
        # + checkbox + Agree/Decline); after a one-time accept it opens straight
        # to the dashboard forever, until TERMS_VERSION changes. Display +
        # localStorage only — no new endpoints, no new data collection.
        'var TERMS_VERSION="' + safe_terms_version + '";'
        'var TERMS_KEY="boosthis:terms-accepted:v1";'
        # In-memory acceptance for this page session — the fallback when
        # localStorage is unavailable, so a user is never locked in a loop.
        "var ACCEPTED_MEM=false;"
        # VERIFIED LINK flag — set true ONLY inside the claim success handler (a
        # 200 from …/claim). Mere sign-in does not count. Exposed to the gate.
        "var LINKED=false;"
        "function termsRead(){try{"
        "var raw=window.localStorage&&window.localStorage.getItem(TERMS_KEY);"
        "if(!raw)return null;var p=JSON.parse(raw);"
        'if(p&&typeof p.version==="string")return p;return null;'
        "}catch(e){return null;}}"
        # Keyed to the CURRENT installId (when the account context has one): a
        # fresh install (new installId) does not inherit an old acceptance, so
        # the full gate flow (sign-in -> telemetry -> terms) runs again
        # (owner requirement, Jul 2026).
        "function termsAccepted(){try{"
        "if(ACCEPTED_MEM)return true;"
        "var p=termsRead();if(!p||p.version!==TERMS_VERSION)return false;"
        'var iid=(typeof ACTX!=="undefined"&&ACTX&&typeof ACTX.installId==="string")?ACTX.installId:null;'
        "if(iid===null)return true;"
        "return (p.installId||null)===iid;"
        "}catch(e){return ACCEPTED_MEM;}}"
        "function termsWrite(){try{ACCEPTED_MEM=true;"
        'var iid=(typeof ACTX!=="undefined"&&ACTX&&typeof ACTX.installId==="string")?ACTX.installId:null;'
        "if(window.localStorage)window.localStorage.setItem(TERMS_KEY,"
        "JSON.stringify({version:TERMS_VERSION,at:Date.now(),installId:iid}));"
        "}catch(e){}}"
        # Sign-out wipes the acceptance so the WHOLE gate flow runs again
        # (owner requirement, Jul 2026).
        "function termsClear(){try{ACCEPTED_MEM=false;"
        "if(window.localStorage)window.localStorage.removeItem(TERMS_KEY);"
        "}catch(e){}}"
        'var host=document.createElement("div");'
        'host.style.cssText="position:fixed;bottom:18px;right:18px;z-index:2147483000;";'
        'var root=host.attachShadow?host.attachShadow({mode:"closed"}):host;'
        # RN kit DARK_THEME palette — keep byte-identical across all server kits.
        'var T={bg:"#0b0c10",card:"#15171c",br:"#262932",fg:"#e6e7eb",mut:"#8b8f99",pri:"#f97316"};'
        'var COLORS={good:"#4ade80","needs-work":"#fbbf24",poor:"#f87171"};'
        'var RL={good:"GOOD","needs-work":"NEEDS WORK",poor:"POOR"};'
        'var btn=document.createElement("button");'
        'btn.type="button";btn.setAttribute("aria-label","Boosthis performance bubble");'
        'btn.style.cssText="width:48px;height:48px;border-radius:50%;background:"+T.bg+";'
        'border:2.5px solid "+T.mut+";color:"+T.pri+";font-size:22px;line-height:1;'
        'cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.4);display:flex;'
        'align-items:center;justify-content:center;padding:0;font-family:system-ui,sans-serif;";'
        # Brand mark — the real Boosthis icon (navy "B" + orange bolt) as an <img>
        # child, so the bubble reads as the app icon (parity with the RN kit).
        'var bImg=document.createElement("img");'
        "bImg.src=" + json.dumps(BOOSTHIS_ICON_URI) + ";"
        'bImg.alt="";bImg.draggable=false;'
        'bImg.style.cssText="width:34px;height:34px;border-radius:50%;pointer-events:none;display:block;";'
        "btn.appendChild(bImg);"
        'var panel=document.createElement("div");'
        'panel.setAttribute("role","dialog");panel.setAttribute("aria-label","Boosthis dashboard");'
        # A PHONE IS A HOST APP'S FIRST SCREEN TOO. A flat 300px panel is wider
        # than the usable width of a small phone once the host's own 18px
        # margins are taken off, so the panel is capped against the viewport as
        # well. What then does not fit scrolls — and SAYS it scrolls: iOS and
        # Android draw an overlay scrollbar that only appears once you are
        # already swiping, so a scrollbar alone is not a cue. The background
        # carries two `local` cover layers and two `scroll` shadows, which is
        # the one cue an inline cssText can express (no ::-webkit-scrollbar
        # rule is reachable here). The plain `background` above it is the
        # fallback: an engine that cannot parse the gradient list drops that
        # whole declaration, and without the first one the panel would render
        # transparent.
        'panel.style.cssText="display:none;position:absolute;bottom:56px;right:0;'
        'width:300px;max-width:calc(100vw - 36px);max-height:72vh;overflow:auto;'
        'overscroll-behavior:contain;-webkit-overflow-scrolling:touch;'
        'scrollbar-width:thin;scrollbar-color:"+T.mut+" transparent;'
        'background:"+T.bg+";'
        'background:linear-gradient("+T.bg+" 50%,rgba(0,0,0,0)) top/100% 20px no-repeat local,'
        'linear-gradient(rgba(0,0,0,0),"+T.bg+" 50%) bottom/100% 20px no-repeat local,'
        'radial-gradient(farthest-side at 50% 0,rgba(0,0,0,.6),rgba(0,0,0,0)) top/100% 10px no-repeat scroll,'
        'radial-gradient(farthest-side at 50% 100%,rgba(0,0,0,.6),rgba(0,0,0,0)) bottom/100% 10px no-repeat scroll,'
        '"+T.bg+";color:"+T.fg+";'
        'border:1px solid "+T.br+";border-radius:20px;padding:12px;'
        'font:12px/1.5 system-ui,sans-serif;box-shadow:0 10px 28px rgba(0,0,0,.5);text-align:left;";'
        # ---- Blocking lock overlay (VAULT contract, owner UX) ----
        # When the server verdict is REVOKED / UNPAID / PAUSED the kit must not
        # silently vanish: the bubble stays visible and opens straight to a
        # BLOCKING full-cover overlay. The overlay dims + BLURS everything behind
        # it (CSS backdrop-filter — the cheap web approximation, no deps) and,
        # being an opaque full-cover layer that absorbs pointer events, nothing
        # of the meters can be read or tapped through. Copy is closed + friendly,
        # keyed off the server-computed lock kind; no server internals, no action
        # button, no in-kit payment. Kept byte-equal to the RN/Node overlay.
        'var lockEl=document.createElement("div");'
        'lockEl.setAttribute("role","alertdialog");'
        'lockEl.setAttribute("aria-label","Boosthis locked");'
        'lockEl.style.cssText="display:none;position:absolute;top:0;left:0;right:0;bottom:0;'
        "z-index:10;box-sizing:border-box;border-radius:20px;padding:32px 22px;"
        "flex-direction:column;align-items:center;justify-content:center;text-align:center;"
        # Dark scrim + blur of everything painted behind the overlay.
        'background:rgba(11,12,16,.92);'
        '-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);";'
        'var lockTitle=document.createElement("div");'
        'lockTitle.style.cssText="font-size:17px;font-weight:700;color:"+T.fg+";margin-bottom:8px;";'
        'var lockBody=document.createElement("div");'
        'lockBody.style.cssText="font-size:13px;line-height:1.5;color:"+T.mut+";max-width:240px;";'
        "lockEl.appendChild(lockTitle);lockEl.appendChild(lockBody);"
        # showLock(lock) — render the overlay for a {kind,title,body} descriptor
        # and block the panel body behind it. hideLock() — restore the normal
        # panel. The overlay absorbs pointer events (default) so no tap passes
        # through.
        "function showLock(lock){try{"
        'lockTitle.textContent=String(lock&&lock.title||"Boosthis is locked");'
        'lockBody.textContent=String(lock&&lock.body||"");'
        'lockEl.style.display="flex";'
        # Freeze scrolling behind the cover so the meters cannot be scrolled to.
        'panel.style.overflow="hidden";'
        "}catch(e){}}"
        "function hideLock(){try{"
        'lockEl.style.display="none";panel.style.overflow="auto";'
        "}catch(e){}}"
        "function card(){"
        'var c=document.createElement("div");'
        'c.style.cssText="background:"+T.card+";border:1px solid "+T.br+";border-radius:16px;padding:12px 14px;margin-bottom:10px;";'
        "return c;}"
        "function cardTitle(tx){"
        'var d=document.createElement("div");'
        'd.style.cssText="font-size:13px;font-weight:700;color:"+T.fg+";";d.textContent=tx;return d;}'
        "function cardSub(tx){"
        'var d=document.createElement("div");'
        'd.style.cssText="font-size:10.5px;color:"+T.mut+";margin-top:1px;margin-bottom:2px;";d.textContent=tx;return d;}'
        # Hero — app-kit hero card: name, runtime subtitle, big score, rating pill.
        "var hero=card();"
        'var hName=document.createElement("div");'
        'hName.style.cssText="font-size:16px;font-weight:800;color:"+T.fg+";";hName.textContent="Boosthis";'
        'var hSub=document.createElement("div");'
        'hSub.style.cssText="font-size:11px;color:"+T.mut+";";hSub.textContent="'
        + RUNTIME_PANEL_SUBTITLE
        + '";'
        # WHICH PROJECT this kit feeds. It is set only with textContent; the
        # developer-authored name can never become markup.
        'var hProj=document.createElement("div");'
        'hProj.style.cssText="font-size:11px;color:"+T.mut+";margin-top:3px;word-break:break-word;";'
        'hProj.textContent="'
        + PROJECT_LABEL
        + ": "
        + PROJECT_UNKNOWN_TEXT
        + '";'
        'var hInstall=document.createElement("div");'
        'hInstall.style.cssText=hProj.style.cssText;'
        'hInstall.textContent="'
        + INSTALL_ID_LABEL
        + ': '
        + INSTALL_ID_UNKNOWN_TEXT
        + '";'
        'var gw=document.createElement("div");gw.style.cssText="text-align:center;padding:10px 0 2px;";'
        'var num=document.createElement("div");'
        'num.style.cssText="font-size:46px;font-weight:800;line-height:1;letter-spacing:-1px;color:"+T.fg+";";'
        'num.textContent="\\u2013";'
        'var of=document.createElement("div");'
        'of.style.cssText="font-size:11px;color:"+T.mut+";margin-top:2px;";of.textContent="/ 100";'
        'var badge=document.createElement("div");'
        'badge.style.cssText="display:inline-flex;align-items:center;gap:5px;margin-top:8px;padding:3px 12px;'
        'border-radius:999px;font-size:10px;font-weight:700;letter-spacing:.6px;'
        'background:"+T.br+";color:"+T.mut+";border:1px solid "+T.br+";";'
        'var bDot=document.createElement("span");'
        'bDot.style.cssText="width:6px;height:6px;border-radius:50%;background:"+T.mut+";";'
        'var bTxt=document.createElement("span");bTxt.textContent="MEASURING";'
        "badge.appendChild(bDot);badge.appendChild(bTxt);"
        'var stat=document.createElement("div");'
        'stat.style.cssText="text-align:center;margin-top:7px;font-size:10.5px;color:"+T.mut+";";'
        'stat.textContent="measuring \\u2014 handle a few requests";'
        'var hCap=document.createElement("div");'
        'hCap.style.cssText="text-align:center;margin-top:5px;font-size:10px;line-height:1.4;color:"+T.mut+";";'
        'hCap.textContent="'
        + SCORE_CAPTION
        + '";'
        "gw.appendChild(num);gw.appendChild(of);"
        "hero.appendChild(hName);hero.appendChild(hSub);hero.appendChild(hProj);hero.appendChild(hInstall);hero.appendChild(gw);"
        'var bWrap=document.createElement("div");bWrap.style.cssText="text-align:center;";bWrap.appendChild(badge);'
        "hero.appendChild(bWrap);hero.appendChild(stat);hero.appendChild(hCap);"
        # Score breakdown — core axes with bars + honest formula, like the app kit.
        "var bd=card();"
        'bd.appendChild(cardTitle("Score breakdown"));'
        'bd.appendChild(cardSub("this process \\u00b7 lower is better"));'
        'var bdRows=document.createElement("div");bd.appendChild(bdRows);'
        'var formula=document.createElement("div");'
        'formula.style.cssText="font-size:10px;color:"+T.mut+";margin-top:8px;";'
        'formula.textContent="score = p95 vs TTI budget \\u00b7 good\\u226585 \\u00b7 needs-work\\u226560";'
        "bd.appendChild(formula);"
        # Runtime health — the environment meters card.
        "var hl=card();"
        'hl.appendChild(cardTitle("Runtime health"));'
        'hl.appendChild(cardSub("environment meters \\u00b7 shown when measurable"));'
        'var hlRows=document.createElement("div");hl.appendChild(hlRows);'
        # Legend — same rating legend strip as the app kit.
        "var lg=card();"
        'lg.style.cssText+="display:flex;flex-wrap:wrap;gap:6px 12px;font-size:10.5px;color:"+T.mut+";";'
        "function lgItem(color,tx){"
        'var s=document.createElement("span");s.style.cssText="display:inline-flex;align-items:center;gap:5px;";'
        'var d=document.createElement("span");d.style.cssText="width:7px;height:7px;border-radius:50%;background:"+color+";";'
        'var t=document.createElement("span");t.textContent=tx;'
        "s.appendChild(d);s.appendChild(t);return s;}"
        'lg.appendChild(lgItem(COLORS.good,"Good \\u226585"));'
        'lg.appendChild(lgItem(COLORS["needs-work"],"Needs work \\u226560"));'
        'lg.appendChild(lgItem(COLORS.poor,"Poor <60"));'
        # "Connect to your account" — a port of the web kit's account card, styled
        # with the panel's T palette (card #15171c, orange buttons). Auth context
        # comes from the loopback-only /account endpoint fetched when the panel
        # opens; nothing is persisted (the session token lives only in this IIFE's
        # memory). Every listener + async path is try/catch guarded and NEVER
        # throws into the host page. All dynamic values use textContent.
        "var acct=card();"
        'var acctBody=document.createElement("div");acct.appendChild(acctBody);'
        # In-memory auth state — never persisted.
        "var ACTX={endpoint:null,installId:null,deleteToken:null,fullTelemetry:false,available:false};"
        "var SESS=null;"  # {token,email} once signed in
        # Full-telemetry toggle state. TELE_KNOWN flips true once a verified
        # claim succeeds (that is when we hold BOTH the session and the delete
        # token the /telemetry endpoint needs); TELE_ON is seeded from the claim
        # response (falling back to ACTX.fullTelemetry, the kit's effective
        # mode); TELE_BUSY guards the in-flight POST.
        "var TELE_ON=false;var TELE_KNOWN=false;var TELE_BUSY=false;"
        # Claim banner — the friendly line the signed-in card shows under the Link
        # button. Held in shared state (not on the message node) so it survives the
        # re-render that reveals the Full-telemetry toggle, and so the manual tap
        # and the automatic link-on-sign-in say exactly the same thing.
        "var CLAIM_MSG=null;var CLAIM_MSG_OK=false;"
        # The honest fallback line when the server rejects the OWNERSHIP proof
        # (401 reason install_token / 403). It no longer claims linking is
        # impossible without this page's credential — an account that owns this
        # project's invite key can link it from any browser.
        'var ACCT_LINK_PROOF_MSG="Couldn\\u2019t link this project. Linking needs either this project\\u2019s own connection credential (available in the session where it registered) or an account that owns this project\\u2019s project key \\u2014 sign in with that account, or link it from your dashboard.";'
        # Claim bookkeeping: CLAIM_BUSY guards the in-flight call (manual tap OR
        # the automatic link-on-sign-in), AUTO_CLAIM_FOR remembers the (session,
        # install) pair already auto-attempted so the automatic path fires exactly
        # once. A failed auto-link simply leaves the manual "Link this project"
        # button in place with the same code-mapped message.
        "var CLAIM_BUSY=false;var AUTO_CLAIM_FOR=null;"
        'var acctView={kind:"signed-out"};'  # signed-out | otp | signed-in
        "var acctBusy=false;"
        'function acctJoin(ep,p){return String(ep).replace(/\\/+$/,"")+p;}'
        "function acctMkInput(type,ph,ac){"
        'var i=document.createElement("input");i.type=type;i.placeholder=ph;'
        'if(ac)i.setAttribute("autocomplete",ac);'
        'i.style.cssText="width:100%;box-sizing:border-box;margin-bottom:8px;padding:8px 10px;'
        'font-size:12.5px;background:"+T.bg+";color:"+T.fg+";border:1px solid "+T.br+";'
        'border-radius:8px;outline:none;font-family:inherit;";return i;}'
        "function acctMkBtn(tx){"
        'var b=document.createElement("button");b.type="button";b.textContent=tx;'
        'b.style.cssText="width:100%;padding:9px 10px;font-size:12.5px;font-weight:700;'
        'background:"+T.pri+";color:"+T.bg+";border:none;border-radius:8px;cursor:pointer;font-family:inherit;";'
        "return b;}"
        "function acctMkGhost(tx){"
        "var b=acctMkBtn(tx);"
        'b.style.background="transparent";b.style.color=T.mut;'
        'b.style.border="1px solid "+T.br;b.style.marginTop="8px";return b;}'
        "function acctMkMsg(){"
        'var d=document.createElement("div");'
        'd.style.cssText="margin-top:8px;font-size:10.5px;line-height:1.5;color:"+T.mut+";";return d;}'
        "function acctSetMsg(el,tx,color){try{el.textContent=tx;el.style.color=color||T.mut;}catch(e){}}"
        # Fetch wrapper — plain fetch, no persistence, errors normalized to a code.
        "function acctFetch(url,init){"
        "return fetch(url,init).then(function(r){return r;});}"
        # ---- Renderers ----
        "function acctRender(){try{"
        "while(acctBody.firstChild)acctBody.removeChild(acctBody.firstChild);"
        'var title=document.createElement("div");'
        'title.style.cssText="font-size:13px;font-weight:700;color:"+T.fg+";";'
        'title.textContent="Connect to your account";acctBody.appendChild(title);'
        "if(!ACTX.available||!ACTX.endpoint){"
        'var s=document.createElement("div");'
        's.style.cssText="font-size:10.5px;color:"+T.mut+";margin-top:6px;line-height:1.5;";'
        's.textContent="Sign-in is available when this page is opened on the dev machine.";'
        "acctBody.appendChild(s);return;}"
        'if(acctView.kind==="signed-in"){acctRenderSignedIn();return;}'
        'if(acctView.kind==="otp"){acctRenderOtp();return;}'
        "acctRenderSignedOut();"
        "}catch(e){}}"
        # signed-out: email + password → Sign in
        "function acctRenderSignedOut(){"
        'var s=document.createElement("div");'
        's.style.cssText="font-size:10.5px;color:"+T.mut+";margin:6px 0 10px;line-height:1.5;";'
        's.textContent="Sign in with your Boosthis dashboard account to link this project directly \\u2014 no project-key matching needed.";'
        "acctBody.appendChild(s);"
        'var email=acctMkInput("email","Email","username");acctBody.appendChild(email);'
        'var pw=acctMkInput("password","Password","current-password");acctBody.appendChild(pw);'
        'var btn=acctMkBtn("Sign in");acctBody.appendChild(btn);'
        "var msg=acctMkMsg();acctBody.appendChild(msg);"
        'btn.addEventListener("click",function(){try{'
        "if(acctBusy)return;"
        'var em=(email.value||"").trim();var pass=pw.value||"";'
        'if(!em||!pass){acctSetMsg(msg,"Enter your email and password.",COLORS.poor);return;}'
        'acctBusy=true;btn.disabled=true;btn.textContent="Signing in\\u2026";acctSetMsg(msg,"",T.mut);'
        'acctFetch(acctJoin(ACTX.endpoint,"/auth/login"),{method:"POST",'
        'headers:{"content-type":"application/json",accept:"application/json"},'
        "body:JSON.stringify({email:em,password:pass})}).then(function(r){"
        'pw.value="";'
        'if(r.status===401){throw "Wrong email or password.";}'
        'if(r.status===429){throw "Too many attempts. Try again later.";}'
        'if(!r.ok){throw "Login failed. Please try again.";}'
        "return r.json();}).then(function(b){"
        'if(b&&b.otpRequired===true&&typeof b.challengeToken==="string"){'
        'acctView={kind:"otp",challengeToken:b.challengeToken,email:em};acctRender();return;}'
        'var tok=b&&typeof b.token==="string"?b.token:null;'
        'var ae=b&&b.account&&typeof b.account.email==="string"?b.account.email:em;'
        'if(!tok){throw "Login failed. Please try again.";}'
        'SESS={token:tok,email:ae};acctView={kind:"signed-in"};acctRender();'
        "}).catch(function(err){"
        'acctBusy=false;try{btn.disabled=false;btn.textContent="Sign in";}catch(e){}'
        'acctSetMsg(msg,typeof err==="string"?err:"Something went wrong. Please try again.",COLORS.poor);'
        "return;}).then(function(){acctBusy=false;});"
        "}catch(e){acctBusy=false;}});}"
        # otp: 6-digit code + resend
        "function acctRenderOtp(){"
        'var s=document.createElement("div");'
        's.style.cssText="font-size:10.5px;color:"+T.mut+";margin:6px 0 10px;line-height:1.5;";'
        's.textContent="We emailed a 6-digit code to "+acctView.email+". Enter it below to finish signing in.";'
        "acctBody.appendChild(s);"
        'var code=acctMkInput("text","6-digit code","one-time-code");code.inputMode="numeric";acctBody.appendChild(code);'
        'var btn=acctMkBtn("Verify");acctBody.appendChild(btn);'
        'var resend=acctMkGhost("Resend code");acctBody.appendChild(resend);'
        "var msg=acctMkMsg();acctBody.appendChild(msg);"
        'btn.addEventListener("click",function(){try{'
        'if(acctBusy)return;var cv=(code.value||"").trim();'
        'if(!cv){acctSetMsg(msg,"Enter the code from your email.",COLORS.poor);return;}'
        'acctBusy=true;btn.disabled=true;btn.textContent="Verifying\\u2026";acctSetMsg(msg,"",T.mut);'
        'acctFetch(acctJoin(ACTX.endpoint,"/auth/login/otp"),{method:"POST",'
        'headers:{"content-type":"application/json",accept:"application/json"},'
        "body:JSON.stringify({challengeToken:acctView.challengeToken,code:cv})}).then(function(r){"
        'if(r.status===401){throw "That code isn\\u2019t right. Check the newest email we sent and try again.";}'
        'if(r.status===400){acctView={kind:"signed-out"};acctRender();throw "__handled__";}'
        'if(r.status===429){throw "Too many attempts. Try again later.";}'
        'if(!r.ok){throw "Couldn\\u2019t verify the code. Please try again.";}'
        "return r.json();}).then(function(b){"
        'var tok=b&&typeof b.token==="string"?b.token:null;'
        'var ae=b&&b.account&&typeof b.account.email==="string"?b.account.email:acctView.email;'
        'if(!tok){throw "Couldn\\u2019t verify the code. Please try again.";}'
        'SESS={token:tok,email:ae};acctView={kind:"signed-in"};acctRender();'
        "}).catch(function(err){acctBusy=false;"
        'if(err==="__handled__")return;'
        'try{btn.disabled=false;btn.textContent="Verify";}catch(e){}'
        'acctSetMsg(msg,typeof err==="string"?err:"Something went wrong. Please try again.",COLORS.poor);'
        "}).then(function(){acctBusy=false;});"
        "}catch(e){acctBusy=false;}});"
        'resend.addEventListener("click",function(){try{'
        'if(acctBusy)return;acctBusy=true;resend.disabled=true;acctSetMsg(msg,"",T.mut);'
        'acctFetch(acctJoin(ACTX.endpoint,"/auth/login/otp/resend"),{method:"POST",'
        'headers:{"content-type":"application/json",accept:"application/json"},'
        "body:JSON.stringify({challengeToken:acctView.challengeToken})}).then(function(r){"
        'if(r.ok){acctSetMsg(msg,"A new code is on its way.",COLORS.good);return;}'
        'if(r.status===400){acctView={kind:"signed-out"};acctRender();return;}'
        'if(r.status===429){throw "Too many resend requests. Please wait a moment.";}'
        'if(r.status===502){throw "We couldn\\u2019t send a new code right now. Try again in a few minutes.";}'
        'throw "Couldn\\u2019t resend the code. Please try again.";'
        "}).catch(function(err){"
        'if(typeof err==="string")acctSetMsg(msg,err,COLORS.poor);'
        "}).then(function(){acctBusy=false;try{resend.disabled=false;}catch(e){}});"
        "}catch(e){acctBusy=false;}});}"
        # signed-in: who + optional claim + sign out
        "function acctRenderSignedIn(){"
        'var who=document.createElement("div");'
        'who.style.cssText="font-size:12.5px;color:"+T.fg+";font-weight:600;";'
        'who.textContent="Signed in as "+(SESS?SESS.email:"");acctBody.appendChild(who);'
        "var msg=acctMkMsg();"
        # The link step needs an installId and nothing else from THIS page. The
        # install delete token is a BONUS proof, not a precondition: when it is
        # absent the claim still fires without the X-Boosthis-Install-Token
        # header and the SERVER decides, accepting the signed-in account when
        # it owns the invite key this project registered under (the same
        # dual-signal ownership the dashboard already uses). Requiring the
        # token here is what deadlocked every session after the first.
        "var canClaim=!!ACTX.installId;"
        "if(canClaim){"
        'var cs=document.createElement("div");'
        'cs.style.cssText="font-size:10.5px;color:"+T.mut+";margin:8px 0 10px;line-height:1.5;";'
        'cs.textContent="Link this project to your account so it appears in your dashboard.";'
        "acctBody.appendChild(cs);"
        # One button, three honest states: idle (tap to link), in-flight (the
        # automatic link-on-sign-in or a manual tap), and done (already verified —
        # disabled, because the work happened without the developer lifting a
        # finger).
        'var claim=acctMkBtn(LINKED?"Linked":(CLAIM_BUSY?"Linking\\u2026":"Link this project"));'
        "claim.disabled=LINKED||CLAIM_BUSY;"
        "acctBody.appendChild(claim);acctBody.appendChild(msg);"
        # The last claim outcome lives in shared state so it survives the
        # re-render that reveals the Full-telemetry toggle — and so a manual tap
        # and the automatic link-on-sign-in say exactly the same thing.
        "if(CLAIM_MSG)acctSetMsg(msg,CLAIM_MSG,CLAIM_MSG_OK?COLORS.good:COLORS.poor);"
        'claim.addEventListener("click",function(){try{'
        'if(acctBusy||CLAIM_BUSY||LINKED)return;acctBusy=true;claim.disabled=true;claim.textContent="Linking\\u2026";acctSetMsg(msg,"",T.mut);'
        "acctClaimRun(function(res){acctBusy=false;"
        "CLAIM_MSG=res.msg;CLAIM_MSG_OK=res.ok===true;try{acctRender();}catch(e){}});"
        "}catch(e){acctBusy=false;}});"
        # Auto-link on sign-in: the developer is signed in AND both proofs are in
        # hand, so fire the SAME claim call the button fires — no tap, no reload.
        # On 200 the re-render shows the Full-telemetry toggle instantly; on
        # failure the manual button above stays exactly where it is with the
        # code-mapped message, so 401/404/409 behave as they always did.
        "acctAutoClaim();"
        "}else{"
        'var ns=document.createElement("div");'
        'ns.style.cssText="font-size:10.5px;color:"+T.mut+";margin:8px 0 0;line-height:1.5;";'
        'ns.textContent="This project hasn\\u2019t registered with Boosthis yet, so it can\\u2019t be linked from here.";'
        "acctBody.appendChild(ns);acctBody.appendChild(msg);}"
        # Full-telemetry toggle — rendered only once the server has VERIFIED
        # the project link (LINKED). The delete token is no longer part of the
        # condition: /installs/:id/telemetry accepts the account session ALONE
        # for an install linked to that account (its session-only mode, the
        # same proof the dashboard switch uses), so the toggle works in a later
        # session that never saw the delete token. This server kit stores the
        # choice server-side; its uploader picks it up on the next check-in.
        "if(LINKED&&ACTX.installId){acctTeleToggle();}"
        'var out=acctMkGhost("Sign out");acctBody.appendChild(out);'
        'out.addEventListener("click",function(){try{'
        "if(acctBusy)return;acctBusy=true;out.disabled=true;"
        "var tok=SESS?SESS.token:null;"
        # Sign-out restarts the WHOLE gate flow: wipe the terms acceptance +
        # LINKED flag, reset the checkbox, re-render the card, and gateSync()
        # swaps the panel back to the gate (owner requirement, Jul 2026).
        'function done(){SESS=null;acctView={kind:"signed-out"};acctBusy=false;'
        # A new sign-in must auto-link (and re-verify) from scratch: forget the
        # attempted pair and the last claim banner.
        "AUTO_CLAIM_FOR=null;CLAIM_MSG=null;CLAIM_MSG_OK=false;TELE_KNOWN=false;"
        "termsClear();LINKED=false;gateChecked=false;"
        'try{if(gCheckBox){gCheckBox.style.borderColor=T.br;gCheckBox.style.background="transparent";'
        'if(gCheckBox.firstChild)gCheckBox.firstChild.style.display="none";}}catch(e){}'
        "acctRender();try{gateSync();}catch(e){}}"
        'if(tok){acctFetch(acctJoin(ACTX.endpoint,"/auth/logout"),{method:"POST",headers:{authorization:"Bearer "+tok}}).then(done,done);}else{done();}'
        "}catch(e){acctBusy=false;}});}"
        # ---- Shared claim runner ----------------------------------------------
        # ONE code path for BOTH the manual "Link this project" tap and the
        # automatic link-on-sign-in. The account session (Bearer SESS.token) is
        # ALWAYS required. The install delete token rides along as
        # X-Boosthis-Install-Token WHEN THIS PAGE HAS IT — otherwise the header
        # is OMITTED entirely (never sent empty, which would read as a failed
        # proof) and the server falls back to its second sanctioned ownership
        # proof: the signed-in account owning the invite key this project
        # registered with. `cb` receives {ok,msg} where msg is the exact
        # friendly, code-mapped line the button always showed (401/404/409/429
        # wording untouched).
        'function acctClaimKey(){return (SESS?SESS.token:"")+"\\u0001"+(ACTX.installId||"");}'
        'function acctClaimHeaders(){var h={accept:"application/json",authorization:"Bearer "+SESS.token};'
        'if(ACTX.deleteToken)h["x-boosthis-install-token"]=ACTX.deleteToken;return h;}'
        'function acctTeleHeaders(){var h=acctClaimHeaders();h["content-type"]="application/json";return h;}'
        "function acctClaimRun(cb){try{"
        "if(CLAIM_BUSY)return;"
        "function done(res){CLAIM_BUSY=false;try{cb(res);}catch(e){}}"
        'if(!SESS||!ACTX.endpoint||!ACTX.installId){done({ok:false,msg:"Linking failed. Please try again."});return;}'
        "CLAIM_BUSY=true;AUTO_CLAIM_FOR=acctClaimKey();"
        'acctFetch(acctJoin(ACTX.endpoint,"/installs/"+encodeURIComponent(ACTX.installId)+"/claim"),{method:"POST",'
        "headers:acctClaimHeaders()}).then(function(r){"
        "if(r.status===200){return r.json().then(function(b){return b||{};},function(){return {};}).then(function(b){"
        "var linked=b&&b.linked===true;"
        # A 200 from the claim endpoint is a VERIFIED LINK (linked:true or
        # already-linked). Set the LINKED flag and refresh the gate so
        # needsConnect clears and "I Agree & Continue" can enable.
        "LINKED=true;try{gateSync();}catch(e){}"
        # Seed the telemetry toggle from the claim response (falling back to the
        # kit's current effective mode reported by the account context).
        'TELE_ON=typeof b.fullTelemetry==="boolean"?b.fullTelemetry:(ACTX.fullTelemetry===true);'
        "TELE_KNOWN=true;"
        'done({ok:true,msg:linked?"Linked. This project now appears in your dashboard.":"This project is already linked to your account."});'
        "return;});}"
        # 401/403 = the OWNERSHIP proof failed, and the honest fallback line
        # says what would actually work. Linking is NOT impossible without this
        # page's credential: signing in as the account that owns this project's
        # invite key links it too (the server accepts that proof).
        'if(r.status===401){return r.json().then(function(b){return b&&b.reason;},function(){return null;}).then(function(rs){'
        'throw rs==="install_token"?ACCT_LINK_PROOF_MSG:"Your session expired. Sign in again.";});}'
        "if(r.status===403){throw ACCT_LINK_PROOF_MSG;}"
        'if(r.status===404){throw "This project isn\\u2019t registered yet. Make sure telemetry is enabled.";}'
        'if(r.status===409){throw "This project is already linked to a different account.";}'
        'if(r.status===429){throw "Too many attempts. Try again later.";}'
        'throw "Linking failed. Please try again.";'
        "}).catch(function(err){"
        'done({ok:false,msg:typeof err==="string"?err:"Linking failed. Please try again."});'
        "}).then(function(){CLAIM_BUSY=false;});"
        "}catch(e){CLAIM_BUSY=false;}}"
        # ---- Auto-link on sign-in ---------------------------------------------
        # The moment the developer is signed in and the project has registered
        # (installId known), fire the claim automatically — no "Link this
        # project" tap, no page reload. The delete token is NOT required:
        # without it the header is omitted and the server decides via key
        # ownership. Runs exactly once per (session, install) pair; a manual tap
        # marks the pair too, so a failure is never silently retried in a loop.
        # On 200 the re-render reveals the Full-telemetry toggle instantly; on
        # failure the manual Link button stays put with the code-mapped message.
        "function acctAutoClaim(){try{"
        "if(LINKED||CLAIM_BUSY)return;"
        "if(!SESS||!ACTX.endpoint||!ACTX.installId)return;"
        "var k=acctClaimKey();if(AUTO_CLAIM_FOR===k)return;AUTO_CLAIM_FOR=k;"
        "acctClaimRun(function(res){CLAIM_MSG=res.msg;CLAIM_MSG_OK=res.ok===true;"
        "try{acctRender();}catch(e){}});"
        "}catch(e){}}"
        # ---- Full-telemetry toggle (rendered by acctRenderSignedIn once LINKED)
        # Consent-flavored: a label, a sub-line explaining ON, and an ON/OFF
        # pill. Tapping POSTs { fullTelemetry } to the telemetry endpoint with
        # the account session + install delete token (the same dual-proof the
        # claim used). Friendly, code-mapped errors — never a raw dump. This
        # server kit stores the choice server-side; its own uploader reads the
        # directive on its next check-in.
        "function acctTeleToggle(){try{"
        'var wrap=document.createElement("div");'
        'wrap.style.cssText="margin-top:12px;padding-top:12px;border-top:1px solid "+T.br+";";'
        'var row=document.createElement("div");'
        'row.style.cssText="display:flex;align-items:center;justify-content:space-between;gap:10px;";'
        'var lw=document.createElement("div");lw.style.cssText="flex:1;min-width:0;";'
        'var lb=document.createElement("div");'
        'lb.style.cssText="font-size:12.5px;font-weight:700;color:"+T.fg+";";lb.textContent="Full telemetry";'
        'var sub=document.createElement("div");'
        'sub.style.cssText="font-size:10.5px;color:"+T.mut+";margin-top:2px;line-height:1.5;";'
        'sub.textContent="Upload the complete meter picture so your dashboard and AI can see performance. Off = private mode.";'
        "lw.appendChild(lb);lw.appendChild(sub);"
        'var sw=document.createElement("button");sw.type="button";'
        'sw.setAttribute("role","switch");'
        'sw.style.cssText="flex:0 0 auto;width:46px;height:26px;border-radius:999px;border:none;cursor:pointer;position:relative;padding:0;transition:background .15s;font-family:inherit;";'
        'var knob=document.createElement("span");'
        'knob.style.cssText="position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:left .15s;";'
        "sw.appendChild(knob);"
        'var pend=document.createElement("span");'
        'pend.style.cssText="font-size:9px;font-weight:700;color:"+T.mut+";margin-left:8px;display:none;";pend.textContent="\\u2026";'
        "row.appendChild(lw);row.appendChild(sw);row.appendChild(pend);wrap.appendChild(row);"
        "var tmsg=acctMkMsg();wrap.appendChild(tmsg);"
        "function reflect(){try{"
        "sw.style.background=TELE_ON?T.pri:T.br;"
        'sw.setAttribute("aria-checked",TELE_ON?"true":"false");'
        'sw.setAttribute("aria-label",(TELE_ON?"Full telemetry on":"Full telemetry off"));'
        'knob.style.left=TELE_ON?"23px":"3px";'
        "}catch(e){}}"
        "reflect();"
        'sw.addEventListener("click",function(){try{'
        "if(TELE_BUSY||acctBusy)return;"
        'var next=!TELE_ON;TELE_BUSY=true;acctSetMsg(tmsg,"",T.mut);'
        'pend.style.display="inline";sw.style.opacity="0.6";'
        'acctFetch(acctJoin(ACTX.endpoint,"/installs/"+encodeURIComponent(ACTX.installId)+"/telemetry"),{method:"POST",'
        # Same header rule as the claim: send the install token only when this
        # page actually has it. /installs/:id/telemetry already accepts the
        # account session ALONE once the install is linked to that account,
        # which is exactly the state the toggle renders in.
        "headers:acctTeleHeaders(),"
        "body:JSON.stringify({fullTelemetry:next})}).then(function(r){"
        "if(r.status===200){return r.json().then(function(b){"
        'return b&&typeof b.fullTelemetry==="boolean"?b.fullTelemetry:next;},function(){return next;}).then(function(applied){'
        "TELE_ON=applied;TELE_KNOWN=true;reflect();"
        'acctSetMsg(tmsg,applied?"Full telemetry on. Your dashboard and AI now see the complete picture.":"Private mode. Only issue-level signals are shared.",COLORS.good);'
        "return;});}"
        'if(r.status===402){return r.json().then(function(b){return b&&b.error;},function(){return null;}).then(function(er){'
        'throw er==="billing_frozen"?"Billing is frozen \\u2014 clear the balance to change this.":"Full telemetry is a Pro plan feature.";});}'
        'if(r.status===403){throw "This project is paused \\u2014 telemetry can\\u2019t change while paused.";}'
        'if(r.status===401){return r.json().then(function(b){return b&&b.reason;},function(){return null;}).then(function(rs){'
        'throw rs==="install_token"?"This copy needs repair \\u2014 press Repair in your dashboard, then reload.":"Your session expired. Sign in again.";});}'
        'if(r.status===409){throw "This project is linked to a different account.";}'
        'if(r.status===429){throw "Too many changes. Try again in a moment.";}'
        'throw "Couldn\\u2019t change telemetry. Please try again.";'
        "}).catch(function(err){"
        'acctSetMsg(tmsg,typeof err==="string"?err:"Couldn\\u2019t change telemetry. Please try again.",COLORS.poor);'
        '}).then(function(){TELE_BUSY=false;try{pend.style.display="none";sw.style.opacity="1";}catch(e){}});'
        "}catch(e){TELE_BUSY=false;}});"
        "acctBody.appendChild(wrap);"
        "}catch(e){}}"
        # ---- Account context load + auto-refresh -------------------------------
        # A FRESH install registers with the server seconds AFTER the page loaded,
        # so a one-shot read leaves installId/deleteToken null forever and the
        # developer is stuck on "reload after telemetry registers" — a dead end.
        # acctCtxRead() re-reads the SAME loopback-only endpoint with IDENTICAL
        # semantics (credentials omit, same path) and re-populates ACTX; on every
        # success it re-renders the account card and re-runs gateSync(). When the
        # context is still incomplete (no installId / no deleteToken) or the fetch
        # fails, acctPollStart() re-reads every 2s for up to 2 minutes and stops
        # the moment BOTH credentials land (or the cap hits — the existing reload
        # hint then stays as the honest fallback).
        "var acctLoaded=false;var acctPollTimer=null;var acctPollUntil=0;"
        "var ACCT_POLL_MS=2000;var ACCT_POLL_WINDOW_MS=120000;"
        "function acctCtxComplete(){return !!(ACTX.installId&&ACTX.deleteToken);}"
        "function acctPollStop(){try{if(acctPollTimer){clearInterval(acctPollTimer);acctPollTimer=null;}}catch(e){}}"
        "function acctCtxRead(){try{"
        + (
            "acctRender();"
            if safe_account is None
            else 'fetch("'
            + safe_account
            + '",{credentials:"omit"}).then(function(r){'
            "if(!r.ok)return null;return r.json();}).then(function(b){"
            'if(b&&typeof b.endpoint==="string"&&b.endpoint){'
            'ACTX.endpoint=b.endpoint;ACTX.installId=typeof b.installId==="string"?b.installId:null;'
            'ACTX.deleteToken=typeof b.deleteToken==="string"?b.deleteToken:null;'
            "ACTX.fullTelemetry=b.fullTelemetry===true;ACTX.available=true;}"
            # Re-run FULL gate resolution once the account context (installId)
            # is known — gateApply() alone only refreshes the button state, so a
            # reinstalled project (new installId) would stay on a stale
            # acceptance until some later incidental gateSync(). On fetch failure
            # re-sync too (installId stays null => version-only check, never a
            # lockout).
            "acctRender();acctTermsHref();try{gateSync();}catch(e){}"
            # Credentials that land LATE (the poll below) while the developer is
            # already signed in must link the project right away — same auto-link
            # the sign-in path fires, no tap and no reload.
            "try{acctAutoClaim();}catch(e){}"
            "if(acctCtxComplete())acctPollStop();"
            "}).catch(function(){acctRender();try{gateSync();}catch(e){}});"
        )
        + "}catch(e){try{acctRender();}catch(e2){}}}"
        "function acctPollStart(){try{"
        "if(acctPollTimer||acctCtxComplete())return;"
        "acctPollUntil=Date.now()+ACCT_POLL_WINDOW_MS;"
        "acctPollTimer=setInterval(function(){try{"
        "if(acctCtxComplete()||Date.now()>acctPollUntil){acctPollStop();return;}"
        "acctCtxRead();}catch(e){}},ACCT_POLL_MS);"
        "}catch(e){}}"
        # Load the auth context once (called when the panel first opens), then let
        # the poll above keep it fresh until the credentials arrive.
        "function acctLoad(){try{"
        "if(acctLoaded)return;acctLoaded=true;"
        "acctCtxRead();"
        + ("" if safe_account is None else "acctPollStart();")
        + "}catch(e){try{acctRender();}catch(e2){}}}"
        "acctRender();"
        'var note=document.createElement("div");'
        'note.style.cssText="margin:2px 2px 0;color:"+T.mut+";font-size:10.5px;";'
        'note.textContent="Local dev meters \\u2014 served by this project, nothing leaves it.";'
        # Terms & Privacy footer — at the very bottom, muted, opens in a new tab.
        # Href uses the telemetry endpoint origin + /terms when available, else a
        # safe fallback. Guarded: any parse failure degrades to the fallback.
        'var footer=document.createElement("div");'
        'footer.style.cssText="margin:8px 2px 0;text-align:center;";'
        'var terms=document.createElement("a");'
        'terms.textContent="Terms & Privacy";terms.target="_blank";terms.rel="noopener";'
        'terms.style.cssText="color:"+T.mut+";font-size:10px;text-decoration:underline;";'
        'terms.href="https://www.boosthis.com/terms";'
        "function acctTermsHref(){try{"
        "if(ACTX.available&&ACTX.endpoint){"
        'var u=new URL(ACTX.endpoint);terms.href=u.origin+"/terms";}'
        "}catch(e){}}"
        # Two containers inside the panel: the first-open GATE and the normal
        # dashboard. gateSync() toggles which one is visible. The dashboard keeps
        # its exact composition (hero/breakdown/health/acct/legend/note/footer);
        # the acct card node is reparented onto the gate while gated and moved
        # back to its dashboard slot on accept (DOM nodes reparent cleanly).
        # Registration notice banner — sits at the TOP of the dashboard so a kit
        # that registered but uploads nothing (or never registered, or had its id
        # rejected) says so, instead of showing live local meters that read as
        # perfectly healthy while the dashboard it feeds stays empty. Hidden
        # unless the panel read carries a {notice:{kind}}; copy is code-defined
        # here — the read only ever carries the coarse kind.
        'var notice=card();notice.style.display="none";'
        'notice.style.cssText+="border:1px solid "+T.pri+"55;background:"+T.pri+"14;";'
        'var noticeTitle=document.createElement("div");'
        'noticeTitle.style.cssText="font-size:12px;font-weight:800;color:"+T.fg+";";'
        'var noticeBody=document.createElement("div");'
        'noticeBody.style.cssText="font-size:11px;line-height:1.5;color:"+T.mut+";margin-top:4px;";'
        "notice.appendChild(noticeTitle);notice.appendChild(noticeBody);"
        # Dropped-measurements line (fixed copy, under the banner). The server
        # can accept a batch and still refuse individual rows; the closed /panel
        # read carries only a count + code-defined cause markers, and every word
        # below is a literal in this snippet, matching the project's web page so
        # the two never tell different stories. Hidden entirely when nothing has
        # been dropped.
        'var dropEl=card();dropEl.style.display="none";'
        'dropEl.style.cssText+="border:1px solid "+COLORS["needs-work"]+";";'
        'var dropTitle=document.createElement("div");'
        'dropTitle.style.cssText="font-size:12px;font-weight:800;color:"+COLORS["needs-work"]+";";'
        'dropTitle.textContent="Measurements dropped";'
        'var dropBody=document.createElement("div");'
        'dropBody.style.cssText="font-size:11px;line-height:1.5;color:"+T.fg+";margin-top:4px;";'
        "dropEl.appendChild(dropTitle);dropEl.appendChild(dropBody);"
        'var gateWrap=document.createElement("div");gateWrap.style.display="none";'
        'var dashWrap=document.createElement("div");dashWrap.style.display="none";'
        "dashWrap.appendChild(notice);dashWrap.appendChild(dropEl);dashWrap.appendChild(hero);dashWrap.appendChild(bd);dashWrap.appendChild(hl);dashWrap.appendChild(acct);dashWrap.appendChild(lg);dashWrap.appendChild(note);dashWrap.appendChild(footer);"
        "footer.appendChild(terms);"
        "panel.appendChild(gateWrap);panel.appendChild(dashWrap);"
        # ---- The GATE (port of the RN kit's BoosthisTermsGate) ----
        # Layout top→bottom: BOOSTHIS kicker · the connect card (moved here) ·
        # "Terms of Service & Privacy" heading + intro · body copy with a "Terms &
        # Privacy" link · agree checkbox · (needsConnect note) · "I Agree &
        # Continue" · "Decline". textContent-only writes; every path guarded.
        "var gateChecked=false;var gAgree=null,gNote=null,gCheckBox=null;"
        "function gateMk(){try{"
        "while(gateWrap.firstChild)gateWrap.removeChild(gateWrap.firstChild);"
        'var kick=document.createElement("div");'
        'kick.style.cssText="font-size:10px;font-weight:600;letter-spacing:2px;color:"+T.mut+";margin:2px 2px 10px;";'
        'kick.textContent="BOOSTHIS";gateWrap.appendChild(kick);'
        # Connect card at the top — the SAME acct node used on the dashboard.
        "gateWrap.appendChild(acct);"
        'var gt=document.createElement("div");'
        'gt.style.cssText="font-size:20px;font-weight:800;color:"+T.fg+";margin:4px 2px 0;line-height:1.2;";'
        'gt.textContent="Terms of Service & Privacy";gateWrap.appendChild(gt);'
        'var gi=document.createElement("div");'
        'gi.style.cssText="font-size:11.5px;color:"+T.mut+";margin:3px 2px 8px;";'
        'gi.textContent="Please review and accept before using Boosthis.";gateWrap.appendChild(gi);'
        'var gb=document.createElement("div");'
        'gb.style.cssText="font-size:11.5px;line-height:1.55;color:"+T.mut+";margin:0 2px 6px;";'
        'gb.textContent="Everything \\u2014 liability, what Boosthis collects, AI suggestions, and project-key access \\u2014 lives on the Terms & Conditions page. Open it and read it, then tick the box below to agree.";'
        "gateWrap.appendChild(gb);"
        # Terms & Privacy link — same href logic as the dashboard footer link.
        'var gl=document.createElement("a");'
        'gl.textContent="Terms & Privacy";gl.target="_blank";gl.rel="noopener";gl.href=terms.href;'
        'gl.style.cssText="display:inline-block;color:"+T.pri+";font-size:11.5px;font-weight:600;text-decoration:underline;margin:0 2px 12px;";'
        "gateWrap.appendChild(gl);"
        # Checkbox row.
        'var crow=document.createElement("div");'
        'crow.style.cssText="display:flex;align-items:flex-start;gap:9px;cursor:pointer;margin:0 2px 10px;";'
        'gCheckBox=document.createElement("div");'
        'gCheckBox.style.cssText="width:20px;height:20px;flex:0 0 20px;border-radius:5px;border:2px solid "+T.br+";display:flex;align-items:center;justify-content:center;box-sizing:border-box;";'
        'var ck=document.createElement("span");ck.textContent="\\u2713";ck.style.cssText="color:"+T.bg+";font-size:13px;font-weight:700;line-height:1;display:none;";'
        "gCheckBox.appendChild(ck);"
        'var cl=document.createElement("span");'
        'cl.style.cssText="font-size:11.5px;line-height:1.45;color:"+T.fg+";";'
        'cl.textContent="I have read and agree to the Terms of Service & Privacy Policy.";'
        "crow.appendChild(gCheckBox);crow.appendChild(cl);gateWrap.appendChild(crow);"
        'crow.addEventListener("click",function(){try{gateChecked=!gateChecked;'
        'gCheckBox.style.borderColor=gateChecked?T.pri:T.br;gCheckBox.style.background=gateChecked?T.pri:"transparent";'
        'ck.style.display=gateChecked?"block":"none";gateSync();}catch(e){}});'
        # needsConnect note (shown only when registered && !connected).
        'gNote=document.createElement("div");'
        'gNote.style.cssText="display:none;font-size:11px;line-height:1.45;color:"+T.mut+";margin:0 2px 10px;";'
        'gNote.textContent="Sign in above to connect this project to your dashboard before continuing.";'
        "gateWrap.appendChild(gNote);"
        # "I Agree & Continue" — disabled (muted, no-op) unless canAgree.
        'gAgree=document.createElement("button");gAgree.type="button";'
        'gAgree.textContent="I Agree & Continue";'
        'gAgree.style.cssText="width:100%;padding:11px 10px;font-size:13px;font-weight:700;border:none;border-radius:10px;cursor:pointer;font-family:inherit;";'
        "gateWrap.appendChild(gAgree);"
        'gAgree.addEventListener("click",function(){try{'
        "if(!gateCanAgree())return;"
        # Write acceptance (guarded), then reveal the dashboard forever.
        "termsWrite();gateSync();}catch(e){}});"
        # "Decline" — closes the panel; nothing recorded.
        'var gd=document.createElement("button");gd.type="button";gd.textContent="Decline";'
        'gd.style.cssText="width:100%;padding:8px 10px;margin-top:8px;font-size:12px;font-weight:600;background:transparent;color:"+T.mut+";border:none;cursor:pointer;font-family:inherit;";'
        "gateWrap.appendChild(gd);"
        'gd.addEventListener("click",function(){try{panel.style.display="none";}catch(e){}});'
        "gateApply();"
        "}catch(e){}}"
        # registered := account context + installId + deleteToken all present.
        # connected := VERIFIED LINK (LINKED flag). needsConnect := registered &&
        # !connected. canAgree := checked && !needsConnect. Unregistered pages
        # (no account context) proceed on terms alone — never hard-locked.
        "function gateRegistered(){return !!(ACTX.available&&ACTX.endpoint&&ACTX.installId&&ACTX.deleteToken);}"
        "function gateNeedsConnect(){return gateRegistered()&&!LINKED;}"
        "function gateCanAgree(){return gateChecked&&!gateNeedsConnect();}"
        # Reflect canAgree/needsConnect on the gate's controls without a rebuild.
        "function gateApply(){try{"
        'if(gNote)gNote.style.display=gateNeedsConnect()?"block":"none";'
        "if(gAgree){var ok=gateCanAgree();"
        'gAgree.style.background=ok?T.pri:T.br;gAgree.style.color=ok?T.bg:T.mut;'
        'gAgree.style.cursor=ok?"pointer":"default";}'
        "}catch(e){}}"
        # gateSync() = the single source of truth for gate-vs-dashboard
        # visibility. Called on open, on accept, and whenever LINKED/checkbox
        # change. When accepted it moves the acct node back to its dashboard slot
        # (before lg).
        "var gateBuilt=false;"
        "function gateSync(){try{"
        "if(termsAccepted()){"
        'gateWrap.style.display="none";dashWrap.style.display="block";'
        "if(acct.parentNode!==dashWrap)dashWrap.insertBefore(acct,lg);"
        "}else{"
        "if(!gateBuilt){gateBuilt=true;gateMk();}else{"
        "if(acct.parentNode!==gateWrap){var k=gateWrap.firstChild;"
        "if(k&&k.nextSibling)gateWrap.insertBefore(acct,k.nextSibling);else gateWrap.appendChild(acct);}"
        "gateApply();}"
        'gateWrap.style.display="block";dashWrap.style.display="none";}'
        "}catch(e){}}"
        'var CORE={responsiveness:1,resilience:1,budget:1};'
        "function axisRow(m){"
        'var w=document.createElement("div");'
        'w.style.cssText="padding:7px 0;border-bottom:1px solid "+T.br+";";'
        'var top=document.createElement("div");'
        'top.style.cssText="display:flex;justify-content:space-between;align-items:baseline;gap:8px;";'
        'var l=document.createElement("span");'
        'l.style.cssText="color:"+T.fg+";font-weight:600;font-size:12px;";l.textContent=String(m.label||m.key||"");'
        'var rc=m.rating&&COLORS[m.rating]?COLORS[m.rating]:T.mut;'
        'var c=document.createElement("span");'
        'c.style.cssText="color:"+rc+";font-variant-numeric:tabular-nums;font-size:11px;text-align:right;";'
        'c.textContent=String(m.caption||"");'
        "top.appendChild(l);top.appendChild(c);"
        "w.appendChild(top);"
        'if(typeof m.score==="number"){var bar=document.createElement("div");'
        'bar.style.cssText="height:4px;border-radius:3px;background:"+T.br+";margin-top:5px;overflow:hidden;";'
        'var fill=document.createElement("div");'
        'var s=Math.max(0,Math.min(100,m.score));'
        'fill.style.cssText="height:100%;border-radius:3px;width:"+s+"%;background:"+rc+";";'
        "bar.appendChild(fill);w.appendChild(bar);}return w;}"
        "function fillRows(box,list,empty){"
        "while(box.firstChild)box.removeChild(box.firstChild);"
        "if(list.length){for(var i=0;i<list.length;i++){var r=axisRow(list[i]);"
        'if(i===list.length-1)r.style.borderBottom="none";box.appendChild(r);}}'
        'else{var e=document.createElement("div");'
        'e.style.cssText="padding:7px 0;color:"+T.mut+";";e.textContent=empty;box.appendChild(e);}}'
        # showNotice(kind) — render the registration banner for a coarse,
        # code-defined kind (the read only ever carries the kind). hideNotice()
        # restores the normal dashboard. All copy is fixed here — no server text,
        # no key, no token, no URL.
        "function showNotice(kind,data){try{"
        'if(kind==="install-id-not-uuid"){'
        'noticeTitle.textContent="Install ID rejected";'
        'noticeBody.textContent="Boosthis rejected this app\'s install ID because it is not a valid UUID. Generate a real UUID, persist it as the install ID, then restart.";'
        '}else if(kind==="not-registered"){'
        'noticeTitle.textContent="Not registered yet";'
        'noticeBody.textContent=(data&&data.body)||"This app has not registered with Boosthis, so nothing is being sent. Check the project key and this app\'s outbound network access, then restart.";'
        '}else if(kind==="registration-unknown"){'
        'noticeTitle.textContent="Can\'t check right now";'
        'noticeBody.textContent="Boosthis could not be reached to confirm whether this app is registered, so this panel cannot say either way yet. This is not a failed install and it does not mean anything stopped — it settles by itself once the check goes through. Do not change the install line\'s id while this is showing.";'
        '}else if(kind==="sharing-off"){'
        'noticeTitle.textContent="Nothing is being uploaded";'
        'noticeBody.textContent="This app is registered, but sharing is off: these meters stay on this screen and your Boosthis dashboard stays empty. Turn sharing on there, or start the kit with share_meter_with_ai=True.";'
        '}else if(kind==="uploads-failing"){'
        'var FAILTXT={unauthorized:"Refused \\u2014 credentials rejected",rejected:"Refused \\u2014 batch rejected","server-error":"Boosthis failed to store it",unreachable:"No answer \\u2014 timed out or unreachable"};'
        'noticeTitle.textContent="Uploads are not getting through";'
        'noticeBody.textContent="This app is registered and sharing is on, but the last batch of measurements did not reach Boosthis, so your dashboard is missing the most recent data. "+(FAILTXT[data&&data.reason]||FAILTXT.unreachable)+"."+(data&&typeof data.lost==="number"&&data.lost>0?" Uploads lost: "+String(data.lost)+".":"");'
        '}else{notice.style.display="none";return;}'
        'notice.style.display="block";'
        "}catch(e){}}"
        'function hideNotice(){try{notice.style.display="none";}catch(e){}}'
        # showDrops(d) — render the cumulative dropped-row figure from a closed
        # {count, causes:[marker]} read. The cause words are literals HERE (never
        # server text), keyed off the code-defined markers and joined in the
        # fixed order the read already carries. Hidden when the count is zero or
        # the read is absent — a healthy app shows nothing at all.
        'var DROPTXT={labelRejected:"route names the privacy guard refused",'
        'traceCapReached:"spans past the 20-span limit",'
        'snapshotEntryFiltered:"snapshot entries the privacy guard refused"};'
        "function showDrops(d){try{"
        'if(!d||typeof d.count!=="number"||d.count<=0){dropEl.style.display="none";return;}'
        "var w=[];var cs=(d.causes&&d.causes.length)?d.causes:[];"
        "for(var i=0;i<cs.length;i++){var t=DROPTXT[cs[i]];if(t)w.push(t);}"
        'dropBody.textContent=String(d.count)+(w.length?" \\u2014 "+w.join("; "):"");'
        'dropEl.style.display="block";'
        "}catch(e){}}"
        "function paint(p){try{"
        # Kill-switch verdict short-circuits everything. A "hidden" kind (env
        # kill / tampered / grace-expired / never-activated) removes the whole
        # bubble host from the page; any other lock kind (revoked/unpaid/paused)
        # keeps the bubble but opens the BLOCKING overlay over the meters.
        "if(p&&p.lock){"
        'if(p.lock.kind==="hidden"){try{host.remove();}catch(e){}return;}'
        "showLock(p.lock);return;}"
        "hideLock();"
        # Registration notice: shown when the read carries {notice:{kind}}, else
        # hidden. This is the one place the panel says a healthy-looking install
        # is actually silent (registered but sharing off), never registered, or
        # had its id rejected.
        "if(p&&p.notice&&p.notice.kind){showNotice(p.notice.kind,p.notice);}else{hideNotice();}"
        # The dropped-measurements line, from the closed {count,causes} read.
        "showDrops(p&&p.drops);"
        # The server-resolved project display is already fully spelled by the
        # kit. Print it as text and retain the honest fallback on any bad read.
        'try{hProj.textContent="'
        + PROJECT_LABEL
        + ': "+((p&&typeof p.project==="string"&&p.project)||"'
        + PROJECT_UNKNOWN_TEXT
        + '");}catch(e){}'
        'try{hInstall.textContent="'
        + INSTALL_ID_LABEL
        + ': "+((p&&typeof p.installId==="string"&&p.installId)||"'
        + INSTALL_ID_UNKNOWN_TEXT
        + '");}catch(e){}'
        "var c=p&&p.rating?COLORS[p.rating]||T.mut:T.mut;"
        "btn.style.borderColor=c;"
        'num.textContent=p&&typeof p.score==="number"?String(p.score):"\\u2013";'
        "num.style.color=p&&p.rating?c:T.fg;"
        'bTxt.textContent=p&&p.rating?(RL[p.rating]||"MEASURING"):"MEASURING";'
        'bDot.style.background=p&&p.rating?c:T.mut;'
        'badge.style.background=p&&p.rating?c+"1a":T.br;'
        'badge.style.borderColor=p&&p.rating?c+"55":T.br;'
        "badge.style.color=p&&p.rating?c:T.mut;"
        'stat.textContent=p&&typeof p.score==="number"?String(p.sampleCount||0)+" requests measured":"measuring \\u2014 handle a few requests";'
        "var ax=(p&&p.axes)||[];var core=[],rest=[];"
        "for(var i=0;i<ax.length;i++){(CORE[ax[i].key]?core:rest).push(ax[i]);}"
        'fillRows(bdRows,core,"measuring\\u2026 appears after a few requests");'
        'fillRows(hlRows,rest,"measuring\\u2026 appears with traffic");'
        "}catch(e){}}"
        "function poll(){try{"
        'fetch("' + safe_panel + '",{credentials:"omit"}).then(function(r){'
        "return r.ok?r.json():null;}).then(paint).catch(function(){});}catch(e){}}"
        'btn.addEventListener("click",function(){try{'
        'var open=panel.style.display==="none";'
        'panel.style.display=open?"block":"none";'
        # On open: pick gate-vs-dashboard, then load the account context (needed
        # on BOTH surfaces — the connect card is on the gate too) and poll.
        "if(open){gateSync();acctLoad();poll();}}catch(e){}});"
        "panel.appendChild(lockEl);"
        "root.appendChild(panel);root.appendChild(btn);"
        "poll();setInterval(poll,2000);"
        "function add(){try{(document.body||document.documentElement).appendChild(host);}catch(e){}}"
        'if(document.body)add();else document.addEventListener("DOMContentLoaded",add);'
        "}catch(e){}})();</script>\n"
    )


# Hero subtitle for this runtime's panel — mirrors the RN kit's
# "React Native Performance" hero subtitle.
RUNTIME_PANEL_SUBTITLE = "Python Performance"


def inject_into_html(body: str, snippet: str) -> str | None:
    """Insert the snippet immediately before the LAST ``</body>``
    (case-insensitive). Returns None when no ``</body>`` exists — the caller
    must then serve the original bytes untouched. Pure; easy to test."""
    try:
        idx = body.lower().rfind("</body>")
        if idx == -1:
            return None
        return body[:idx] + snippet + body[idx:]
    except Exception:  # noqa: BLE001
        return None


def _reset_bubble_for_tests() -> None:
    """Test helper — nothing module-level to reset today; kept for parity with
    the other kits so future state has a sanctioned reset point."""
    return None
