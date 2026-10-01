"""symptom_hints — proactive "what's probably wrong" pairing for the triage tool.

Mirror of the React-Native source of truth
(``lib/boosthis-runtime-rn/src/symptomHints.ts``) and
``lib/boosthis-runtime-node``. Boosthis never reads the developer's source, so it
cannot *know* which rule a slow route violates; it can only see the timing SHAPE
of a route (its percentiles / spike ratio / rating). That shape is a strong prior
on the *class* of cause:

  • a route that is consistently slow (tight spread, p99≈p50) is almost always
    doing steady, avoidable work on every request — sequential awaits, an N+1, a
    missing index, no caching, heavy synchronous work.
  • a route whose tail blows up (p99 ≫ p50) is spiky — GC pauses, event-loop
    stalls, cold starts, retry storms, CPU-bound bursts.

So this maps the timing shape → a small set of the rule ids most commonly behind
that shape, plus a ``next_step`` telling the AI to CONFIRM with
``match_rules_for_code`` on the real handler source (which the developer's own AI
*can* read) and then ``get_rule`` for the fix. It is a prior, never a diagnosis —
the copy says so.

The rule ids below are asserted to exist in the Python checklist by
``tests/test_symptom_hints.py``.
"""

from __future__ import annotations

from typing import Any, Optional

# "steady-slow" | "spiky-tail" | "unknown"
Symptom = str


# Symptom → the rule ids most commonly behind that latency shape (Python pack).
# Kept deliberately small (≤8) and ranked most-likely-first so the AI has a
# short, high-signal list to confirm — not the whole rule book.
SYMPTOM_HINTS: dict[str, list[str]] = {
    # Consistently slow route — steady per-request work to cut.
    "steady-slow": [
        "sequential-awaits-not-gathered",
        "n-plus-one-orm-query",
        "sync-io-in-async-handler",
        "fastapi-sync-route-blocks-event-loop",
        "unbounded-queryset-no-pagination",
        "pandas-iterrows-anti-pattern",
        "large-json-serialize-blocking",
        "python-missing-cache-headers",
    ],
    # Intermittent tail spikes — bursty work, not steady load.
    "spiky-tail": [
        "python-regex-redos",
        "requests-retry-storm",
        "requests-default-no-timeout",
        "gunicorn-cold-fork-startup",
        "cache-stampede-no-lock",
        "asyncio-unbounded-gather-fanout",
        "gil-bound-cpu-in-asyncio",
        "sqlalchemy-pool-not-sized",
    ],
    # Rated slow but the shape can't be classified (no percentile spread) — a
    # general blend of the most common causes to confirm.
    "unknown": [
        "sequential-awaits-not-gathered",
        "n-plus-one-orm-query",
        "sync-io-in-async-handler",
        "fastapi-sync-route-blocks-event-loop",
        "python-regex-redos",
    ],
}


# Fixed prose — makes clear this is a prior to confirm, never a diagnosis.
NEXT_STEP = (
    "These are the rule ids most commonly behind this latency shape — a prior, "
    "not a confirmed diagnosis. Call boosthis.match_rules_for_code on this "
    "route's handler source to see which actually apply, then boosthis.get_rule "
    "for the fix."
)

_SPIKY_RATIO = 4


def _opt_num(v: Any) -> Optional[float]:
    """Coerce to a finite float, or None (booleans are never numbers here)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    return None


def classify_symptom(stats: dict[str, Any]) -> Optional[str]:
    """Classify a route's timing shape. Returns None when the route is healthy
    (good / unrated) so callers attach NO nudge to fast routes."""
    rating = stats.get("worstRating")
    if not isinstance(rating, str) or rating == "good":
        return None
    ratio: Optional[float] = None
    sr = _opt_num(stats.get("spikeRatio"))
    if sr is not None:
        ratio = sr
    else:
        p99 = _opt_num(stats.get("p99Ms"))
        p50 = _opt_num(stats.get("p50Ms"))
        if p99 is not None and p50 is not None and p50 > 0:
            ratio = p99 / p50
    if ratio is None:
        return "unknown"
    return "spiky-tail" if ratio >= _SPIKY_RATIO else "steady-slow"


def nudge_fields(stats: dict[str, Any]) -> dict[str, Any]:
    """Build the nudge fields to merge onto a triage row, or ``{}`` for a healthy
    route. Attach these AFTER the PII filter — they are shipped constants (enum +
    rule ids + fixed prose), so the guard must never drop a row over them."""
    symptom = classify_symptom(stats)
    if not symptom:
        return {}
    return {
        "symptom": symptom,
        "candidate_rule_ids": list(SYMPTOM_HINTS[symptom]),
        "next_step": NEXT_STEP,
    }


__all__ = [
    "SYMPTOM_HINTS",
    "NEXT_STEP",
    "classify_symptom",
    "nudge_fields",
]
