/** Web-vitals score thresholds — the web runtime's rating model.
 *
 * Same `Rating` union and composite bands as the RN / Node / Python runtimes
 * (good ≥85 · needs-work ≥60), but the metrics are the ones that matter in a
 * browser: TTFB, LCP, INP, and CLS. The per-metric bands mirror Google's
 * published web-vitals thresholds so a Boosthis rating always matches what a
 * developer already sees in Lighthouse / CrUX — no second opinion to argue
 * with.
 */

import { MIN_RATE_WINDOW_MIN } from "./rateHonesty";

export type Rating = "good" | "needs-work" | "poor";

/** Version of the web runtime. Surfaced in snapshots + MCP server info. */
export const RUNTIME_VERSION = "1.0.0-alpha.130";

/** Duration-based metrics (all milliseconds). `weight` mirrors the composite
 *  weighting model of the other runtimes (loading .25 / main content .45 /
 *  input responsiveness .30). */
export const SCORE_THRESHOLDS = {
  ttfb: { good: 800, poor: 1800, weight: 0.25 },
  lcp: { good: 2500, poor: 4000, weight: 0.45 },
  inp: { good: 200, poor: 500, weight: 0.3 },
} as const;

export type DurationMetric = keyof typeof SCORE_THRESHOLDS;

/** CLS is unitless and additive (a sum of layout-shift scores, not a
 *  duration), so it gets its own bands instead of a SCORE_THRESHOLDS row. */
export const CLS_THRESHOLDS = { good: 0.1, poor: 0.25 } as const;

/** Latency-tail stability band: the p99/p50 "tail blowup" ratio. Mirrors the
 *  Node/Python resilience axis (good ≤3× · poor ≥8×). Additive + display-only —
 *  it NEVER feeds the composite Speed score. */
export const TAIL_RATIO_THRESHOLDS = { good: 3, poor: 8 } as const;

/** Long-task rate band: main-thread blocks (≥50ms) per minute. Additive +
 *  display-only — it NEVER feeds the composite Speed score. */
export const LONG_TASK_THRESHOLDS = { good: 2, poor: 10 } as const;

/** Rate a raw value against a good/poor band pair (lower is better). */
export function rateValue(value: number, good: number, poor: number): Rating {
  if (!Number.isFinite(value) || value < 0) return "poor";
  if (value <= good) return "good";
  if (value >= poor) return "poor";
  return "needs-work";
}

/** Rate a duration against one of the web-vitals metrics (default LCP —
 *  the closest browser analogue of the other runtimes' TTI). */
export function rateDuration(
  ms: number,
  metric: DurationMetric = "lcp",
): Rating {
  const t = SCORE_THRESHOLDS[metric];
  return rateValue(ms, t.good, t.poor);
}

/** Rate a cumulative layout shift score. */
export function rateCls(cls: number): Rating {
  return rateValue(cls, CLS_THRESHOLDS.good, CLS_THRESHOLDS.poor);
}

/**
 * An axis band whose two constants are the wrong way round.
 *
 * A band is a pair of CONSTANTS in our own source: for a lower-is-better axis
 * `good` is the value at or below which a reading scores 100 and `poor` the
 * value at or above which it scores 0, so `good` below `poor` is what makes
 * the interpolation mean anything. (`linearScoreHigh` inverts the pair on
 * purpose and is checked the other way round.) A pair the wrong way round is
 * a typo in the kit — never something a customer's page did.
 *
 * The Node helper used to answer 100 for one, so a meter wired backwards
 * reported perfect health on every install for ever and nothing complained.
 * This one silently returned 0 instead, which is a third answer to the same
 * mistake. Every runtime refuses now, with the same words.
 *
 * The throw is the BACKSTOP, not the guard. scripts' axis-band-inversion gate
 * reads the declared constants out of every shipped kit and fails the release
 * before an inverted pair can reach anybody.
 */
export class InvertedAxisBandError extends Error {
  readonly good: number;
  readonly poor: number;
  constructor(good: number, poor: number) {
    super(`${INVERTED_BAND_MESSAGE} (good=${good}, poor=${poor})`);
    this.name = "InvertedAxisBandError";
    this.good = good;
    this.poor = poor;
  }
}

/** The leading words every runtime uses when it refuses an inverted band, so
 *  the same mistake is recognisable whichever kit reported it. */
export const INVERTED_BAND_MESSAGE =
  "boosthis: inverted axis band, good must be below poor";

/** Map a raw value onto a 0–100 score: 100 at/below `good`, 0 at/above
 *  `poor`, linear in between. Same curve as the RN/Node health axes. A
 *  misconfigured band (poor <= good, or either end not a finite number)
 *  REFUSES — see {@link InvertedAxisBandError}. */
export function linearScore(value: number, good: number, poor: number): number {
  if (!Number.isFinite(good) || !Number.isFinite(poor) || poor <= good) {
    throw new InvertedAxisBandError(good, poor);
  }
  if (!Number.isFinite(value) || value <= good) return 100;
  if (value >= poor) return 0;
  return Math.round(100 * (1 - (value - good) / (poor - good)));
}

/** Composite rating bands shared with every runtime. */
export function ratingFor(score: number): Rating {
  if (score >= 85) return "good";
  if (score >= 60) return "needs-work";
  return "poor";
}

/** Map a raw value where HIGHER is better onto 0–100: 100 at/above `good`, 0
 *  at/below `poor`, linear between (`good` > `poor`). The mirror of
 *  `linearScore` for "more is better" axes (e.g. resource efficiency). The
 *  band is declared the other way up here, so the inversion this REFUSES is
 *  `good <= poor` — same mistake, mirrored. */
export function linearScoreHigh(value: number, good: number, poor: number): number {
  if (!Number.isFinite(good) || !Number.isFinite(poor) || good <= poor) {
    throw new InvertedAxisBandError(good, poor);
  }
  if (!Number.isFinite(value) || value >= good) return 100;
  if (value <= poor) return 0;
  return Math.round(100 * ((value - poor) / (good - poor)));
}

/** Memory-pressure band: JS heap in use as a % of the tab's hard limit (how
 *  close the page sits to the point where the browser OOM-kills the tab). good
 *  ≤50% · poor ≥90%. Additive + display-only — it NEVER feeds the Speed score. */
export const MEMORY_PCT_THRESHOLDS = { good: 50, poor: 90 } as const;

/** Resource-efficiency band: % of sizable subresources served cached or
 *  compressed (HIGHER is better). good ≥90% · poor ≤50%. Additive +
 *  display-only — it NEVER feeds the Speed score. */
export const RESOURCE_EFF_THRESHOLDS = { good: 90, poor: 50 } as const;

/** Paint-readiness band: first-contentful-paint (ms). Mirrors Google's FCP
 *  web-vitals thresholds (good ≤1800ms · poor ≥3000ms). Additive +
 *  display-only — it NEVER feeds the Speed score. */
export const FCP_THRESHOLDS = { good: 1800, poor: 3000 } as const;

/** Animation-smoothness band: long-animation-frames (LoAF) per minute. good
 *  ≤2/min · poor ≥30/min. Additive + display-only — it NEVER feeds the Speed
 *  score. */
export const LOAF_THRESHOLDS = { good: 2, poor: 30 } as const;

/** Crash-free band: crashes per hour, from the crash-hook counter. good 0/hr ·
 *  poor ≥3/hr. Additive + display-only — it NEVER feeds the Speed score. */
export const CRASHFREE_RATE_THRESHOLDS = { good: 0, poor: 3 } as const;

/** Observe at least this many minutes before claiming crash-free (a crash
 *  surfaces immediately regardless of the window). */
export const CRASHFREE_MIN_WINDOW_MIN = MIN_RATE_WINDOW_MIN;

// ─── Additive web-meter batch bands (9 new axes) ──────────────────────────
// Every band below feeds an ADDITIVE, display-only axis — none is ever mixed
// into the composite Speed score. All lower-is-better (scored via linearScore)
// except where noted.

/** Slow-frame-cause band: LoAF blocking ms per minute. good ≤100 · poor ≥1000.
 *  Additive + display-only — it NEVER feeds the Speed score. */
export const FRAME_CAUSE_THRESHOLDS = { good: 100, poor: 1000 } as const;

/** Third-party-cost band: main-thread ms/min burned by scripts from other
 *  origins (LoAF script attribution). good ≤50 · poor ≥500. Additive +
 *  display-only — it NEVER feeds the Speed score. */
export const THIRD_PARTY_THRESHOLDS = { good: 50, poor: 500 } as const;

/** Soft-navigation band: p75 responsiveness (ms) between a soft-nav start and
 *  the next LCP. good ≤800 · poor ≥2400. Additive + display-only — it NEVER
 *  feeds the Speed score. */
export const SOFT_NAV_THRESHOLDS = { good: 800, poor: 2400 } as const;

/** Blocking-time band: main-thread long-task overage (ms/min, the TBT field
 *  cousin). good ≤200 · poor ≥1000. Additive + display-only — it NEVER feeds
 *  the Speed score. */
export const BLOCKING_TIME_THRESHOLDS = { good: 200, poor: 1000 } as const;

/** Memory-trend band: heap growth in MB/min (clamped at a 0 floor for scoring).
 *  good ≤2 · poor ≥20. Additive + display-only — it NEVER feeds the Speed
 *  score. */
export const MEMORY_TREND_THRESHOLDS = { good: 2, poor: 20 } as const;

/** Resource-weight band: total transferred KB across sized subresources.
 *  good ≤2048 (2MB) · poor ≥10240 (10MB). Additive + display-only — it NEVER
 *  feeds the Speed score. */
export const RESOURCE_BLOAT_THRESHOLDS = { good: 2048, poor: 10240 } as const;

/** Rage-click band: detected click bursts per minute. good ≤0.2 · poor ≥3.
 *  Additive + display-only — it NEVER feeds the Speed score. */
export const RAGE_CLICK_THRESHOLDS = { good: 0.2, poor: 3 } as const;

/** Input-readiness band: discrete inputs that landed BEFORE first-contentful-
 *  paint (dropped early taps). good ≤0 · poor ≥3. Additive + display-only —
 *  it NEVER feeds the Speed score. */
export const EARLY_INPUT_THRESHOLDS = { good: 0, poor: 3 } as const;

// ─── 2026-08 web platform batch bands ─────────────────────────────────────
// Every band below feeds an ADDITIVE, display-only axis — none is ever mixed
// into the composite Speed score. Lower-is-better (scored via linearScore)
// except where noted "HIGHER is better" (linearScoreHigh).

/** Navigation-readiness band: domInteractive (ms from navigation start). Mirrors
 *  the loading feel — good ≤2000 · poor ≥5000. */
export const NAV_READINESS_THRESHOLDS = { good: 2000, poor: 5000 } as const;

/** Redirect-overhead band: redirect time (ms) on the main navigation. A clean
 *  navigation has 0. good ≤50 · poor ≥800. */
export const REDIRECT_THRESHOLDS = { good: 50, poor: 800 } as const;

/** Server-processing band: worst Server-Timing `duration` reported (ms). good
 *  ≤200 · poor ≥1000. */
export const SERVER_TIMING_THRESHOLDS = { good: 200, poor: 1000 } as const;

/** Handshake-cost band: DNS+TCP+TLS negotiation on the main navigation (ms).
 *  Warm connections reuse, so 0 is common. good ≤150 · poor ≥1000. */
export const HANDSHAKE_THRESHOLDS = { good: 150, poor: 1000 } as const;

/** Protocol-mix band: % of timed resources on a modern protocol (HTTP/2 or
 *  HTTP/3). HIGHER is better. good ≥90% · poor ≤50%. */
export const PROTOCOL_MIX_THRESHOLDS = { good: 90, poor: 50 } as const;

/** Render-blocking band: count of observed styles/scripts marked
 *  render-blocking. good ≤2 · poor ≥10. */
export const RENDER_BLOCKING_THRESHOLDS = { good: 2, poor: 10 } as const;

/** Transfer-waste band: % of transferred bytes beyond the decoded size across
 *  sized resources (compression inefficiency / over-transfer). good ≤5% ·
 *  poor ≥40%. */
export const TRANSFER_WASTE_THRESHOLDS = { good: 5, poor: 40 } as const;

/** Cache-revalidation band: % of cache-eligible resources that still needed a
 *  network revalidation (transferSize 0 = fresh hit). good ≤10% · poor ≥60%. */
export const CACHE_REVALIDATION_THRESHOLDS = { good: 10, poor: 60 } as const;

/** Cache-reuse band: % of the resources a page loaded that the browser served
 *  from its own cache instead of fetching again. Higher is better. Deliberately
 *  generous at the bottom — a genuine first visit has nothing to reuse, so the
 *  band only calls it poor when almost nothing is being reused across a
 *  session. good ≥60% · poor ≤15%. */
export const CACHE_REUSE_THRESHOLDS = { good: 60, poor: 15 } as const;
/** Storage-headroom band: % of the origin storage quota already used. good
 *  ≤50% · poor ≥90%. */
export const STORAGE_HEADROOM_THRESHOLDS = { good: 50, poor: 90 } as const;

/** Connection-quality band: browser-reported round-trip time (ms). good ≤150 ·
 *  poor ≥600. */
export const CONNECTION_QUALITY_THRESHOLDS = { good: 150, poor: 600 } as const;

/** Report-pressure band: browser deprecation/intervention reports per hour.
 *  good ≤1 · poor ≥20. */
export const REPORT_PRESSURE_THRESHOLDS = { good: 1, poor: 20 } as const;
/** Observe at least this many minutes before rating report pressure (a report
 *  surfaces immediately regardless of the window). */
export const REPORT_PRESSURE_MIN_WINDOW_MIN = 5;

/** Unhandled-failure band: uncaught errors + unhandled rejections per hour.
 *  good ≤1 · poor ≥30. */
export const UNHANDLED_THRESHOLDS = { good: 1, poor: 30 } as const;
/** No clean bill until at least 5 minutes have been watched. */
export const UNHANDLED_MIN_WINDOW_MIN = 5;

/** Layout-shift-source band: worst single layout-shift's contributing source
 *  count (how many nodes moved together). good ≤2 · poor ≥10. */
export const SHIFT_SOURCE_THRESHOLDS = { good: 2, poor: 10 } as const;

/** Media-playback band: % of decoded video frames the browser dropped. good
 *  ≤1% · poor ≥10%. */
export const MEDIA_PLAYBACK_THRESHOLDS = { good: 1, poor: 10 } as const;

/** Font-readiness band: time (ms) from navigation start to document.fonts
 *  ready. good ≤1000 · poor ≥3000. */
export const FONT_READINESS_THRESHOLDS = { good: 1000, poor: 3000 } as const;

/** Asset-footprint band: script + stylesheet element count in the document.
 *  good ≤30 · poor ≥120. */
export const ASSET_FOOTPRINT_THRESHOLDS = { good: 30, poor: 120 } as const;

/** DOM-footprint band (opt-in): total element count in the document. good
 *  ≤1500 · poor ≥8000. */
export const DOM_FOOTPRINT_THRESHOLDS = { good: 1500, poor: 8000 } as const;

/** Idle-opportunity band (opt-in): % of requestIdleCallback opportunities that
 *  timed out (deadline missed). good ≤5% · poor ≥40%. */
export const IDLE_OPPORTUNITY_THRESHOLDS = { good: 5, poor: 40 } as const;

// ─── Patch Lag (build-identity / exposure window) ─────────────────────────
// How stale the currently-deployed build is. ADDITIVE + display-only — it
// NEVER feeds the composite Speed score. Emitted ONLY when a build time is
// honestly known (see buildIdentity.ts); absent otherwise (never a fake axis).

/** One day in milliseconds — used to turn a build age (ms) into age-in-days. */
export const PATCH_LAG_DAY_MS = 86_400_000;

/** Patch-lag rating bands, expressed in build age (days):
 *  good < 7 days · needs-work < 30 days · poor otherwise. */
export const PATCH_LAG_RATING_DAYS = { good: 7, needsWork: 30 } as const;

/** The age (days) at which the patch-lag score reaches 0. The score curve is
 *  `100 - (ageDays/60)*100` clamped to [0, 100], so 60 days maps to 0. */
export const PATCH_LAG_SCORE_FULL_DAYS = 60;

/** Rate a build age (in ms) against the patch-lag bands. */
export function ratePatchLag(buildAgeMs: number): Rating {
  const ageDays = buildAgeMs / PATCH_LAG_DAY_MS;
  if (ageDays < PATCH_LAG_RATING_DAYS.good) return "good";
  if (ageDays < PATCH_LAG_RATING_DAYS.needsWork) return "needs-work";
  return "poor";
}

/** Map a build age (in ms) onto a 0–100 patch-lag score: fresh builds score
 *  100, a 60-day-old build scores 0, linear in between (clamped). */
export function patchLagScore(buildAgeMs: number): number {
  const ageDays = Math.max(0, buildAgeMs) / PATCH_LAG_DAY_MS;
  const raw = 100 - (ageDays / PATCH_LAG_SCORE_FULL_DAYS) * 100;
  return Math.round(Math.min(100, Math.max(0, raw)));
}

/** Edge-cache band: of the replies the hosting platform treated as cacheable,
 *  the % it actually served from its own cache. Higher is better. Replies the
 *  platform deliberately bypasses are excluded from both sides of the share.
 *  good ≥70% · poor ≤20%. */
export const EDGE_CACHE_THRESHOLDS = { good: 70, poor: 20 } as const;

/* ── Resilience tail contract — shared by EVERY kit ──────────────────────────
 *
 * p99/p50 alone produces confident nonsense at low latency: it rated a service
 * with a 22 ms worst case POOR because its median was 3 ms, and it handed a
 * FREE PERFECT SCORE to any route whose median rounded to 0 ms — which, since
 * durations are captured in whole milliseconds, is every cached read, health
 * check and static handler. Three numbers fix it, and they are identical in
 * every kit that computes this ratio (bands and blend weights stay per-kit).
 * `scripts/src/__tests__/resilience-tail-parity.test.ts` names every copy.
 */

/** A p99 at or below this is not a tail worth acting on, whatever the ratio. */
export const RESILIENCE_TAIL_FLOOR_MS = 50;

/** Below this many samples a "p99" is just the slowest of a handful. */
export const RESILIENCE_MIN_TAIL_SAMPLES = 20;

/** A median below the capture resolution is unmeasured, not small. */
export const RESILIENCE_MIN_MEDIAN_MS = 1;

/** What the tail can honestly say about a set of samples. */
export type TailReading =
  | { state: "insufficient" }
  | { state: "flat"; ratio: number | null }
  | { state: "unmeasurable" }
  | { state: "rated"; ratio: number };

/** The one place this judgement is made in the web kit. */
export function readResilienceTail(
  p50Ms: number | null | undefined,
  p99Ms: number | null | undefined,
  sampleCount: number,
): TailReading {
  if (
    sampleCount < RESILIENCE_MIN_TAIL_SAMPLES ||
    p50Ms == null ||
    p99Ms == null ||
    !Number.isFinite(p50Ms) ||
    !Number.isFinite(p99Ms)
  ) {
    return { state: "insufficient" };
  }
  const measurableMedian = p50Ms >= RESILIENCE_MIN_MEDIAN_MS;
  if (p99Ms <= RESILIENCE_TAIL_FLOOR_MS) {
    return { state: "flat", ratio: measurableMedian ? p99Ms / p50Ms : null };
  }
  if (!measurableMedian) return { state: "unmeasurable" };
  return { state: "rated", ratio: p99Ms / p50Ms };
}
