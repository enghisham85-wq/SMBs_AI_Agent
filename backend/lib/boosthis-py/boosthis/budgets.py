"""Auto perf budgets — learn-then-alert.

Mirrors ``lib/boosthis-runtime-node/src/budgets.ts``.

This is the one Boosthis reading that is LEARNED rather than typed, so it is
held to the three conditions ``docs/decisions/adaptive-anomaly-detection.md``
names: reproducible from stated inputs, explainable in a sentence, and
abstaining when under-sampled. The rule in full is
``docs/learned-budget-contract.md``.

Three properties the earlier version did not have:

1. THE TWO WINDOWS CANNOT OVERLAP, AND THE BASELINE DOES NOT DEPEND ON WHEN
   IT IS READ. The baseline is the route's FIRST samples, kept by the ring
   module as they arrived; the window compared against it takes only samples
   stamped strictly later. Re-deriving both slices from the shared ring meant
   a busy route's "baseline" was the same few minutes as its "recent" — the
   app compared against itself just now — so a regression that built over an
   hour never tripped it. Capturing at arrival rather than freezing on first
   read is what closes that for a process nobody reads until the drift has
   already happened.
2. WARM-UP IS NOT THE YARDSTICK. The first ``BUDGET_WARMUP_N`` samples of a
   route are cold-cache and cold-import. They are excluded from the baseline
   on that stated rule, so ordinary running does not read as "improved"
   against a cold start.
3. A ROUTE WHOSE HISTORY WAS EVICTED SAYS SO, AND KEEPS ITS ROW. The ring
   holds 1000 samples shared by every route; a quiet route can be pushed
   below the floor and never climb back. That is a different answer from
   "still learning", and it carries what would change it. It is also why
   :func:`all_statuses` lists routes by what this process has MEASURED rather
   than by what the ring still holds — a route whose last sample was evicted
   would otherwise vanish from every surface before it could say anything.

States per route:

- ``learning``  — not enough of this route's samples yet; more traffic fixes it.
- ``evicted``   — this route HAD more samples and the shared ring dropped them
                  before a verdict could be drawn.
- ``stable``    — the later window is within tolerance of the baseline p95.
- ``regressed`` — the later window exceeds the baseline by REGRESSION_FACTOR.
- ``improved``  — the later window is well below the baseline.

Both spans travel with the answer, and so does the name of the algorithm that
produced it: the phone and browser kits answer the same question from a
server-computed window instead, and a reader must never have to guess which
one replied.

No I/O, no background threads. Always callable; safe to invoke from the MCP
tool path.
"""

from __future__ import annotations

from typing import Any

from boosthis import samples

#: Samples of a route treated as warm-up and never entered into its baseline.
BUDGET_WARMUP_N = 5
#: Samples that form the frozen baseline, after warm-up.
BUDGET_BASELINE_N = 20
#: Samples taken AFTER the baseline closed that form the compared window.
BUDGET_RECENT_N = 20

REGRESSION_FACTOR = 1.5
IMPROVEMENT_FACTOR = 0.66

#: How many of this route's samples must sit in the ring at once before a
#: first verdict is possible.
BUDGET_SAMPLES_FOR_VERDICT = BUDGET_WARMUP_N + BUDGET_BASELINE_N + BUDGET_RECENT_N

#: The shared ring's capacity, as this module reads it.
BUDGET_RING_LIMIT = 1000

#: Back-compatible names — the module's public constants from its first
#: release, kept so a reader importing them does not break under a rename.
BASELINE_N = BUDGET_BASELINE_N
RECENT_N = BUDGET_RECENT_N

#: Which algorithm produced a budget answer. ``device-ring-frozen-baseline``
#: is this one: computed in the app's own process from its in-memory sample
#: ring, against a baseline frozen on the device. The phone and browser kits
#: answer the same question from a window the Boosthis server computes over
#: uploaded readings — a different algorithm over different data. Named on
#: every surface so a developer's assistant is never left to infer it.
BUDGET_ALGORITHM = "device-ring-frozen-baseline"

#: The three states that mean a verdict was actually reached. A caller
#: counting "routes carrying a budget" must use this rather than testing for
#: ``!= "learning"``, or an evicted route is counted as judged.
JUDGED_STATES = frozenset({"stable", "regressed", "improved"})


def is_judged(status: dict[str, Any]) -> bool:
    """True when this route reached a verdict rather than abstaining."""
    return status.get("state") in JUDGED_STATES


_LEARNING_NO_BASELINE = (
    f"Still learning — a baseline needs {BUDGET_WARMUP_N} warm-up samples of "
    f"this route followed by {BUDGET_BASELINE_N} more, and a verdict needs "
    f"{BUDGET_RECENT_N} taken after that baseline closes "
    f"({BUDGET_SAMPLES_FOR_VERDICT} in the ring at once). More traffic on "
    "this route is all it takes."
)

_LEARNING_AWAITING_RECENT = (
    "Baseline frozen and closed; still learning — a verdict needs "
    f"{BUDGET_RECENT_N} samples taken strictly after it, so the two windows "
    "share none. More traffic on this route is all it takes."
)

_EVICTED = (
    f"Not judged — this route's samples were dropped from the "
    f"{BUDGET_RING_LIMIT}-sample ring, shared by every route, before a "
    "verdict could be drawn. It is judged again once "
    f"{BUDGET_SAMPLES_FOR_VERDICT} of its samples sit in the ring at once: a "
    "larger share of this app's traffic, or a quieter app."
)


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * p))
    return s[idx]


def _baseline_for(name: str) -> dict[str, Any] | None:
    """The route's frozen baseline, drawn from its ARRIVAL record.

    The baseline is the first samples the process ever recorded for this
    route, kept by the ring module as they arrived
    (:func:`samples.route_head`). Nothing here is frozen at READ time.

    Freezing on the first read looks equivalent and is not: a process that
    serves traffic for an hour before anything reads a budget hands that first
    read a ring holding only the last few minutes of the route, so the
    "frozen" baseline is once again the app measured against itself just now.
    The arrival record has no such dependence on when it is read — it is the
    same twenty samples whether the first read comes after a minute or after a
    day, which is also what makes the verdict reproducible from stated inputs.

    Returns ``None`` until the route has warm-up plus a full baseline behind
    it.
    """
    rec = samples.route_head(name)
    if rec is None:
        return None
    if len(rec.head) < BUDGET_WARMUP_N + BUDGET_BASELINE_N:
        return None
    sl = rec.head[BUDGET_WARMUP_N : BUDGET_WARMUP_N + BUDGET_BASELINE_N]
    return {
        "p95_ms": _percentile([s.duration_ms for s in sl], 0.95),
        "samples": len(sl),
        "from_ms": sl[0].timestamp_ms,
        "to_ms": sl[-1].timestamp_ms,
        "warmup_excluded": BUDGET_WARMUP_N,
    }


def reset_baselines() -> None:
    """Drop every frozen baseline. Tests only.

    Nothing is cached here any more: a baseline is derived from the route's
    arrival record, which :func:`samples.clear` resets along with the ring.
    Kept so a caller written against the earlier module keeps working, and so
    the intent has one obvious name.
    """
    samples.clear()


def _judged_explanation(base: dict[str, Any], recent_p95: int, recent_n: int) -> str:
    return (
        f"Baseline p95 {base['p95_ms']}ms from {base['samples']} samples, "
        f"against {recent_p95}ms from {recent_n} taken strictly after that "
        "baseline closed — the two windows share no sample. The route's first "
        f"{base['warmup_excluded']} samples are warm-up and are not in the "
        f"baseline. Regression at {REGRESSION_FACTOR}× the baseline, "
        f"improvement at {IMPROVEMENT_FACTOR}×."
    )


def status_for(name: str) -> dict[str, Any]:
    """Return budget status for a single route name."""
    history = samples.recent(limit=BUDGET_RING_LIMIT, name=name)
    # samples.recent() returns newest-first; reverse to chronological.
    ordered = list(reversed(history))
    base = _baseline_for(name)

    # Eviction is claimed only on positive evidence: more samples of this
    # route have arrived than the ring still holds. A route that has simply
    # never been busy has never had any dropped, and is still learning.
    _head = samples.route_head(name)
    arrived = _head.total if _head is not None else 0
    lost_history = arrived > len(history)

    if base is None:
        return {
            "route": name,
            "state": "evicted" if lost_history else "learning",
            "algorithm": BUDGET_ALGORITHM,
            "explanation": _EVICTED if lost_history else _LEARNING_NO_BASELINE,
            "samples_seen": len(history),
            "samples_needed": BUDGET_SAMPLES_FOR_VERDICT,
            "warmup_excluded": BUDGET_WARMUP_N,
        }

    after = [s for s in ordered if s.timestamp_ms > base["to_ms"]]
    baseline_window = {
        "samples": base["samples"],
        "from_ms": base["from_ms"],
        "to_ms": base["to_ms"],
        "p95_ms": base["p95_ms"],
    }
    if len(after) < BUDGET_RECENT_N:
        return {
            "route": name,
            "state": "evicted" if lost_history else "learning",
            "algorithm": BUDGET_ALGORITHM,
            "explanation": _EVICTED if lost_history else _LEARNING_AWAITING_RECENT,
            "baseline_p95_ms": base["p95_ms"],
            "baseline_window": baseline_window,
            "samples_seen": len(history),
            "samples_needed": BUDGET_SAMPLES_FOR_VERDICT,
            "warmup_excluded": base["warmup_excluded"],
        }

    recent_slice = after[-BUDGET_RECENT_N:]
    recent_p95 = _percentile([s.duration_ms for s in recent_slice], 0.95)
    baseline_p95 = base["p95_ms"]
    if baseline_p95 == 0:
        state = "stable"
    elif recent_p95 >= baseline_p95 * REGRESSION_FACTOR:
        state = "regressed"
    elif recent_p95 <= baseline_p95 * IMPROVEMENT_FACTOR:
        state = "improved"
    else:
        state = "stable"
    return {
        "route": name,
        "state": state,
        "algorithm": BUDGET_ALGORITHM,
        "explanation": _judged_explanation(base, recent_p95, len(recent_slice)),
        "baseline_p95_ms": baseline_p95,
        "recent_p95_ms": recent_p95,
        "baseline_window": baseline_window,
        "recent_window": {
            "samples": len(recent_slice),
            "from_ms": recent_slice[0].timestamp_ms,
            "to_ms": recent_slice[-1].timestamp_ms,
            "p95_ms": recent_p95,
        },
        "samples_seen": len(history),
        "warmup_excluded": base["warmup_excluded"],
    }


def all_statuses() -> list[dict[str, Any]]:
    """Every route this process has measured — not only the ones the ring
    still holds a sample of.

    Listing the ring's names alone is what made the ``evicted`` answer
    unreachable in practice: the moment a quiet route's last sample was pushed
    out, the route vanished from the panel, the budgets answer, the CLI, the
    MCP tool, the snapshot and the resilience input — so the one surface that
    would have said "this route was dropped before it could be judged" had
    already stopped mentioning the route at all. The arrival record outlives
    eviction, so the route keeps its row and states why it is unjudged.
    """
    seen: set[str] = set(samples.tracked_routes())
    for s in samples.recent(limit=BUDGET_RING_LIMIT):
        seen.add(s.name)
    return [status_for(name) for name in sorted(seen)]


def regressions() -> list[dict[str, Any]]:
    """Convenience: only routes whose later window exceeded the baseline."""
    return [b for b in all_statuses() if b.get("state") == "regressed"]
