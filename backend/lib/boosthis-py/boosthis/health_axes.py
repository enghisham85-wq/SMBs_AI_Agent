"""Additive dashboard meters derived from live, privacy-safe perf state.

Python mirror of ``lib/boosthis-runtime-node/src/healthAxes.ts``. Two
read-only gauges shown on the dashboard:

- **Code Health** — how much rule pressure the running app is under right now
  (poor/needs-work routes, budget regressions, distinct live finding kinds).
- **Resilience** — how stable latency is under load (tail blowup p99/p50,
  poor-sample rate, and budget-regression pressure).

IMPORTANT: these axes are ADDITIVE. They are computed from the same in-process
summary + budget state the dashboard already shows, and they NEVER feed the
composite Speed score (TTFF/TTI/FID). They carry only counts, ratios, scores,
and rating buckets; no route names, timings, source, or values are added to any
transmit path. Keep this file in sync with the Node implementation.
"""

from __future__ import annotations

import math

from boosthis import budgets, samples
from boosthis.live_detectors import collect_detector_findings
from boosthis.thresholds import (
    read_resilience_tail,
    RESILIENCE_MIN_MEDIAN_MS,
    RESILIENCE_MIN_TAIL_SAMPLES,
    RESILIENCE_TAIL_FLOOR_MS,
)

# Below this sample count both meters report "pending" (null score) rather than
# a misleading 100 or 0 computed from one or two requests.
MIN_SAMPLES_FOR_AXES = 5

INVERTED_BAND_MESSAGE = "boosthis: inverted axis band, good must be below poor"


class InvertedAxisBandError(ValueError):
    """An inverted pair of scoring constants in this kit's own source."""

    def __init__(self, good: float, poor: float) -> None:
        self.good = good
        self.poor = poor
        super().__init__(f"{INVERTED_BAND_MESSAGE} (good={good}, poor={poor})")


def rating_for(score: float) -> str:
    """Map a 0..100 score onto the shared rating bands."""
    if score >= 85:
        return "good"
    if score >= 60:
        return "needs-work"
    return "poor"


def linear_score(value: float, good: float, poor: float) -> int:
    """Score a lower-is-better reading against a band of two source constants.

    ``good`` (or below) scores 100 and ``poor`` (or above) scores 0, with
    interpolation between them. An equal, reversed, or non-finite pair is a
    typo in our kit, never a customer's app error. This used to answer 100
    silently, hiding a backwards meter behind a perfect verdict on every
    install. The throw is the BACKSTOP: scripts' axis-band-inversion gate
    reads declared constants from every shipped kit and fails the release
    before an inverted pair can reach anybody.
    """
    if not math.isfinite(good) or not math.isfinite(poor) or poor <= good:
        raise InvertedAxisBandError(good, poor)
    if value <= good:
        return 100
    if value >= poor:
        return 0
    return round(100 * (poor - value) / (poor - good))


def _clamp(n: float) -> int:
    return max(0, min(100, round(n)))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def collect_findings(
    summary: dict | None = None,
    regressions: list[dict] | None = None,
) -> list[dict]:
    """Live finding set — mirror of Node ``reporting.collectFindings``: every
    non-good route becomes a ``slow-route`` finding and every regressed route a
    ``budget-regression`` finding. ``name`` is local-only (never transmitted);
    callers here only ever count distinct ``kind`` values."""
    sum_ = summary if summary is not None else samples.summary()
    regs = regressions if regressions is not None else budgets.regressions()
    out: list[dict] = []
    for route, r in sum_.get("byRoute", {}).items():
        if r.get("worst_rating") == "good":
            continue
        out.append({"kind": "slow-route", "name": route})
    for reg in regs:
        out.append({"kind": "budget-regression", "name": reg.get("route", "")})
    # Live cross-cutting detectors (retry-storm / idle-burn). These carry their
    # own honest p95/count (host burst span, idle CPU ms) — unlike the route
    # findings above, whose p95/count is enriched later from the route lookup.
    out.extend(collect_detector_findings())
    return out


def _pending(key: str, label: str, total: int) -> dict:
    return {
        "key": key,
        "label": label,
        "score": None,
        "rating": "pending",
        "caption": f"Need {MIN_SAMPLES_FOR_AXES} samples — {total} so far.",
    }


def compute_code_health(
    summary: dict, regressions: list[dict], findings: list[dict]
) -> dict:
    """Rule pressure right now. Starts at 100 and subtracts capped penalties for
    poor/needs-work routes, budget regressions, and distinct live finding kinds."""
    total = summary.get("total", 0)
    if total < MIN_SAMPLES_FOR_AXES:
        return _pending("codeHealth", "Code Health", total)

    poor_routes = 0
    needs_work_routes = 0
    for r in summary.get("byRoute", {}).values():
        wr = r.get("worst_rating")
        if wr == "poor":
            poor_routes += 1
        elif wr == "needs-work":
            needs_work_routes += 1

    regressed_routes = len(regressions)
    distinct_kinds = len({f["kind"] for f in findings})

    penalty_routes = min(45, poor_routes * 12 + needs_work_routes * 6)
    penalty_regress = min(30, regressed_routes * 15)
    penalty_kinds = min(25, distinct_kinds * 10)
    score = _clamp(100 - penalty_routes - penalty_regress - penalty_kinds)

    return {
        "key": "codeHealth",
        "label": "Code Health",
        "score": score,
        "rating": rating_for(score),
        "caption": (
            f"{_plural(len(findings), 'live rule signal')} · "
            f"{_plural(poor_routes, 'poor route')} · "
            f"{_plural(regressed_routes, 'regression')}"
        ),
    }


def compute_resilience(
    summary: dict, all_budgets: list[dict], regressions: list[dict]
) -> dict:
    """How stable latency is under load. Weighted blend of tail blowup
    (p99/p50, weight 0.40), poor-sample rate (weight 0.35), and regression
    pressure against budgeted routes (weight 0.25)."""
    total = summary.get("total", 0)
    if total < MIN_SAMPLES_FOR_AXES:
        return _pending("resilience", "Resilience", total)

    # The tail term follows the cross-kit contract in thresholds.py: an
    # absolute floor below which no ratio is worth acting on, a sample floor
    # below which no p99 is published, and an honest abstention when the median
    # is smaller than the capture resolution.
    p50 = summary.get("p50_ms")
    p99 = summary.get("p99_ms")
    tail_state, tail_ratio = read_resilience_tail(p50, p99, total)
    if tail_state == "insufficient":
        return {
            "key": "resilience",
            "label": "Resilience",
            "score": None,
            "rating": "pending",
            "caption": (
                f"Need {RESILIENCE_MIN_TAIL_SAMPLES} samples for a tail — {total} so far."
            ),
        }
    if tail_state == "unmeasurable":
        # The tail is the dominant term. Rescoring the axis on the other two
        # would publish a verdict most of whose weight was never measured, so
        # the whole axis withholds instead.
        return {
            "key": "resilience",
            "label": "Resilience",
            "score": None,
            "rating": "pending",
            "caption": (
                f"Tail not judged — median under {RESILIENCE_MIN_MEDIAN_MS}ms, "
                f"too small to measure a p99/p50 ratio against (p99 {p99}ms)."
            ),
        }
    tail_score = 100 if tail_state == "flat" else linear_score(tail_ratio, 3, 8)

    poor_rate = (summary.get("poor", 0) / total) if total > 0 else 0
    poor_rate_score = linear_score(poor_rate, 0.05, 0.25)

    # Only a route that reached a verdict carries a budget. A route still
    # learning has no baseline yet, and one whose history the shared ring
    # evicted never will on its own — counting either would make a fresh app
    # look perfectly resilient or unfairly penalized before anything was
    # measured.
    routes_with_budgets = sum(1 for b in all_budgets if budgets.is_judged(b))

    # WHEN NOTHING CARRIES A BUDGET THE TERM WITHDRAWS — it is not scored 100.
    # A full mark awarded because nothing could be computed is the fabricated
    # all-clear the four-state rule forbids everywhere else in the product: it
    # lifted the axis by up to 25 points on every app whose routes were all
    # still learning. The two measured terms are renormalised over their own
    # weight instead, and the caption says the third was not judged.
    terms: list[tuple[float, float]] = [(0.4, tail_score), (0.35, poor_rate_score)]
    if routes_with_budgets > 0:
        regression_ratio = len(regressions) / routes_with_budgets
        terms.append((0.25, linear_score(regression_ratio, 0, 0.25)))
        regression_caption = (
            f"{_plural(len(regressions), 'regression')} of "
            f"{routes_with_budgets} budgeted"
        )
    else:
        regression_caption = "regressions not judged — no route carries a baseline"
    weight = sum(w for w, _ in terms)
    score = _clamp(sum(w * s for w, s in terms) / weight)

    return {
        "key": "resilience",
        "label": "Resilience",
        "score": score,
        "rating": rating_for(score),
        "caption": (
            (
                f"tail {p99}ms — every request under {RESILIENCE_TAIL_FLOOR_MS}ms · "
                if tail_state == "flat"
                else f"tail p99/p50 {tail_ratio:.1f}× · "
            )
            + f"{round(poor_rate * 100)}% poor · "
            + regression_caption
        ),
    }


def compute_dashboard_axes(summary: dict | None = None) -> dict:
    """Build both dashboard axes from live state. Computes budget statuses once
    and derives the regressed subset + findings from it to avoid recomputation."""
    sum_ = summary if summary is not None else samples.summary()
    all_budgets = budgets.all_statuses()
    regressions = [b for b in all_budgets if b.get("state") == "regressed"]
    findings = collect_findings(sum_, regressions)
    axes = {
        "codeHealth": compute_code_health(sum_, regressions, findings),
        "resilience": compute_resilience(sum_, all_budgets, regressions),
    }
    try:
        from boosthis.held_open import get_held_open_stats

        held = get_held_open_stats()
        if held["heldOpenExcluded"] > 0:
            axes["resilience"].update(held)
    except Exception:  # noqa: BLE001
        pass
    return axes
