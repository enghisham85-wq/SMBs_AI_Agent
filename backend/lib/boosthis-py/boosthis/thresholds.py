"""Score thresholds, weights, and rating cutoffs.

These values are intentionally identical to `@boosthis/runtime` so a "good"
Python route and a "good" React Native screen mean the same thing across
the stack.

Source of truth: Indeed Engineering — "Bringing Lighthouse to the App" (2026).
"""

from typing import Final, Literal, TypedDict


class ThresholdBand(TypedDict):
    good: int
    poor: int


class ScoreThresholds(TypedDict):
    ttff: ThresholdBand
    tti: ThresholdBand
    fid: ThresholdBand


class ScoreWeights(TypedDict):
    ttff: float
    tti: float
    fid: float


class RatingCutoffs(TypedDict):
    good: int
    needsWork: int


SCORE_THRESHOLDS: Final[ScoreThresholds] = {
    "ttff": {"good": 300, "poor": 800},
    "tti": {"good": 500, "poor": 1500},
    "fid": {"good": 50, "poor": 150},
}

SCORE_WEIGHTS: Final[ScoreWeights] = {
    "ttff": 0.25,
    "tti": 0.45,
    "fid": 0.30,
}

RATING_CUTOFFS: Final[RatingCutoffs] = {
    "good": 85,
    "needsWork": 60,
}

RUNTIME_VERSION: Final[str] = "1.0.0a154"

Rating = Literal["good", "needs-work", "poor", "pending"]

# ── The three silences ───────────────────────────────────────────────────────
#
# "pending" used to be the ONLY word for "no score", and three genuinely
# different states were all reported with it:
#
#   pending        still warming up — not enough data yet, a score IS coming.
#   not-available  this host cannot take the reading, so no score will ever
#                  appear on THIS install (another host may score it fine).
#   not-scored     a real reading that is deliberately never graded, by design,
#                  permanently, on every install. refusalHonesty is the clearest
#                  case: header presence cannot tell a wrongly refused caller
#                  from a correctly refused one, so it reports what it saw and
#                  never grades it.
#
# A reader who could only see "pending" read a permanent observation as a wait
# and came back for a verdict that was never coming. Same four-state doctrine
# the dashboard already uses: nothing-yet, off, not-available and cannot-tell
# must LOOK different from one another.
NoScoreRating = Literal["pending", "not-available", "not-scored"]

# ── Resilience tail contract — shared by EVERY kit ───────────────────────────
#
# The resilience axis scores p99/p50, the "tail blowup" ratio. On its own that
# ratio produces confident nonsense at low latency:
#
#   * NO FLOOR. A service with a 3 ms median and a 22 ms p99 was rated POOR
#     (ratio 7.3). Nothing about a 22 ms worst case is a problem.
#   * THE ZERO MEDIAN WAS A FREE 100. Durations are captured in whole
#     milliseconds, so any route with a median under half a millisecond — a
#     health check, a cached read, a static handler — had a p50 of exactly 0
#     and the ratio was hard-coded to the healthiest value instead of computed.
#     That is the NORMAL condition for a fast endpoint, and the tail is the
#     dominant term in the score.
#   * QUANTISATION NOISE. At a 3-4 ms median one millisecond of rounding swings
#     the ratio by a third. Driven deliberately against an identical 22 ms
#     tail, the score went 100 (p50 0 ms) -> 48 (p50 3 ms) -> 70 (p50 4 ms).
#
# Bands and blend weights stay per-kit; these three numbers do not. Every copy
# is named by scripts/src/__tests__/resilience-tail-parity.test.ts.

#: A p99 at or below this is not a tail worth acting on, whatever the ratio.
RESILIENCE_TAIL_FLOOR_MS: Final[int] = 50

#: Below this many samples a "p99" is just the slowest of a handful.
RESILIENCE_MIN_TAIL_SAMPLES: Final[int] = 20

#: A median below the capture resolution is unmeasured, not small.
RESILIENCE_MIN_MEDIAN_MS: Final[int] = 1

#: One of "insufficient" | "flat" | "unmeasurable" | "rated".
TailState = Literal["insufficient", "flat", "unmeasurable", "rated"]


def read_resilience_tail(
    p50_ms: float | None, p99_ms: float | None, sample_count: int
) -> tuple[str, float | None]:
    """The one place this judgement is made in the Python kit.

    Returns ``(state, ratio)``. ``ratio`` is None for every state except
    "rated" and the sub-floor "flat" case with a measurable median. Callers
    apply their own bands to the ratio and their own blend weight to the term.
    """
    if (
        sample_count < RESILIENCE_MIN_TAIL_SAMPLES
        or p50_ms is None
        or p99_ms is None
    ):
        return ("insufficient", None)
    measurable_median = p50_ms >= RESILIENCE_MIN_MEDIAN_MS
    if p99_ms <= RESILIENCE_TAIL_FLOOR_MS:
        return ("flat", (p99_ms / p50_ms) if measurable_median else None)
    if not measurable_median:
        return ("unmeasurable", None)
    return ("rated", p99_ms / p50_ms)
