"""Composite score computation — identical formula to `@boosthis/runtime`.

    score = TTFF×0.25 + TTI×0.45 + FID×0.30
    good ≥ 85 · needs-work ≥ 60 · poor < 60
"""

import math
from typing import Literal

from boosthis.health_axes import InvertedAxisBandError
from boosthis.thresholds import (
    RATING_CUTOFFS,
    SCORE_THRESHOLDS,
    SCORE_WEIGHTS,
    Rating,
)


def score_metric(value_ms: float, good: int, poor: int) -> int:
    """Score a reading against a band of two constants in our own source.

    ``good`` (or below) scores 100 and ``poor`` (or above) scores 0, linearly
    between them. An equal or reversed band is a typo in this kit, not the
    customer's app. This used to answer silently (or divide by zero), hiding
    a backwards meter behind a perfect verdict. The throw is the BACKSTOP:
    scripts' axis-band-inversion gate reads declared constants from every
    shipped kit and fails the release before an inverted pair reaches anybody.
    """
    if not math.isfinite(good) or not math.isfinite(poor) or poor <= good:
        raise InvertedAxisBandError(good, poor)
    if value_ms <= good:
        return 100
    if value_ms >= poor:
        return 0
    return round(100 * (1 - (value_ms - good) / (poor - good)))


def compute_score(
    ttff_ms: float | None = None,
    tti_ms: float | None = None,
    fid_ms: float | None = None,
) -> int:
    """Compute the composite Boosthis score (0-100) from raw metrics.

    Any metric that's ``None`` falls back to a full score of 100 — i.e. it's
    treated as "not measured, assume good". This matches the JS runtime's
    behaviour for unmeasured FID on web.
    """
    ttff_score = (
        score_metric(ttff_ms, SCORE_THRESHOLDS["ttff"]["good"], SCORE_THRESHOLDS["ttff"]["poor"])
        if ttff_ms is not None
        else 100
    )
    tti_score = (
        score_metric(tti_ms, SCORE_THRESHOLDS["tti"]["good"], SCORE_THRESHOLDS["tti"]["poor"])
        if tti_ms is not None
        else 100
    )
    fid_score = (
        score_metric(fid_ms, SCORE_THRESHOLDS["fid"]["good"], SCORE_THRESHOLDS["fid"]["poor"])
        if fid_ms is not None
        else 100
    )
    return round(
        ttff_score * SCORE_WEIGHTS["ttff"]
        + tti_score * SCORE_WEIGHTS["tti"]
        + fid_score * SCORE_WEIGHTS["fid"]
    )


def get_rating(score: int) -> Literal["good", "needs-work", "poor"]:
    if score >= RATING_CUTOFFS["good"]:
        return "good"
    if score >= RATING_CUTOFFS["needsWork"]:
        return "needs-work"
    return "poor"


def get_duration_rating(ms: float) -> Rating:
    """Convenience: rate a single duration measurement (uses TTI band).

    The poor boundary is inclusive (``ms >= poor`` -> ``"poor"``), byte-identical
    to the RN ``rateSpanDuration`` and Node ``rateDuration`` helpers, so a given
    duration maps to the SAME rating enum in all three runtimes (a full-stack
    span's rating never disagrees across layers at the band edge). This also
    keeps the enum consistent with ``score_metric``, which already returns 0 at
    exactly ``poor``.
    """
    tti = SCORE_THRESHOLDS["tti"]
    if ms <= tti["good"]:
        return "good"
    if ms >= tti["poor"]:
        return "poor"
    return "needs-work"
