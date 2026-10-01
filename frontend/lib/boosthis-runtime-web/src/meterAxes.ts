/** Boosthis meter axes — ADDITIVE browser-side perf signals.
 *
 * The web parity for the React-Native kit's `meterAxes.ts`. Each scorer here
 * ENRICHES the dashboard without touching the shared composite Speed score
 * (which lives in thresholds.ts). Every band, gate, and rounding rule mirrors
 * the RN canonical source (`lib/boosthis-runtime-rn/src/meterAxes.ts`) so the
 * same number means the same thing in every runtime.
 *
 * These functions are PURE + label-free: only counts, durations, and ratios
 * reach them, never a URL, host, route key, or any customer string. The route
 * unit for confidence/budget/baseline is a sanitized navigation label produced
 * by the kit's existing `normalizeRouteLabel` mechanism — the label itself
 * never enters these scorers (only ordered duration series do).
 *
 * Axes added here (web set):
 *   - confidence  — how much data backs the numbers (least-sampled scored route)
 *   - budget      — share of scored routes on their learned load budget
 *   - baseline    — per-route regression vs. its own earlier history
 *   - network     — outbound-call reliability (durations + failure/stall rate)
 *   - stability   — rate of ≥50ms main-thread freezes
 *   - idle        — main-thread busyness while the page is NOT being driven
 *   - scroll      — frame jank while actively scrolling
 *   - frameFloor  — worst completed 1s window's effective FPS while scrolling
 *   - timerHealth — growth trend of outstanding set/interval timer handles
 *   - swallowedErrors — errors the host LOGGED but did not crash on (near miss)
 */

import { linearScore, ratingFor, type Rating } from "./thresholds";
import { SCORE_THRESHOLDS } from "./thresholds";

import {
  MIN_RATE_WINDOW_MS,
  earnedPerHour,
  earnedPerMin,
  windowMinOf,
} from "./rateHonesty";
/** A rating that can also be "pending" while an axis is still warming up. */
export type AxisRating = Rating | "pending";

/** Higher-is-better inversion onto linearScore's lower-is-better curve: score
 *  the SHORTFALL below the good floor (0 shortfall = 100). Mirrors RN's
 *  computeFrameFloor inversion. */
function scoreHigherIsBetter(value: number, goodFloor: number, poorFloor: number): number {
  const shortfall = Math.max(0, goodFloor - value);
  return linearScore(shortfall, 0, goodFloor - poorFloor);
}

/* ─── Confidence (sample count) ──────────────────────────────────────────
 * Mirrors RN's CONFIDENCE_CUTOFFS + confidenceLevel/Rating/Caption. The
 * sample count is the LEAST-sampled SCORED route (routes not scorable yet are
 * excluded). 0 => none/pending, <5 => low/poor, <20 => medium/needs-work,
 * >=20 => high/good.
 */
export const CONFIDENCE_CUTOFFS = { medium: 5, high: 20 } as const;
export type ConfidenceLevel = "none" | "low" | "medium" | "high";

export function confidenceLevel(sampleCount: number): ConfidenceLevel {
  if (!sampleCount || sampleCount <= 0) return "none";
  if (sampleCount < CONFIDENCE_CUTOFFS.medium) return "low";
  if (sampleCount < CONFIDENCE_CUTOFFS.high) return "medium";
  return "high";
}

export function confidenceRatingFor(level: ConfidenceLevel): AxisRating {
  return level === "high"
    ? "good"
    : level === "medium"
      ? "needs-work"
      : level === "low"
        ? "poor"
        : "pending";
}

export type ConfidenceCaption =
  | "no samples yet"
  | "few samples"
  | "moderate samples"
  | "well-sampled";

const CONFIDENCE_CAPTIONS: Record<ConfidenceLevel, ConfidenceCaption> = {
  none: "no samples yet",
  low: "few samples",
  medium: "moderate samples",
  high: "well-sampled",
};

export function confidenceCaptionFor(level: ConfidenceLevel): ConfidenceCaption {
  return CONFIDENCE_CAPTIONS[level];
}

/* ─── Budget compliance ──────────────────────────────────────────────────
 * "How many of the routes we can actually score are already on their learned
 * budget?" Mirrors the Node/Python learned-budget model: per route, learn a
 * budget from early samples, mark a route regressed when the recent window
 * exceeds it. This scorer takes the already-derived per-route budget states
 * (learning / on-budget / regressed) so the label never enters this layer.
 */
export interface BudgetCompliance {
  /** Scored routes currently on budget (not regressed). */
  onBudget: number;
  /** Scored routes (excludes still-learning routes). */
  total: number;
  /** Percentage on budget (0-100), or null when nothing is scored yet. */
  pct: number | null;
  /** 0-100 score, or null while pending. */
  score: number | null;
  rating: AxisRating;
}

/** Per-route budget verdict (label-free): whether the route has enough samples
 *  to be scored, and if so whether its recent window regressed past budget. */
export interface RouteBudgetVerdict {
  scored: boolean;
  regressed: boolean;
}

export function computeBudgetCompliance(
  verdicts: RouteBudgetVerdict[],
): BudgetCompliance {
  const scored = verdicts.filter((v) => v && v.scored);
  if (scored.length === 0) {
    return { onBudget: 0, total: 0, pct: null, score: null, rating: "pending" };
  }
  const onBudget = scored.filter((v) => !v.regressed).length;
  const pct = Math.round((onBudget / scored.length) * 100);
  // Score IS the on-budget percentage (higher is better); rating uses the
  // shared bands directly, matching RN's computeBudgetCompliance.
  return { onBudget, total: scored.length, pct, score: pct, rating: ratingFor(pct) };
}

/* ─── Baseline (anomaly vs. its own normal) ──────────────────────────────
 * Mirrors RN's computeBaselineScore exactly: per route with >=6 mounts, split
 * the ordered durations into a BASELINE window (all but the last 3) and a
 * RECENT window (last 3); anomaly ratio = recentMedian / baselineMedian; a
 * route is anomalous only when it is BOTH materially slower (ratio >= 1.2) AND
 * the absolute jump clears 50ms. Score the WORST eligible ratio (1.2 => 100,
 * 2.0 => 0). Medians, not means. Pending until >=1 route has enough mounts.
 */
export const BASELINE_THRESHOLDS = { good: 1.2, poor: 2 } as const;
export const BASELINE_MIN_SAMPLES = 6;
export const BASELINE_RECENT_N = 3;
export const BASELINE_MIN_DELTA_MS = 50;

/** Chronologically-ordered (oldest→newest) durations for ONE route. The route
 *  key never enters this layer — the series is label-free. */
export interface RouteSeriesLike {
  durations: number[];
}

export interface BaselineResult {
  score: number | null;
  rating: AxisRating;
  worstRatio: number | null;
  worstBaselineMs: number | null;
  worstCurrentMs: number | null;
  anomalyCount: number;
  /** Routes that completed a comparison — NOT routes that merely had enough
   *  samples. A route whose earlier window is too fast to divide by counts in
   *  `unscoredRoutes` instead, so a caption built from this can never claim
   *  steadiness over a route the axis never examined. */
  scoredRoutes: number;
  /** Routes with enough samples whose earlier-window median was 0ms. Durations
   *  are captured as whole milliseconds, so a sub-millisecond route leaves no
   *  denominator to divide by and its drift is unknowable here. Counted, never
   *  silently dropped. */
  unscoredRoutes: number;
  /** 0 when nothing could be compared, 1 once any route was. Mirrors the
   *  can't-know shape the other axes use (`measurable: 0` + "pending"), so an
   *  abstention is visibly different from a good reading. */
  measurable: 0 | 1;
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[mid - 1] + sorted[mid]) / 2
    : sorted[mid];
}

export function computeBaselineScore(series: RouteSeriesLike[]): BaselineResult {
  let scoredRoutes = 0;
  let unscoredRoutes = 0;
  let anomalyCount = 0;
  let worstRatio: number | null = null;
  let worstBaselineMs: number | null = null;
  let worstCurrentMs: number | null = null;

  for (const route of series) {
    const durations = route?.durations;
    if (!Array.isArray(durations) || durations.length < BASELINE_MIN_SAMPLES) {
      continue;
    }
    const recent = durations.slice(-BASELINE_RECENT_N);
    const base = durations.slice(0, -BASELINE_RECENT_N);
    const baseMed = median(base);
    const recentMed = median(recent);
    // A sub-millisecond route records every duration as 0 (whole-ms capture),
    // so its earlier window medians to 0 and there is no ratio to take. Abstain
    // for this route and SAY SO: counting it as scored here is what let a
    // hundred-fold regression on a fast route report "steady".
    if (baseMed <= 0) {
      unscoredRoutes++;
      continue;
    }
    scoredRoutes++;
    const ratio = recentMed / baseMed;
    const delta = recentMed - baseMed;
    if (ratio < BASELINE_THRESHOLDS.good || delta < BASELINE_MIN_DELTA_MS) {
      continue;
    }
    anomalyCount++;
    if (worstRatio === null || ratio > worstRatio) {
      worstRatio = ratio;
      worstBaselineMs = Math.round(baseMed);
      worstCurrentMs = Math.round(recentMed);
    }
  }

  if (scoredRoutes === 0) {
    // Nothing was compared, so there is no verdict — never a perfect score.
    // `unscoredRoutes` tells the two silences apart downstream: 0 = no route
    // has enough samples yet (warming up), >0 = every route was too fast to
    // divide by (cannot tell).
    return {
      score: null,
      rating: "pending",
      worstRatio: null,
      worstBaselineMs: null,
      worstCurrentMs: null,
      anomalyCount: 0,
      scoredRoutes: 0,
      unscoredRoutes,
      measurable: 0,
    };
  }
  if (worstRatio === null) {
    return {
      score: 100,
      rating: "good",
      worstRatio: null,
      worstBaselineMs: null,
      worstCurrentMs: null,
      anomalyCount: 0,
      scoredRoutes,
      unscoredRoutes,
      measurable: 1,
    };
  }
  const score = linearScore(
    worstRatio,
    BASELINE_THRESHOLDS.good,
    BASELINE_THRESHOLDS.poor,
  );
  return {
    score,
    rating: ratingFor(score),
    worstRatio: Math.round(worstRatio * 100) / 100,
    worstBaselineMs,
    worstCurrentMs,
    anomalyCount,
    scoredRoutes,
    unscoredRoutes,
    measurable: 1,
  };
}

/* ─── Network reliability ─────────────────────────────────────────────────
 * Mirrors RN's computeNetworkScore: >=3 attempts required; score = the WORSE
 * of two sub-scores — p75 latency (good 800ms · poor 3000ms) and stall rate
 * ((timeout + stall) / attempts, good 1% · poor 10%). "failed" loud errors are
 * surfaced as context but NOT scored. Every field is a count or a duration.
 *
 * In the browser the ONLY honest observation point is Resource Timing (the same
 * source the request-burst counter uses): it fires per COMPLETED request, so
 * this runtime can measure durations + loud failures but cannot see a request
 * that never returns. stallCount/timeoutCount therefore stay 0 in the browser
 * (we never fabricate them) and the axis effectively scores on latency + the
 * observed failure rate — which folds into the same stall-rate sub-score.
 */
export const NETWORK_THRESHOLDS = {
  p75GoodMs: 800,
  p75PoorMs: 3000,
  stallRateGood: 0.01,
  stallRatePoor: 0.1,
} as const;

export const NETWORK_MIN_ATTEMPTS = 3;

export interface NetworkStatsLike {
  attemptCount: number;
  completedCount: number;
  failedCount: number;
  timeoutCount: number;
  stallCount: number;
  p75Ms: number;
  worstMs: number;
}

export interface NetworkResult {
  score: number | null;
  rating: AxisRating;
  p75Ms: number | null;
  stallPct: number | null;
  attemptCount: number;
  timeoutCount: number;
  stallCount: number;
  failedCount: number;
  worstMs: number;
}

export function computeNetworkScore(stats: NetworkStatsLike): NetworkResult {
  if (!stats || stats.attemptCount < NETWORK_MIN_ATTEMPTS) {
    return {
      score: null,
      rating: "pending",
      p75Ms: null,
      stallPct: null,
      attemptCount: stats?.attemptCount ?? 0,
      timeoutCount: stats?.timeoutCount ?? 0,
      stallCount: stats?.stallCount ?? 0,
      failedCount: stats?.failedCount ?? 0,
      worstMs: Math.round(stats?.worstMs ?? 0),
    };
  }
  // In the browser, loud failures ARE the observable silent-drop class (a
  // failed resource entry is the closest analogue to RN's stall/timeout), so
  // they fold into the stall-rate sub-score alongside any host-reported
  // timeout/stall counts. RN's raw "failed" context count is still surfaced.
  const dropCount = stats.timeoutCount + stats.stallCount + stats.failedCount;
  const stallRate = dropCount / stats.attemptCount;
  const latencyScore = linearScore(
    stats.p75Ms,
    NETWORK_THRESHOLDS.p75GoodMs,
    NETWORK_THRESHOLDS.p75PoorMs,
  );
  const stallScore = linearScore(
    stallRate,
    NETWORK_THRESHOLDS.stallRateGood,
    NETWORK_THRESHOLDS.stallRatePoor,
  );
  const score = Math.min(latencyScore, stallScore);
  return {
    score,
    rating: ratingFor(score),
    p75Ms: Math.round(stats.p75Ms),
    stallPct: Math.round(stallRate * 1000) / 10,
    attemptCount: stats.attemptCount,
    timeoutCount: stats.timeoutCount,
    stallCount: stats.stallCount,
    failedCount: stats.failedCount,
    worstMs: Math.round(stats.worstMs),
  };
}

/* ─── Stability (long main-thread freeze rate) ─────────────────────────────
 * Browser parity for RN's scored Stability axis. The Long Tasks API reports
 * main-thread blocks at the same ≥50ms boundary RN uses. We score their
 * session-wide rate with RN's unchanged bands (good ≤2/min · poor ≥20/min).
 *
 * The observation window is an honesty input, not decoration: a supported
 * browser with no entries earns a real zero only after ten seconds. Before
 * then, and when `longtask` observation is unsupported, the result is pending
 * so snapshot assembly omits it rather than manufacturing a green score.
 */
export const STABILITY_THRESHOLDS = { good: 2, poor: 20 } as const;
export const STABILITY_MIN_OBSERVED_MS = 10_000;

export interface StabilityStatsLike {
  supported: boolean;
  longTaskCount: number;
  totalBlockedMs: number;
  worstBlockMs: number;
  observedWindowMs: number;
}

export interface StabilityResult {
  score: number | null;
  rating: AxisRating;
  longTasksPerMin: number | null;
  longTaskCount: number;
  worstBlockMs: number;
  /** The observation the rate came from. Published beside it so no caption
   *  can print a projection without the window that earned it. */
  windowMin: number | null;
}

export function computeStabilityScore(
  stats: StabilityStatsLike,
): StabilityResult {
  const count = Math.max(0, Math.round(stats?.longTaskCount ?? 0));
  const worstBlockMs = Math.max(0, Math.round(stats?.worstBlockMs ?? 0));
  if (
    !stats ||
    !stats.supported ||
    stats.observedWindowMs < STABILITY_MIN_OBSERVED_MS
  ) {
    return {
      score: null,
      rating: "pending",
      longTasksPerMin: null,
      longTaskCount: count,
      worstBlockMs,
      windowMin: null,
    };
  }
  // The earned-rate contract (rateHonesty.ts) — the 10s gate above is already
  // past the per-minute minimum, so this cannot be null.
  const perMin = earnedPerMin(count, stats.observedWindowMs) ?? 0;
  const score = linearScore(
    perMin,
    STABILITY_THRESHOLDS.good,
    STABILITY_THRESHOLDS.poor,
  );
  return {
    score,
    rating: ratingFor(score),
    longTasksPerMin: perMin,
    longTaskCount: count,
    worstBlockMs,
    windowMin: windowMinOf(stats.observedWindowMs),
  };
}

/* ─── Idle efficiency ─────────────────────────────────────────────────────
 * "When the page is NOT being driven, does the main thread go quiet?" Mirrors
 * RN's computeIdleEfficiency thresholds + minimum-sample gate. In the browser
 * the idle "busy" measure is main-thread busy ms (long-task / LoAF blocked
 * time) accumulated over idle wall time (windows with no user input):
 * idleBusyPct = busy CPU ms / idle wall ms * 100. Pending until enough idle
 * wall time has been observed.
 */
export const IDLE_EFFICIENCY_THRESHOLDS = { good: 2, poor: 20 } as const;

/** Minimum idle wall time (ms) before the axis leaves "pending". */
export const IDLE_MIN_WALL_MS = 2_000;

export interface IdleStatsLike {
  /** Main-thread busy ms observed during idle (no-input) windows. */
  idleBusyMs: number;
  /** Idle wall time (ms) observed. */
  idleWallMs: number;
  /** Long tasks observed while idle (context only). */
  idleLongTaskCount: number;
}

export interface IdleResult {
  score: number | null;
  rating: AxisRating;
  idleBusyPct: number | null;
  idleLongTaskCount: number;
}

export function computeIdleEfficiency(stats: IdleStatsLike): IdleResult {
  if (!stats || stats.idleWallMs < IDLE_MIN_WALL_MS) {
    return {
      score: null,
      rating: "pending",
      idleBusyPct: null,
      idleLongTaskCount: stats?.idleLongTaskCount ?? 0,
    };
  }
  const busyPct = (stats.idleBusyMs / stats.idleWallMs) * 100;
  const score = linearScore(
    busyPct,
    IDLE_EFFICIENCY_THRESHOLDS.good,
    IDLE_EFFICIENCY_THRESHOLDS.poor,
  );
  return {
    score,
    rating: ratingFor(score),
    idleBusyPct: Math.round(busyPct * 10) / 10,
    idleLongTaskCount: stats.idleLongTaskCount,
  };
}

/* ─── Scroll / list health ───────────────────────────────────────────────
 * "How smooth does scrolling actually feel?" Mirrors RN's computeScrollHealth
 * bands (jank fraction during scroll: good <=5% · poor >=20%) and its
 * minimum-frame gate. Frames are sampled ONLY while a scroll is in progress
 * (see scrollSampler) so no permanent rAF loop runs in the host page.
 * blankEvents/worstBlankPx have no browser equivalent, so they are 0/0.
 */
export const SCROLL_THRESHOLDS = { good: 0.05, poor: 0.2 } as const;
export const SCROLL_MIN_FRAMES = 60;

export interface ScrollStatsLike {
  frameCount: number;
  jankFraction: number;
  blankEvents: number;
  worstBlankPx: number;
}

export interface ScrollHealthResult {
  score: number | null;
  rating: AxisRating;
  jankPct: number | null;
  frameCount: number;
  blankEvents: number;
  worstBlankPx: number;
}

export function computeScrollHealth(stats: ScrollStatsLike): ScrollHealthResult {
  if (!stats || stats.frameCount < SCROLL_MIN_FRAMES) {
    return {
      score: null,
      rating: "pending",
      jankPct: null,
      frameCount: stats?.frameCount ?? 0,
      blankEvents: stats?.blankEvents ?? 0,
      worstBlankPx: Math.round(stats?.worstBlankPx ?? 0),
    };
  }
  const score = linearScore(
    stats.jankFraction,
    SCROLL_THRESHOLDS.good,
    SCROLL_THRESHOLDS.poor,
  );
  return {
    score,
    rating: ratingFor(score),
    jankPct: Math.round(stats.jankFraction * 1000) / 10,
    frameCount: stats.frameCount,
    blankEvents: stats.blankEvents,
    worstBlankPx: Math.round(stats.worstBlankPx),
  };
}

/* ─── Frame Floor ─────────────────────────────────────────────────────────
 * "How bad was the WORST second while scrolling?" Mirrors RN's
 * computeFrameFloor: score the worst completed 1s window's effective FPS
 * (higher is better; floor >=50fps = 100, <=20fps = 0). Pending until enough
 * completed 1s windows exist. Web samples frames only during scroll, so this
 * stays absent until the user has scrolled enough.
 */
export const FRAME_FLOOR_THRESHOLDS = { goodFps: 50, poorFps: 20 } as const;
/** Mirrors RN's FRAME_FLOOR_MIN_WINDOWS. Below this, one warm-up second would
 *  define the whole session's floor — and a lower gate here would make the web
 *  kit publish a floor while RN still called the same data pending. */
export const FRAME_FLOOR_MIN_WINDOWS = 5;

export interface FrameFloorStatsLike {
  floorFps: number;
  windowCount: number;
}

export interface FrameFloorResult {
  score: number | null;
  rating: AxisRating;
  floorFps: number | null;
  windowCount: number;
}

export function computeFrameFloor(stats: FrameFloorStatsLike): FrameFloorResult {
  if (
    !stats ||
    stats.windowCount < FRAME_FLOOR_MIN_WINDOWS ||
    stats.floorFps <= 0
  ) {
    return {
      score: null,
      rating: "pending",
      floorFps: null,
      windowCount: stats?.windowCount ?? 0,
    };
  }
  const score = scoreHigherIsBetter(
    stats.floorFps,
    FRAME_FLOOR_THRESHOLDS.goodFps,
    FRAME_FLOOR_THRESHOLDS.poorFps,
  );
  return {
    score,
    rating: ratingFor(score),
    floorFps: Math.round(stats.floorFps),
    windowCount: stats.windowCount,
  };
}

/* ─── Timer Health ────────────────────────────────────────────────────────
 * "Are outstanding timer handles growing over time (a leak)?" Mirrors RN's
 * timerHealth: score the growth TREND of outstanding set/interval handles
 * (handles added per minute of observed time). A steady app parks its timers;
 * a leak grows the outstanding count without bound. Pending until enough
 * samples span enough time. good <=5/min · poor >=60/min growth.
 */
export const TIMER_HEALTH_THRESHOLDS = { good: 5, poor: 60 } as const;
export const TIMER_HEALTH_MIN_SAMPLES = 3;
export const TIMER_HEALTH_MIN_ELAPSED_MS = 10_000;

export interface TimerStatsLike {
  /** Current outstanding set/interval handles. */
  outstanding: number;
  /** Growth in outstanding handles per minute over the observed window. */
  growthPerMin: number;
  /** Samples backing the trend. */
  sampleCount: number;
  /** Wall time (ms) spanned by the samples. */
  elapsedMs: number;
}

export interface TimerHealthResult {
  score: number | null;
  rating: AxisRating;
  timers: number;
  growthPerMin: number | null;
}

export function computeTimerHealth(stats: TimerStatsLike): TimerHealthResult {
  const timers = Math.max(0, Math.round(stats?.outstanding ?? 0));
  if (
    !stats ||
    stats.sampleCount < TIMER_HEALTH_MIN_SAMPLES ||
    stats.elapsedMs < TIMER_HEALTH_MIN_ELAPSED_MS
  ) {
    return { score: null, rating: "pending", timers, growthPerMin: null };
  }
  const growth = Math.max(0, stats.growthPerMin);
  const score = linearScore(
    growth,
    TIMER_HEALTH_THRESHOLDS.good,
    TIMER_HEALTH_THRESHOLDS.poor,
  );
  return {
    score,
    rating: ratingFor(score),
    timers,
    growthPerMin: Math.round(growth * 10) / 10,
  };
}

/* ─── CPU / thread / scheduling readings ──────────────────────────────────
 * Passive browser observations only. These are additive/display-only and
 * never feed the shared Speed score.
 */
export const BLOCKING_ASYNC_THRESHOLDS = { good: 0.5, poor: 10 } as const;
export const BLOCKING_ASYNC_MIN_WINDOW_MS = 60_000;
/** The Long Tasks API's platform-defined floor. Kept named so the event being
 * counted is explicit even though the browser, rather than this kit, applies it. */
export const BLOCKING_ASYNC_LONG_TASK_MS = 50;

export interface BlockingAsyncStatsLike {
  supported: boolean;
  count: number;
  worstMs: number;
  windowMs: number;
}

export interface BlockingAsyncResult {
  score: number | null;
  rating: AxisRating;
  count: number;
  perMin: number | null;
  worstMs: number;
  windowMin: number | null;
}

export function computeBlockingAsync(
  stats: BlockingAsyncStatsLike,
): BlockingAsyncResult {
  const count = Math.max(0, Math.round(stats?.count ?? 0));
  const worstMs = Math.max(0, Math.round(stats?.worstMs ?? 0));
  if (
    !stats ||
    !stats.supported ||
    stats.windowMs < BLOCKING_ASYNC_MIN_WINDOW_MS
  ) {
    return {
      score: null,
      rating: "pending",
      count,
      perMin: null,
      worstMs,
      windowMin: null,
    };
  }
  const perMin = earnedPerMin(count, stats.windowMs);
  // A window too short to earn a rate withholds the rate AND the verdict, the
  // same answer as an unsupported browser rather than a rate of zero.
  if (perMin === null) {
    return {
      score: null,
      rating: "pending",
      count,
      perMin: null,
      worstMs,
      windowMin: null,
    };
  }
  const score = linearScore(
    perMin,
    BLOCKING_ASYNC_THRESHOLDS.good,
    BLOCKING_ASYNC_THRESHOLDS.poor,
  );
  return {
    score,
    rating: ratingFor(score),
    count,
    perMin,
    worstMs,
    windowMin: windowMinOf(stats.windowMs),
  };
}

export const ASYNC_SLOW_CALLBACK_THRESHOLDS = { good: 0.2, poor: 10 } as const;
export const ASYNC_SLOW_CALLBACK_MIN_WINDOW_MS = 60_000;
/** A timer callback occupying one long-task-sized slice is slow. */
export const ASYNC_SLOW_CALLBACK_MS = 50;

export interface AsyncSlowCallbackStatsLike {
  count: number;
  windowMs: number;
  installed: boolean;
}

export interface AsyncSlowCallbacksResult {
  score: number | null;
  rating: AxisRating;
  count: number;
  perMin: number | null;
  windowMin: number | null;
}

export function computeAsyncSlowCallbacks(
  stats: AsyncSlowCallbackStatsLike,
): AsyncSlowCallbacksResult {
  const count = Math.max(0, Math.round(stats?.count ?? 0));
  if (
    !stats ||
    !stats.installed ||
    stats.windowMs < ASYNC_SLOW_CALLBACK_MIN_WINDOW_MS
  ) {
    return {
      score: null,
      rating: "pending",
      count,
      perMin: null,
      windowMin: null,
    };
  }
  const perMin = earnedPerMin(count, stats.windowMs);
  // Too short a window to earn a rate: withhold the rate and the verdict
  // rather than publish a rate of zero over a window nobody watched.
  if (perMin === null) {
    return {
      score: null,
      rating: "pending",
      count,
      perMin: null,
      windowMin: null,
    };
  }
  const score = linearScore(
    perMin,
    ASYNC_SLOW_CALLBACK_THRESHOLDS.good,
    ASYNC_SLOW_CALLBACK_THRESHOLDS.poor,
  );
  return {
    score,
    rating: ratingFor(score),
    count,
    perMin,
    windowMin: windowMinOf(stats.windowMs),
  };
}

export const EVENT_LOOP_LAG_THRESHOLDS = { goodMs: 20, poorMs: 200 } as const;
export const EVENT_LOOP_LAG_MIN_SAMPLES = 20;

export interface EventLoopLagStatsLike {
  samples: number[];
  windowMs: number;
}

export interface EventLoopLagResult {
  score: number | null;
  rating: AxisRating;
  lagMs: number | null;
  p95Ms: number | null;
  sampleCount: number;
  windowMin: number | null;
}

export function computeEventLoopLag(
  stats: EventLoopLagStatsLike,
): EventLoopLagResult {
  const samples = (stats?.samples ?? []).filter(
    (v) => Number.isFinite(v) && v >= 0,
  );
  if (samples.length < EVENT_LOOP_LAG_MIN_SAMPLES) {
    return {
      score: null,
      rating: "pending",
      lagMs: null,
      p95Ms: null,
      sampleCount: samples.length,
      windowMin: null,
    };
  }
  const lagMs = Math.round(samples[samples.length - 1]);
  const p95Ms = Math.round(percentileOf(samples, 0.95));
  const score = linearScore(
    p95Ms,
    EVENT_LOOP_LAG_THRESHOLDS.goodMs,
    EVENT_LOOP_LAG_THRESHOLDS.poorMs,
  );
  return {
    score,
    rating: ratingFor(score),
    lagMs,
    p95Ms,
    sampleCount: samples.length,
    windowMin: windowMinOf(Math.max(0, stats.windowMs)),
  };
}

/* ─── Interaction to Next Paint (INP) ─────────────────────────────────────
 * "How slow did the page FEEL when the user tapped/typed?" The Google Core
 * Web Vital that replaced FID — the one number every mainstream web perf tool
 * (Lighthouse, CrUX, PageSpeed) reports, so our bands must match theirs
 * exactly or the meter argues with the tools developers already trust.
 *
 * Bands (Google-published, = SCORE_THRESHOLDS.inp — a future port to another
 * runtime must NOT re-pick these): good ≤200ms · poor ≥500ms.
 *
 * Estimator (web-vitals-library style, fed by the existing event-timing
 * PerformanceObserver in vitals.ts — no new sampling loop): interactions are
 * deduped by `interactionId`, the 10 longest are kept, the total interaction
 * count is derived from the id range (ids advance by 7 per interaction), and
 * INP is the (count/50)th-from-worst duration. Distinct from the frustration
 * axis, which keeps the single worst delay as a proxy.
 *
 * Warm-up gate (ports must keep this too): pending until
 * INP_MIN_INTERACTIONS distinct interactions have been measured — a single
 * warm-up tap would otherwise define the whole session. Browsers without
 * event-timing `interactionId` support honestly never publish this axis.
 */
export const INP_MIN_INTERACTIONS = 3;

export interface InpStatsLike {
  /** Current INP estimate (ms), or null before any interaction measured. */
  inpMs: number | null;
  /** Estimated distinct interactions this session. */
  interactionCount: number;
}

export interface InpResult {
  score: number | null;
  rating: AxisRating;
  inpMs: number | null;
  interactionCount: number;
}

export function computeInteractionToNextPaint(stats: InpStatsLike): InpResult {
  if (
    !stats ||
    stats.inpMs === null ||
    !Number.isFinite(stats.inpMs) ||
    stats.interactionCount < INP_MIN_INTERACTIONS
  ) {
    return {
      score: null,
      rating: "pending",
      inpMs: null,
      interactionCount: stats?.interactionCount ?? 0,
    };
  }
  const score = linearScore(
    stats.inpMs,
    SCORE_THRESHOLDS.inp.good,
    SCORE_THRESHOLDS.inp.poor,
  );
  return {
    score,
    rating: ratingFor(score),
    inpMs: Math.round(stats.inpMs),
    interactionCount: stats.interactionCount,
  };
}

/* ─── Swallowed errors (Near-Miss Rate) ──────────────────────────────────
 * "How often does the host app LOG an error but keep running?" — a near miss.
 * Ported from the Python kit's `swallowedErrors` axis
 * (`lib/boosthis-py/boosthis/extra_meters.py`, `_read_swallowed_errors`) with
 * its gates, bands, and rounding UNCHANGED so the same number means the same
 * thing in every runtime:
 *   - no minimum count,
 *   - a 5-minute minimum window before the axis reports at all,
 *   - the trailing window capped at 60 minutes,
 *   - bands 1/hour = good · 60/hour = poor.
 * The observation point (chained `console.error`) and window/ring state live in
 * `swallowedErrors.ts`; this layer is PURE — only a count + an elapsed window
 * reach it, never a message, logger name, or stack.
 */
export const SWALLOWED_THRESHOLDS = { good: 1, poor: 60 } as const;
/** No clean bill until at least 5 minutes have been watched (mirrors Python's
 *  SWALLOWED_MIN_WINDOW_MS). */
export const SWALLOWED_MIN_WINDOW_MS = MIN_RATE_WINDOW_MS;

export interface SwallowedStatsLike {
  /** Errors logged in the (capped) trailing window. */
  count: number;
  /** The trailing window length in ms (already capped at 60 min by the caller). */
  windowMs: number;
}

export interface SwallowedErrorsResult {
  score: number | null;
  rating: AxisRating;
  count: number;
  perHour: number | null;
  windowMin: number | null;
}

export function computeSwallowedErrors(
  stats: SwallowedStatsLike,
): SwallowedErrorsResult {
  const windowMs = stats?.windowMs ?? 0;
  const count = Math.max(0, Math.round(stats?.count ?? 0));
  if (windowMs < SWALLOWED_MIN_WINDOW_MS) {
    // too early for a clean bill to be honest
    return { score: null, rating: "pending", count, perHour: null, windowMin: null };
  }
  // The earned-rate contract (rateHonesty.ts) — the gate above already holds
  // the reading until the window earns the projection, so this cannot be null.
  const perHour = earnedPerHour(count, windowMs) ?? 0;
  const score = linearScore(
    perHour,
    SWALLOWED_THRESHOLDS.good,
    SWALLOWED_THRESHOLDS.poor,
  );
  return {
    score,
    rating: ratingFor(score),
    count,
    perHour,
    windowMin: windowMinOf(windowMs),
  };
}

/* ─── Leak Watch (additive; NEVER feeds the Speed score) ──────────────────
 * "Is the host app leaking stack traces, secrets/keys, or personal details to
 * its OWN users?" — a self-inflicted information-disclosure signal. Shared
 * cross-runtime axis (`leakWatch`, label "Leak Watch"). Runtime evidence only;
 * additive/display-only, exactly like swallowedErrors.
 *
 * Gates + window MIRROR swallowedErrors: a 5-minute minimum observation window
 * before the axis reports at all, a 60-minute trailing window, a bounded ring.
 * Scoring is its OWN band (per the shared contract): a clean window is 100, and
 * ANY observed leak is at best "needs-work":
 *   - count === 0  → score 100,
 *   - count  >  0  → max(0, 70 - round(perHour * 10)).
 * This layer is PURE — only counts + an elapsed window reach it, never the
 * matched text/value, the log line, the response body, or the route/path name.
 */
export const LEAK_MIN_WINDOW_MS = MIN_RATE_WINDOW_MS;

export interface LeakStatsLike {
  /** Total leak observations in the (capped) trailing window. */
  count: number;
  /** Per-category split (each observation is in exactly ONE category, so
   *  stackCount + secretCount + piiCount === count). */
  stackCount: number;
  secretCount: number;
  piiCount: number;
  /** The trailing window length in ms (already capped at 60 min by the caller). */
  windowMs: number;
}

export interface LeakWatchResult {
  score: number | null;
  rating: AxisRating;
  count: number;
  perHour: number | null;
  windowMin: number | null;
  stackCount: number;
  secretCount: number;
  piiCount: number;
  /** Human caption — COUNTS + CATEGORY WORDS ONLY, never any matched content.
   *  Reads as absence-of-evidence when clean (never "you are protected"). */
  caption: string | null;
}

/** Build the honest caption: counts + category words ONLY. Never the matched
 *  text, value, log line, response body, or route/path name. */
function leakCaption(
  count: number,
  stackCount: number,
  secretCount: number,
  piiCount: number,
): string {
  if (count === 0) return "no leaks observed in this window";
  const parts: string[] = [];
  if (secretCount > 0) parts.push(`${secretCount} secret-like`);
  if (piiCount > 0) parts.push(`${piiCount} personal-detail`);
  if (stackCount > 0) parts.push(`${stackCount} stack trace`);
  const detail = parts.length ? ` (${parts.join(", ")})` : "";
  return `${count} possible leak${count === 1 ? "" : "s"} observed${detail}`;
}

export function computeLeakWatch(stats: LeakStatsLike): LeakWatchResult {
  const windowMs = stats?.windowMs ?? 0;
  const stackCount = Math.max(0, Math.round(stats?.stackCount ?? 0));
  const secretCount = Math.max(0, Math.round(stats?.secretCount ?? 0));
  const piiCount = Math.max(0, Math.round(stats?.piiCount ?? 0));
  const count = stackCount + secretCount + piiCount;
  if (windowMs < LEAK_MIN_WINDOW_MS) {
    // too early to be honest — the snapshot omits the axis while warming
    return {
      score: null,
      rating: "pending",
      count,
      perHour: null,
      windowMin: null,
      stackCount,
      secretCount,
      piiCount,
      caption: null,
    };
  }
  // The earned-rate contract (rateHonesty.ts) — the gate above already holds
  // the reading until the window earns the projection, so this cannot be null.
  const perHour = earnedPerHour(count, windowMs) ?? 0;
  const score =
    count === 0 ? 100 : Math.max(0, 70 - Math.round(perHour * 10));
  return {
    score,
    rating: ratingFor(score),
    count,
    perHour,
    windowMin: windowMinOf(windowMs),
    stackCount,
    secretCount,
    piiCount,
    caption: leakCaption(count, stackCount, secretCount, piiCount),
  };
}

/* ─── Latency-tail helpers (shared) ─────────────────────────────────────── */

/** Nearest-rank percentile of an unordered list (does not mutate). q in (0,1]. */
export function percentileOf(values: number[], q: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.max(
    0,
    Math.min(sorted.length - 1, Math.ceil(q * sorted.length) - 1),
  );
  return sorted[idx];
}

// Re-export the poor-latency threshold so callers deriving budget verdicts can
// stay consistent with the composite's poor band without re-importing.
export const POOR_LATENCY_MS = SCORE_THRESHOLDS.lcp.poor;

/* ─── Dependency distance (how far the app is from its data) ─────────────
 *
 * THE MEASUREMENT, AND WHY IT IS THIS ONE
 * A round trip to a dependency contains the distance AND however long the
 * service spent thinking. Judging distance from a round trip therefore reports
 * a SLOW service as a DISTANT one — and tells someone to move a database that
 * was merely busy. So distance is judged ONLY on connection setup: how long it
 * took to open the connection and secure it, BEFORE a single byte of the
 * request was sent. That number is pure distance; it does not move when the
 * service gets slower or the query gets heavier.
 *
 * HONESTY GATES
 *   • A verdict needs DEPENDENCY_DISTANCE_MIN_CONNECTIONS newly-opened
 *     connections for a kind. On a long-running server connections are pooled
 *     and reused, so new setups are rare — below the floor the reading says
 *     "too few new connections to judge" instead of guessing from two samples.
 *   • The cost is expressed as it is FELT: the share of a typical request spent
 *     purely on distance. With no typical request to compare against, the share
 *     is omitted rather than invented.
 *
 * IN THE BROWSER: connection phases are hidden for anything that is not the
 * app's own origin unless the other side opts in, and almost nobody does. So
 * the browser kit speaks ONLY about the app's own backend and abstains for
 * everything else — never a zero standing in for a reading it cannot take.
 *
 * PRIVACY: closed numeric codes only — a dependency KIND, and the platform's
 * own declared area. No address, hostname, path or place ever reaches here.
 */

/** Setup-time bands. 20 ms is same-region; 150 ms is another continent. */
export const DEPENDENCY_DISTANCE_SETUP_MS_THRESHOLDS = {
  good: 20,
  poor: 150,
} as const;

/** New connections needed for ONE kind before that kind can be judged. */
export const DEPENDENCY_DISTANCE_MIN_CONNECTIONS = 5;

/** One kind's newly-opened-connection setup times, label-free. */
export interface DependencyDistanceKindStat {
  /** Closed code from depKinds.ts — never a name. */
  kind: number;
  /** How many NEW connections to this kind were timed. */
  connections: number;
  /** Median setup (connect + secure) in ms across those connections. */
  setupMs: number;
}

export interface DependencyDistanceStatsLike {
  kinds: DependencyDistanceKindStat[];
  /** Every timed new connection, including kinds still under the floor. */
  newConnections: number;
  /** A typical request in THIS app (p50), or null when nothing is measured. */
  typicalRequestMs: number | null;
  /** The platform's OWN declared area code; 0 when it declared nothing. */
  hostArea: number;
  /** 1 where the runtime can only time the app's own backend (the browser). */
  ownBackendOnly: number;
}

export interface DependencyDistanceKindRow {
  kind: number;
  connections: number;
  setupMs: number;
  /** Share of a typical request spent purely on distance; omitted when there
   *  is no typical request to compare against. */
  sharePct?: number;
}

export interface DependencyDistanceResult {
  score: number | null;
  rating: AxisRating;
  /** 1 once at least one kind cleared the floor; 0 while it cannot judge. */
  measurable: number;
  newConnections: number;
  minConnections: number;
  /** How many kinds cleared the floor. */
  judgedKinds: number;
  /** The furthest judged kind, or 0 when nothing could be judged. */
  worstKind: number;
  worstSetupMs: number;
  worstSharePct: number | null;
  /**
   * A LARGER READING THIS AXIS HELD AND DID NOT JUDGE.
   *
   * A kind below the connection floor is excluded from the verdict — rightly:
   * two handshakes cannot establish a distance. But the tile then published
   * the smaller, judged figure under the word "worst" while carrying a bigger
   * one in its own rows. A "worst" must never be smaller than something the
   * same reading holds, so the excluded maximum travels beside it.
   *
   * Null when nothing larger was excluded, which is the ordinary case.
   */
  unjudgedWorstSetupMs: number | null;
  unjudgedWorstKind: number | null;
  unjudgedWorstConnections: number | null;
  /** How many kinds were held back for having too few connections. */
  unjudgedKinds: number;
  /** 0 when the app has no typical request yet. */
  typicalRequestMs: number;
  hostArea: number;
  ownBackendOnly: number;
  kinds: DependencyDistanceKindRow[];
}

function ms1(v: number): number {
  return Math.max(0, Math.round(v * 10) / 10);
}

export function computeDependencyDistance(
  stats: DependencyDistanceStatsLike,
): DependencyDistanceResult {
  const typical =
    typeof stats?.typicalRequestMs === "number" &&
    Number.isFinite(stats.typicalRequestMs) &&
    stats.typicalRequestMs > 0
      ? stats.typicalRequestMs
      : 0;
  const shareOf = (setupMs: number): number | null =>
    typical > 0
      ? Math.max(0, Math.min(100, Math.round((setupMs / typical) * 1000) / 10))
      : null;

  const rows: DependencyDistanceKindRow[] = [];
  let worstKind = 0;
  let worstSetupMs = 0;
  let judgedKinds = 0;
  // The excluded side of the same sweep, so an unjudged reading larger than
  // the published worst can never be invisible.
  let unjudgedKinds = 0;
  let unjudgedWorstSetupMs: number | null = null;
  let unjudgedWorstKind: number | null = null;
  let unjudgedWorstConnections: number | null = null;

  for (const k of stats?.kinds ?? []) {
    if (!k || !Number.isFinite(k.kind) || k.kind <= 0) continue;
    const connections = Math.max(0, Math.round(k.connections ?? 0));
    if (connections <= 0) continue;
    const setupMs = ms1(k.setupMs ?? 0);
    const row: DependencyDistanceKindRow = {
      kind: Math.round(k.kind),
      connections,
      setupMs,
    };
    const share = shareOf(setupMs);
    if (share !== null) row.sharePct = share;
    rows.push(row);
    // A kind is only JUDGED once it has enough new connections of its own.
    if (connections >= DEPENDENCY_DISTANCE_MIN_CONNECTIONS) {
      judgedKinds++;
      if (setupMs > worstSetupMs) {
        worstSetupMs = setupMs;
        worstKind = row.kind;
      }
    } else {
      unjudgedKinds++;
      if (unjudgedWorstSetupMs === null || setupMs > unjudgedWorstSetupMs) {
        unjudgedWorstSetupMs = setupMs;
        unjudgedWorstKind = row.kind;
        unjudgedWorstConnections = connections;
      }
    }
  }
  rows.sort((a, b) => b.setupMs - a.setupMs || a.kind - b.kind);

  const newConnections = Math.max(0, Math.round(stats?.newConnections ?? 0));
  const hostArea = Math.max(0, Math.round(stats?.hostArea ?? 0));
  const ownBackendOnly = stats?.ownBackendOnly ? 1 : 0;

  if (judgedKinds === 0) {
    // Too few newly-opened connections to judge. This is a STATE, not a zero:
    // the counts still ship so the reading can say how far off a verdict is.
    return {
      score: null,
      rating: "pending",
      measurable: 0,
      newConnections,
      minConnections: DEPENDENCY_DISTANCE_MIN_CONNECTIONS,
      judgedKinds: 0,
      worstKind: 0,
      worstSetupMs: 0,
      worstSharePct: null,
      unjudgedWorstSetupMs,
      unjudgedWorstKind,
      unjudgedWorstConnections,
      unjudgedKinds,
      typicalRequestMs: Math.round(typical),
      hostArea,
      ownBackendOnly,
      kinds: rows,
    };
  }

  const score = linearScore(
    worstSetupMs,
    DEPENDENCY_DISTANCE_SETUP_MS_THRESHOLDS.good,
    DEPENDENCY_DISTANCE_SETUP_MS_THRESHOLDS.poor,
  );
  return {
    score,
    rating: ratingFor(score),
    measurable: 1,
    newConnections,
    minConnections: DEPENDENCY_DISTANCE_MIN_CONNECTIONS,
    judgedKinds,
    worstKind,
    worstSetupMs,
    worstSharePct: shareOf(worstSetupMs),
    // Only interesting when it is BIGGER than the published worst — a smaller
    // excluded reading changes nothing about what "worst" means.
    unjudgedWorstSetupMs:
      unjudgedWorstSetupMs !== null && unjudgedWorstSetupMs > worstSetupMs
        ? unjudgedWorstSetupMs
        : null,
    unjudgedWorstKind:
      unjudgedWorstSetupMs !== null && unjudgedWorstSetupMs > worstSetupMs
        ? unjudgedWorstKind
        : null,
    unjudgedWorstConnections:
      unjudgedWorstSetupMs !== null && unjudgedWorstSetupMs > worstSetupMs
        ? unjudgedWorstConnections
        : null,
    unjudgedKinds,
    typicalRequestMs: Math.round(typical),
    hostArea,
    ownBackendOnly,
    kinds: rows,
  };
}

/* ─── AI calls: how long the AI part of a turn takes ─────────────────────
 *
 * An AI product built as a front end usually calls the model FROM THE PAGE, so
 * the slowest and by far the most expensive thing it does happened where this
 * kit was already watching and said nothing about it. This axis reports the
 * SHAPE of that wait — how long the calls took, how long before the first
 * words of a streamed answer appeared, whether a stream went quiet halfway,
 * and which of the failures that actually happen to AI calls happened here.
 *
 * SAME NUMBERS, SAME NAMES AS THE NODE KIT. Every band, gate and field name
 * mirrors `lib/boosthis-runtime-node/src/meterAxes.ts`, so the tile, the AI
 * answers and the rules need no browser special case. What differs is only
 * what a browser can honestly measure, and that difference is carried in the
 * data rather than in a different shape:
 *
 *   • The unit of work is the USER'S TURN, not an inbound request. There is no
 *     server request to divide by in a page; the honest denominator is the
 *     wait a person sat through, which begins at the input that sent the
 *     prompt. `watchedRequests` therefore counts turns that made AI calls.
 *   • A cross-origin reply only exposes the headers the provider CHOSE to
 *     expose, so the headroom reading below can be present-but-unreadable —
 *     an abstention with a reason, never a comfortable zero.
 *
 * WHAT IS SCORED, AND WHAT DELIBERATELY IS NOT.
 *   • The share of a turn spent on AI is REPORTED and never scored. Time spent
 *     waiting for a model is the work the app exists to do.
 *   • Failures ARE scored: a call that never delivered an answer is time and
 *     money spent for nothing, whoever's fault it was.
 *   • Time to the first words IS scored, for streamed answers only. On a
 *     stream that is the number a person experiences; total duration is close
 *     to meaningless because it tracks the length of the answer.
 *
 * ADDITIVE + DISPLAY-ONLY: never feeds the composite Speed score.
 */

/** Share of AI calls that delivered nothing. Good at none, poor at 1 in 10. */
export const AI_FAIL_PCT_THRESHOLDS = { good: 0, poor: 10 } as const;
/**
 * Time to the first words of a streamed answer.
 *
 * Good at 1s and poor at 8s: under a second the answer feels immediate, and
 * past eight a reader has already decided the app is broken. Applied only to
 * streamed replies, and only once some have been seen.
 */
export const AI_TTFT_MS_THRESHOLDS = { good: 1000, poor: 8000 } as const;

/**
 * Turns with AI work needed before the axis leaves "pending".
 *
 * Five, the same floor every other axis in this kit uses (snapshot.ts's
 * MIN_SAMPLES_FOR_AXES) and the same number the Node kit requires of watched
 * requests — declared here rather than imported so this module stays pure.
 */
export const AI_WORK_MIN_TURNS = 5;

/** Minimal label-free aggregate consumed from the AI-call detector. Every
 *  member is a number: no provider name, host, URL, model, prompt or answer
 *  can reach a scorer, because none of them is in this shape. */
export interface AiCallStatsLike {
  /** Turns (the browser's unit of work) that made at least one AI call. */
  watchedRequests: number;
  watchedRequestMs: number;
  aiMs: number;
  callCount: number;
  providerCount: number;
  /** 1-based position in the maintained provider list; 0 when none. */
  topProvider: number;
  p75Ms: number;
  worstMs: number;
  streamCount: number;
  ttftP75Ms: number;
  stallCount: number;
  failCount: number;
  rateLimitedCount: number;
  quotaCount: number;
  timeoutCount: number;
  truncatedCount: number;
  filteredCount: number;
  serverMsP75: number;
  serverMsCalls: number;
  unwatchedClients: number;
  /** Calls whose PATH looked like an AI inference request while their host
   *  classified to nothing — the team's own model server, a private gateway
   *  or a regional endpoint, none of which this kit can measure until it is
   *  declared. A count only; absent from a kit that cannot tell. */
  unclassifiedCalls?: number;
  tokensIn: number;
  tokensOut: number;
  cachedIn: number;
  costMicros: number;
  reportedCostCalls: number;
  pricedCalls: number;
  unpricedCalls: number;
  worstRequestCostMicros: number;
  windowMs: number;
  usageMissingCalls: number;
  streamUsageMissingCalls: number;
  /** Calls that went to an endpoint the app declared as its own. */
  declaredCalls?: number;
  /** Of those, the ones we saw usage for and deliberately did not price. */
  declaredUnpricedCalls?: number;
  promptRepeatWorst: number;
  promptRepeatRequests: number;
  serialWorst: number;
  serialRequests: number;
  noTimeLimitCalls: number;
  retryNoBackoffCount: number;
  headroomReads: number;
  /** Browser-only: AI replies from another origin that exposed NO rate-limit
   *  header at all. The evidence behind an abstention — the page was not
   *  ALLOWED to see the ceiling, which is not the same as a provider that
   *  publishes none. */
  headersHiddenCalls: number;
  worstRequestsPct: number | null;
  worstTokensPct: number | null;
  worstRetryAfterMs: number;
}

export interface AiCallsResult {
  score: number | null;
  rating: AxisRating;
  /** 0 = watching, but nothing honest to score yet AND a blind spot is the
   *  reason the tile must not read as "this app makes no AI calls". */
  measurable: 0 | 1;
  waitPct: number;
  callCount: number;
  watchedRequests: number;
  providerCount: number;
  topProvider: number;
  p75Ms: number;
  worstMs: number;
  streamCount: number;
  ttftP75Ms: number;
  stallCount: number;
  failCount: number;
  rateLimitedCount: number;
  quotaCount: number;
  timeoutCount: number;
  truncatedCount: number;
  filteredCount: number;
  serverMsP75: number;
  serverMsCalls: number;
  unwatchedClients: number;
  /** AI-shaped calls to a host this kit could not classify — see
   *  AI_SHAPED_PATHS in aiCalls.ts. Never part of any count above. */
  unclassifiedCalls: number;
}

/**
 * Score the AI-wait axis, or return null when there is nothing honest to say.
 *
 * THREE OUTCOMES, the same three the Node kit uses:
 *   • null — this page made no AI calls, or too few turns to read, and there
 *     is no blind spot. The axis is absent from the snapshot, which every
 *     surface reads as "cannot tell". A page that calls no AI provider shows
 *     nothing here rather than a row of zeros, and a runtime without this
 *     reading looks exactly the same — neither can be mistaken for a measured
 *     zero.
 *   • measurable 0 — too few turns to score, but a call to a provider was seen
 *     going out on a transport this kit cannot wrap, so a real blind spot
 *     would otherwise pass for a page with no AI in it. The same answer
 *     covers AI-SHAPED traffic to a host the classifier could not name: the
 *     page IS doing AI work, this kit could not see whose, and saying
 *     nothing would be a lie rather than a silence.
 *   • measurable 1 — a real reading.
 */
export function computeAiCalls(stats: AiCallStatsLike): AiCallsResult | null {
  if (!stats) return null;
  const unwatchedClients = Math.max(0, Math.round(stats.unwatchedClients ?? 0));
  const unclassifiedCalls = Math.max(0, Math.round(stats.unclassifiedCalls ?? 0));
  const base = {
    callCount: Math.max(0, Math.round(stats.callCount ?? 0)),
    watchedRequests: Math.max(0, Math.round(stats.watchedRequests ?? 0)),
    providerCount: Math.max(0, Math.round(stats.providerCount ?? 0)),
    topProvider: Math.max(0, Math.round(stats.topProvider ?? 0)),
    p75Ms: Math.max(0, Math.round(stats.p75Ms ?? 0)),
    worstMs: Math.max(0, Math.round(stats.worstMs ?? 0)),
    streamCount: Math.max(0, Math.round(stats.streamCount ?? 0)),
    ttftP75Ms: Math.max(0, Math.round(stats.ttftP75Ms ?? 0)),
    stallCount: Math.max(0, Math.round(stats.stallCount ?? 0)),
    failCount: Math.max(0, Math.round(stats.failCount ?? 0)),
    rateLimitedCount: Math.max(0, Math.round(stats.rateLimitedCount ?? 0)),
    quotaCount: Math.max(0, Math.round(stats.quotaCount ?? 0)),
    timeoutCount: Math.max(0, Math.round(stats.timeoutCount ?? 0)),
    truncatedCount: Math.max(0, Math.round(stats.truncatedCount ?? 0)),
    filteredCount: Math.max(0, Math.round(stats.filteredCount ?? 0)),
    serverMsP75: Math.max(0, Math.round(stats.serverMsP75 ?? 0)),
    serverMsCalls: Math.max(0, Math.round(stats.serverMsCalls ?? 0)),
    unwatchedClients,
    unclassifiedCalls,
  };
  // No AI call has EVER been seen. With nothing else to report, that is a
  // page with no AI layer, and the honest answer is silence.
  //
  // Two things break that silence, and neither is a reading. An AI-SHAPED
  // call to a host we could not classify: the page IS doing AI work and this
  // kit could not see whose. And an AI-shaped call that arrived through a
  // transport this kit does not wrap: "no AI here" and "AI we cannot see"
  // must not arrive looking the same, which is the whole reason the count
  // travels.
  if (base.callCount === 0) {
    if (unclassifiedCalls === 0 && unwatchedClients === 0) return null;
    return { score: null, rating: "pending", measurable: 0, waitPct: 0, ...base };
  }
  if (base.watchedRequests < AI_WORK_MIN_TURNS) {
    if (unwatchedClients === 0 && unclassifiedCalls === 0) return null;
    return { score: null, rating: "pending", measurable: 0, waitPct: 0, ...base };
  }
  const waitPct = (() => {
    const raw =
      stats.watchedRequestMs > 0 ? (stats.aiMs / stats.watchedRequestMs) * 100 : 0;
    return Math.max(0, Math.min(100, Math.round(raw * 10) / 10));
  })();
  const failPct =
    base.callCount > 0 ? (base.failCount / base.callCount) * 100 : 0;
  const terms = [
    linearScore(failPct, AI_FAIL_PCT_THRESHOLDS.good, AI_FAIL_PCT_THRESHOLDS.poor),
  ];
  // Only judge the first-words wait where there were streamed answers to
  // judge. A page that never streams is not slow at streaming.
  if (base.streamCount > 0 && base.ttftP75Ms > 0) {
    terms.push(
      linearScore(
        base.ttftP75Ms,
        AI_TTFT_MS_THRESHOLDS.good,
        AI_TTFT_MS_THRESHOLDS.poor,
      ),
    );
  }
  const score = Math.min(...terms);
  return { score, rating: ratingFor(score), measurable: 1, waitPct, ...base };
}

/* ─── AI spend: tokens, cache and money ─────────────────────────────────
 *
 * The token counts a provider returns, what they cost, and how much of the
 * input it served out of its own prompt cache — which is the whole difference
 * between an expensive app and a cheap one.
 *
 * WHAT IS SCORED. Two things a developer can actually fix:
 *   • Usage the page never got told. On a streamed reply some providers only
 *     report usage when the caller asks for it, so a project can be flying
 *     blind on cost without knowing. That is a finding with a one-line fix.
 *   • The same prompt sent more than once inside one turn — the same tokens
 *     paid for twice.
 *
 * WHAT IS NOT SCORED, ON PURPOSE. Money. A big bill can be exactly right: an
 * app that does real work with a real model is not a broken app. Cost is
 * reported so the developer can see it, never graded — and it is only ever
 * reported where the provider's own reply admitted something. Where no usage
 * was reported and no price is known, the call is counted as UNPRICED rather
 * than guessed at.
 */

/** Share of AI calls whose usage was never reported. */
export const AI_USAGE_MISSING_PCT_THRESHOLDS = { good: 0, poor: 50 } as const;
/** Identical prompts inside ONE turn. 1 is the honest floor. */
export const AI_PROMPT_REPEAT_THRESHOLDS = { good: 1, poor: 4 } as const;

export interface AiSpendResult {
  score: number | null;
  rating: AxisRating;
  measurable: 0 | 1;
  calls: number;
  tokensIn: number;
  tokensOut: number;
  cachedIn: number;
  /** Share of input tokens the provider served from its own cache, or null
   *  when no reply ever reported an input-token count — which is "cannot
   *  tell", never a cold cache. */
  cacheHitPct: number | null;
  costMicros: number;
  reportedCostCalls: number;
  pricedCalls: number;
  unpricedCalls: number;
  costPerRequestMicros: number;
  worstRequestCostMicros: number;
  /** The session window the cost accrued over, so a surface can project a
   *  monthly rate and SAY it is a projection over this window. */
  windowMs: number;
  usageMissingCalls: number;
  usageMissingPct: number;
  streamUsageMissingCalls: number;
  /** Calls to declared endpoints, and those deliberately left unpriced. */
  declaredCalls: number;
  declaredUnpricedCalls: number;
  repeatWorst: number | null;
  repeatRequests: number;
  /** The price table's publication date, as whole days since the epoch, so no
   *  surface can show an estimate without being able to show its age. */
  priceTableDay: number;
}

/**
 * Score the AI-spend axis, or return null when this page buys no AI at all.
 *
 * `priceTableDay` is supplied by the caller rather than imported, so this
 * module stays free of the price table and the scorer stays pure.
 */
export function computeAiSpend(
  stats: AiCallStatsLike,
  priceTableDay: number,
): AiSpendResult | null {
  if (!stats) return null;
  const calls = Math.max(0, Math.round(stats.callCount ?? 0));
  if (calls === 0) return null;
  const tokensIn = Math.max(0, Math.round(stats.tokensIn ?? 0));
  const tokensOut = Math.max(0, Math.round(stats.tokensOut ?? 0));
  const cachedIn = Math.max(0, Math.round(stats.cachedIn ?? 0));
  const usageMissingCalls = Math.max(0, Math.round(stats.usageMissingCalls ?? 0));
  const watchedRequests = Math.max(0, Math.round(stats.watchedRequests ?? 0));
  const costMicros = Math.max(0, Math.round(stats.costMicros ?? 0));
  const usageMissingPct =
    Math.round(((usageMissingCalls / calls) * 100) * 10) / 10;
  const shape = {
    calls,
    tokensIn,
    tokensOut,
    cachedIn,
    // Never 0% for an app whose provider simply reported no input tokens —
    // that is "cannot tell", and a 0% cache hit rate is a very different
    // (and actionable) statement.
    cacheHitPct:
      tokensIn > 0
        ? Math.max(0, Math.min(100, Math.round((cachedIn / tokensIn) * 1000) / 10))
        : null,
    costMicros,
    reportedCostCalls: Math.max(0, Math.round(stats.reportedCostCalls ?? 0)),
    pricedCalls: Math.max(0, Math.round(stats.pricedCalls ?? 0)),
    unpricedCalls: Math.max(0, Math.round(stats.unpricedCalls ?? 0)),
    // The denominator is a TURN that called AI, never an AI call and never the
    // page's whole traffic: `watchedRequests` is only incremented for a turn
    // that made at least one call, so a turn that made three is charged once,
    // at what all three cost together. That is the number a developer budgets
    // with. Money per CALL is `costMicros / pricedCalls`, derived where it is
    // shown rather than carried twice on the wire.
    costPerRequestMicros:
      watchedRequests > 0 ? Math.round(costMicros / watchedRequests) : 0,
    worstRequestCostMicros: Math.max(
      0,
      Math.round(stats.worstRequestCostMicros ?? 0),
    ),
    windowMs: Math.max(0, Math.round(stats.windowMs ?? 0)),
    usageMissingCalls,
    usageMissingPct,
    streamUsageMissingCalls: Math.max(
      0,
      Math.round(stats.streamUsageMissingCalls ?? 0),
    ),
    declaredCalls: Math.max(0, Math.round(stats.declaredCalls ?? 0)),
    // A subset of `unpricedCalls`, and the surfaces subtract one from the
    // other to tell a gap from an abstention. Sanitised independently the two
    // can contradict each other and the subtraction goes negative, so the
    // invariant is enforced where the numbers leave.
    declaredUnpricedCalls: Math.min(
      Math.max(0, Math.round(stats.unpricedCalls ?? 0)),
      Math.max(0, Math.round(stats.declaredUnpricedCalls ?? 0)),
    ),
    repeatRequests: Math.max(0, Math.round(stats.promptRepeatRequests ?? 0)),
    priceTableDay: Math.round(priceTableDay),
  };
  if (watchedRequests < AI_WORK_MIN_TURNS) {
    return {
      score: null,
      rating: "pending",
      measurable: 0,
      repeatWorst: null,
      ...shape,
    };
  }
  // A watched turn always made at least one AI call, so the worst repeat is at
  // least 1 — "1" is the honest reading for a page that never sent the same
  // prompt twice.
  const repeatWorst = Math.max(1, Math.round(stats.promptRepeatWorst ?? 0));
  const score = Math.min(
    linearScore(
      usageMissingPct,
      AI_USAGE_MISSING_PCT_THRESHOLDS.good,
      AI_USAGE_MISSING_PCT_THRESHOLDS.poor,
    ),
    linearScore(
      repeatWorst,
      AI_PROMPT_REPEAT_THRESHOLDS.good,
      AI_PROMPT_REPEAT_THRESHOLDS.poor,
    ),
  );
  return {
    score,
    rating: ratingFor(score),
    measurable: 1,
    repeatWorst,
    ...shape,
  };
}

/* ─── AI rate-limit headroom ────────────────────────────────────────────
 *
 * How close this project is to its provider's published limits, in the SAME
 * shape as every other ceiling a kit reports: a remaining percentage,
 * worst-seen, with the refusals that already happened beside it.
 *
 * Free to collect where the browser is allowed to read it — and honest where
 * it is not. A cross-origin reply exposes only the headers the provider opted
 * to publish to the page, so this axis has a third state the server kits
 * rarely reach: replies were seen, a ceiling certainly exists, and the page
 * was not permitted to read it. That ships as measurable 0 with the hidden
 * count beside it, so the surface can say WHY rather than render a comfortable
 * zero — and a page whose provider published nothing at all still gets no axis
 * rather than a comforting 100%.
 */

/** Remaining share of the window. Good with 40% left, poor at 5%. */
export const AI_HEADROOM_PCT_THRESHOLDS = { good: 40, poor: 5 } as const;

export interface AiHeadroomResult {
  score: number | null;
  rating: AxisRating;
  measurable: 0 | 1;
  reads: number;
  worstRequestsPct: number;
  worstTokensPct: number;
  refusals: number;
  worstRetryAfterMs: number;
  /** Browser-only: replies whose ceiling the browser would not let the page
   *  read. The REASON an abstention is an abstention. */
  headersHidden: number;
}

/**
 * Score the AI rate-limit headroom axis, or return null when there was nothing
 * to read and no evidence that anything was hidden.
 *
 * A refusal that arrived with no headroom headers still ships the axis, with
 * measurable 0 — being refused is itself the strongest possible evidence that
 * a ceiling exists, and staying silent about it would be the one dishonest
 * outcome here. So does a reply whose headers the browser hid: the ceiling is
 * there, this page simply may not see it.
 */
export function computeAiHeadroom(
  stats: AiCallStatsLike,
): AiHeadroomResult | null {
  if (!stats) return null;
  const reads = Math.max(0, Math.round(stats.headroomReads ?? 0));
  const refusals = Math.max(0, Math.round(stats.rateLimitedCount ?? 0));
  const headersHidden = Math.max(0, Math.round(stats.headersHiddenCalls ?? 0));
  if (reads === 0 && refusals === 0 && headersHidden === 0) return null;
  const worstRetryAfterMs = Math.max(0, Math.round(stats.worstRetryAfterMs ?? 0));
  const pct = (v: number | null): number | null =>
    typeof v === "number" && Number.isFinite(v)
      ? Math.max(0, Math.min(100, Math.round(v * 10) / 10))
      : null;
  const req = pct(stats.worstRequestsPct ?? null);
  const tok = pct(stats.worstTokensPct ?? null);
  if (req === null && tok === null) {
    return {
      score: null,
      rating: "pending",
      measurable: 0,
      reads,
      // Not "full" — unknown. The tile reads measurable 0 first and says so,
      // and `headersHidden` tells it WHICH kind of unknown this is.
      worstRequestsPct: 0,
      worstTokensPct: 0,
      refusals,
      worstRetryAfterMs,
      headersHidden,
    };
  }
  // The tighter of the two ceilings decides: a project with plenty of request
  // headroom and no tokens left is out of headroom.
  const worst = Math.min(req ?? 100, tok ?? 100);
  const score = linearScore(
    // linearScore rises as the value falls, and headroom is the other way
    // round, so it is scored on how much of the window is USED.
    100 - worst,
    100 - AI_HEADROOM_PCT_THRESHOLDS.good,
    100 - AI_HEADROOM_PCT_THRESHOLDS.poor,
  );
  return {
    score,
    rating: ratingFor(score),
    measurable: 1,
    reads,
    worstRequestsPct: req ?? 100,
    worstTokensPct: tok ?? 100,
    refusals,
    worstRetryAfterMs,
    headersHidden,
  };
}
