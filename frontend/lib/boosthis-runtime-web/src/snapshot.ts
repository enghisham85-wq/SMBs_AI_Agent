/** Privacy-safe perf-snapshot mirror for the web runtime.
 *
 * Web parity for the Node runtime's `snapshot.ts`. It captures the current
 * in-process meter picture as ONE small, allowlisted JSON object and ships it
 * to `POST /api/snapshots` (the same ingest RN and Node use) so the
 * developer's OWN AI can read the live picture back over the hosted MCP
 * live-read tools + `GET /api/snapshot`. No new server surface is added — the
 * payload maps into the existing allowlisted snapshot fields (`capturedAt`,
 * `runtimeVersion`, `platform`, `startedAt`, `totalEvents`, `rows`, `axes`)
 * so it passes the server's `sanitizeSnapshotLabels` + PII guard unchanged.
 *
 * PRIVACY: the snapshot carries only normalized page-path labels + numeric
 * timings/counts/rating buckets — never user values, source, or PII. Route
 * keys are still filtered through the PII guard (in telemetry.ts
 * `transmitSnapshot`) before upload, and the whole payload clears
 * `assertNoPII`. Uploading is OFF by default in issues-only mode and enabled
 * only by the developer's explicit `shareMeterWithAI` opt-in or the server
 * directive once they connect an AI from the web dashboard — matching the
 * RN / Node privacy-by-default contract.
 */

import { readPressToScreen } from "./pressToScreen";
import { recent, summary, type Summary } from "./samples";
import { pageActivityFacts, type PageActivityFacts } from "./pageActivity";
import { readLiveConnections } from "./liveConnections";
import {
  SCORE_THRESHOLDS,
  TAIL_RATIO_THRESHOLDS,
  readResilienceTail,
  CLS_THRESHOLDS,
  MEMORY_PCT_THRESHOLDS,
  RESOURCE_EFF_THRESHOLDS,
  FCP_THRESHOLDS,
  LOAF_THRESHOLDS,
  CRASHFREE_RATE_THRESHOLDS,
  CRASHFREE_MIN_WINDOW_MIN,
  FRAME_CAUSE_THRESHOLDS,
  THIRD_PARTY_THRESHOLDS,
  SOFT_NAV_THRESHOLDS,
  BLOCKING_TIME_THRESHOLDS,
  MEMORY_TREND_THRESHOLDS,
  RESOURCE_BLOAT_THRESHOLDS,
  RAGE_CLICK_THRESHOLDS,
  EARLY_INPUT_THRESHOLDS,
  NAV_READINESS_THRESHOLDS,
  REDIRECT_THRESHOLDS,
  SERVER_TIMING_THRESHOLDS,
  HANDSHAKE_THRESHOLDS,
  PROTOCOL_MIX_THRESHOLDS,
  RENDER_BLOCKING_THRESHOLDS,
  TRANSFER_WASTE_THRESHOLDS,
  CACHE_REVALIDATION_THRESHOLDS,
  CACHE_REUSE_THRESHOLDS,
  EDGE_CACHE_THRESHOLDS,
  STORAGE_HEADROOM_THRESHOLDS,
  CONNECTION_QUALITY_THRESHOLDS,
  REPORT_PRESSURE_THRESHOLDS,
  REPORT_PRESSURE_MIN_WINDOW_MIN,
  UNHANDLED_THRESHOLDS,
  UNHANDLED_MIN_WINDOW_MIN,
  SHIFT_SOURCE_THRESHOLDS,
  MEDIA_PLAYBACK_THRESHOLDS,
  FONT_READINESS_THRESHOLDS,
  ASSET_FOOTPRINT_THRESHOLDS,
  DOM_FOOTPRINT_THRESHOLDS,
  IDLE_OPPORTUNITY_THRESHOLDS,
  RUNTIME_VERSION,
  linearScore,
  linearScoreHigh,
  ratingFor,
  ratePatchLag,
  patchLagScore,
  PATCH_LAG_DAY_MS,
  type Rating,
} from "./thresholds";
import {
  resolveBuildIdentity,
  REASON_NOT_WIRED_BY_HOST,
  REASON_NOTHING_TO_COMPARE,
  type BuildIdentity,
} from "./buildIdentity";
import {
  getVitals,
  normalizeRouteLabel,
  currentRouteLabel,
  getLongTaskStats,
  getHeapStats,
  getResourceEfficiency,
  getFcpMs,
  getLoafStats,
  getInpStats,
  getDeadClickStats,
  getRouteReachabilityStats,
  getRegisteredWebRoutes,
  getCircuitStats,
  getLoafCauseStats,
  getBlockingTimeStats,
  getBfCacheStats,
  getInputReadinessStats,
  getSoftNavStats,
  getMemoryTrendStats,
  getResourceBloat,
  getRageClickStats,
  sampleMemoryTrendNow,
} from "./vitals";
import { crashCount } from "./crashReporter";
import {
  routeListForSnapshot,
  type RouteListReport,
} from "./routeInventory";
import {
  controlCensusForSnapshot,
  type ControlCensusReport,
} from "./controlCensus";
import {
  computeBaselineScore,
  computeBudgetCompliance,
  computeScrollHealth,
  computeInteractionToNextPaint,
  computeFrameFloor,
  computeStabilityScore,
  computeBlockingAsync,
  confidenceLevel,
  confidenceRatingFor,
  confidenceCaptionFor,
  computeDependencyDistance,
  computeAiCalls,
  computeAiSpend,
  computeAiHeadroom,
  type RouteSeriesLike,
  type RouteBudgetVerdict,
} from "./meterAxes";
import { getAiCallStats } from "./aiCalls";
import { AI_PRICE_TABLE_DAY } from "./aiUsage";
import {
  getAbortedCount,
  getDependencyDistanceStats,
  getGroupOverflowCount,
  getInFlightCount,
  isCallWatchActive,
  readCallGroups,
  readNetwork,
  type CallGroupRow,
} from "./networkSampler";
import { getEdgeCacheStats } from "./edgeCache";
import { readIdle } from "./idleTracker";
import { getScrollStats, getFrameFloorStats } from "./scrollSampler";
import {
  readTimerHealth,
  sampleTimerHealth,
  readAsyncSlowCallbacks,
  readEventLoopLag,
} from "./timerHealth";
import {
  readWebStorageLatency,
  readWebStorageFailures,
} from "./localStore";
import { readSwallowedErrors } from "./swallowedErrors";
import { readLeakWatch } from "./leakWatch";
import { getRouteOutcomeTotals } from "./routeOutcomes";
import {
  getNavReadinessStats,
  getRedirectStats,
  getServerTimingStats,
  getHandshakeStats,
  getProtocolMixStats,
  getRenderBlockingStats,
  getTransferWasteStats,
  getCacheRevalidationStats,
  getCacheReuseStats,
  getInitiatorBalanceStats,
  getStorageHeadroomStats,
  sampleStorageHeadroomNow,
  sampleSwControlNow,
  getSwControlStats,
  getConnectionQualityStats,
  getDeviceCapacityStats,
  getPrerenderStats,
  getFontReadinessStats,
  getAssetFootprintStats,
  getCrossOriginIsolationStats,
  getDomFootprintStats,
  getIdleOpportunityStats,
  getShiftSourceStats,
  getInteractionVolumeStats,
  getMediaPlaybackStats,
} from "./webPlatform";
import { readUnhandled, readReportPressure } from "./webReports";
import { readBackgroundWork } from "./backgroundWork";
import { readDevPosture } from "./devPosture";

import { earnedPerHour, windowMinOf } from "./rateHonesty";
/** How often the snapshot mirror re-uploads while sharing is enabled, once
 *  the turbo first-run ramp (see {@link nextSnapshotDelayMs}) has settled. */
export const SNAPSHOT_FLUSH_MS = 60_000;

/** Turbo first-run upload cadence. A freshly installed kit fills its dashboard
 *  tiles in seconds instead of minutes by uploading the perf snapshot more
 *  often for the first ~3 minutes of a session, then reverting to the steady
 *  {@link SNAPSHOT_FLUSH_MS} interval. This ONLY changes how soon data the kit
 *  already measured reaches the server — every honesty gate (min sample count,
 *  warm-up windows, empty-snapshot skip) is untouched; count-based gates simply
 *  clear sooner because the same data arrives sooner.
 *
 *  Stateless + age-driven on purpose: a skipped or empty upload must not burn a
 *  ramp step, so the delay is derived purely from how long the session has been
 *  alive, never from an upload counter. The ramp is at most 4 uploads in the
 *  first 70 seconds (10s → 20s → 40s → 60s), well under the server's 60
 *  requests/minute-per-install ingest limit. */
export function nextSnapshotDelayMs(sessionAgeMs: number): number {
  if (sessionAgeMs < 30_000) return 10_000;
  if (sessionAgeMs < 90_000) return 20_000;
  if (sessionAgeMs < 180_000) return 40_000;
  return SNAPSHOT_FLUSH_MS;
}

/** Route rows are prefixed with "screen:" so the server's live-data MCP tools
 *  — which filter rows by that prefix (matching the RN convention) — light up
 *  for web installs exactly as they do for RN and Node. */
const SCREEN_PREFIX = "screen:";

/** Cap route rows so a busy app never ships an unbounded payload. */
const MAX_SNAPSHOT_ROWS = 60;

/** Newest-first sample scan window used to derive per-route p50/p95/max/last. */
const SAMPLE_WINDOW = 1000;

/** Axes need a minimum sample count before they claim to be honest. */
export const MIN_SAMPLES_FOR_AXES = 5;

/** Minimum wall time the LoAF observer must run before the animationSmoothness
 *  axis reports (a per-minute rate over a few seconds is noise). Matches the
 *  spec warm-up gate of ≥30s observed. */
export const LOAF_MIN_ELAPSED_MS = 30_000;

/** Minimum wall time the longtask observer and the rage-click window must run
 *  before the blockingTime / frameCause / thirdPartyCost / rageClicks axes
 *  report (a per-minute rate over a few seconds is noise). Matches the spec's
 *  ≥30s observed gate. */
export const BLOCKING_MIN_ELAPSED_MS = 30_000;
export const RAGE_MIN_ELAPSED_MS = 30_000;

/** Memory-trend axis needs ≥3 readings spanning ≥2 minutes to claim a slope. */
export const MEMORY_TREND_MIN_SAMPLES = 3;
export const MEMORY_TREND_MIN_SPAN_MS = 120_000;

/** p75 of a numeric list (ascending), used by the softNav axis. */
function p75Of(values: number[]): number {
  if (values.length === 0) return 0;
  const asc = values.slice().sort((a, b) => a - b);
  const idx = Math.min(asc.length - 1, Math.floor(asc.length * 0.75));
  return asc[idx];
}

export interface WebSnapshotRow {
  key: string;
  count: number;
  p50: number;
  p95: number;
  max: number;
  last: number;
}

export interface WebAxisResult {
  /** Null while an axis has evidence worth reporting but not yet enough of it
   *  to score honestly (see the outbound-call axis: a single hung call is reportable,
   *  a score drawn from it would not be). Never null once `rating` is a real
   *  band. */
  score: number | null;
  /** A band, or one of the three silences. Only "pending" means a score is
   *  still coming: "not-available" says this page's host cannot take the
   *  reading at all, and "not-scored" says the reading is real and
   *  deliberately never graded. Widened here so a reading that abstains for
   *  good can say which of the three it is, on the wire and on the panel,
   *  rather than every silence arriving as a wait. */
  rating: Rating | "pending" | "not-available" | "not-scored";
  /** Per-group outbound-call rows. ONLY the outbound-call axis emits this — it is
   *  named explicitly (rather than widening the index signature for every
   *  axis) so no other axis can start shipping arrays by accident. */
  groups?: CallGroupRow[];
  [k: string]: number | string | CallGroupRow[] | null | undefined;
}

/** A value in the axes container. Most axes are objects (`WebAxisResult`), but
 *  the confidence axis is emitted as FOUR SCALAR sibling keys per the shared
 *  axis contract (`confidence` string level + `confidenceMounts` /
 *  `confidenceRating` / `confidenceCaption`), so a value can also be a bare
 *  string/number. The map is typed `WebAxisResult` for ergonomic object-axis
 *  access; the scalar confidence keys are written through a narrow cast. */
export type WebAxisValue = WebAxisResult | number | string;

export interface WebSnapshotPayload {
  capturedAt: number;
  runtimeVersion: string;
  platform: "web";
  startedAt: number;
  totalEvents: number;
  rows: WebSnapshotRow[];
  axes: Record<string, WebAxisResult>;
  /** Build identity for the Patch Lag meter — WHAT build is running + WHEN it
   *  was built. Present ONLY when at least one component is honestly known
   *  (never fabricated); the whole object is omitted when nothing is known. */
  build?: WebSnapshotBuild;
  /** Live event counters — the same numbers the bubble's "live events" rows
   *  show (main-thread blocks, dead clicks, unreachable routes, loop/burst
   *  detections), mirrored to the web dashboard. CLOSED all-numeric shape:
   *  counts only, no labels/strings, so nothing new can ride the wire. */
  events: WebSnapshotEvents;
  /** The site's WHOLE route list, as the router describes it — so the map can
   *  draw routes nobody has opened, marked "not seen". Present only when a
   *  router was handed over (`registerRouter`) or routes were declared
   *  (`registerWebRoutes`), or when the whole thing is switched off (which
   *  says so, so "off" can be told from "an old kit"). Absent otherwise:
   *  that absence is "nothing yet", not a claim the site has no other pages. */
  routeList?: RouteListReport;
  /** The CONTROLS the rendered page has, as the page itself describes them —
   *  the same trick as `routeList`, one level down. Counts and structural
   *  handles only: no label, no value, no text, nothing a person typed (see
   *  `controlCensus.ts`). Present whenever this kit knows the question,
   *  including when it says `off`; absent only when the block could not be
   *  produced at all, which is how a kit too old to have it is told apart. */
  controlCensus?: ControlCensusReport;
  /** WHAT THIS VISIT CONSISTED OF, so a near-silent install can be explained
   *  instead of guessed at. A reading is taken when a page view ends, so a
   *  single-page app produces one reading however much a person does in it —
   *  and on our side that is indistinguishable from an app nobody opened.
   *  This block is the difference: address changes, presses, readings taken,
   *  view changes that could not be timed, and ONE closed word saying which
   *  of the four situations this is (see `pageActivity.ts`). Counts and one
   *  word from a fixed list: no label, no URL, nothing a person typed.
   *  Always present from this kit version on, including when it says it
   *  cannot tell — which is how a kit too old to have it is told apart. */
  pageActivity?: PageActivityFacts;
}

export interface WebSnapshotEvents {
  longTaskCount: number;
  deadClickCount: number;
  actionableClickCount: number;
  registeredRouteCount: number;
  unreachableRouteCount: number;
  navLoopMaxBounces: number;
  requestBurstMax1s: number;
}

/** Top-level build-identity object for the Patch Lag meter. Every field is
 *  optional and included ONLY when honestly known; the whole object is omitted
 *  from the snapshot when nothing is known (never fabricated). */
export interface WebSnapshotBuild {
  /** Lowercase hex commit sha (7–40 chars), when known. */
  commit?: string;
  /** Build time as epoch ms, when known. */
  buildTimeMs?: number;
  /** now − buildTimeMs at capture, clamped ≥ 0. Present only with buildTimeMs. */
  buildAgeMs?: number;
}

/** Assemble the top-level build object from resolved identity. Returns
 *  undefined when nothing is known so the snapshot omits the field entirely.
 *  `buildAgeMs` is derived (now − buildTimeMs, clamped ≥ 0) only when a build
 *  time is known — never fabricated. */
function buildBuildObject(
  identity: BuildIdentity,
  now: number,
): WebSnapshotBuild | undefined {
  const out: WebSnapshotBuild = {};
  if (identity.commit !== undefined) out.commit = identity.commit;
  if (identity.buildTimeMs !== undefined) {
    out.buildTimeMs = identity.buildTimeMs;
    out.buildAgeMs = Math.max(0, now - identity.buildTimeMs);
  }
  return out.commit !== undefined || out.buildTimeMs !== undefined
    ? out
    : undefined;
}

/** Numeric-only live-event counters for the snapshot. Every getter is
 *  individually guarded so one unsupported browser API can never cost the
 *  whole snapshot (kit-guest-safety rule). */
function buildEvents(): WebSnapshotEvents {
  const n = (fn: () => number): number => {
    try {
      const v = fn();
      return Number.isFinite(v) ? v : 0;
    } catch {
      return 0;
    }
  };
  return {
    longTaskCount: n(() => getLongTaskStats().longTaskCount),
    deadClickCount: n(() => getDeadClickStats().deadClickCount),
    actionableClickCount: n(() => getDeadClickStats().actionableClickCount),
    registeredRouteCount: n(() => getRouteReachabilityStats().registeredCount),
    unreachableRouteCount: n(() => getRouteReachabilityStats().unreachableCount),
    navLoopMaxBounces: n(() => getCircuitStats().navLoopMaxBounces),
    requestBurstMax1s: n(() => getCircuitStats().requestBurstMax1s),
  };
}

function percentile(sortedAsc: number[], p: number): number {
  if (sortedAsc.length === 0) return 0;
  const idx = Math.min(sortedAsc.length - 1, Math.floor(sortedAsc.length * p));
  return sortedAsc[idx];
}

/** Learned-budget model parameters — mirrors the Node/Python `budgets.ts`
 *  (BASELINE_N=20, RECENT_N=20, REGRESSION_FACTOR=1.5). A route needs
 *  BUDGET_BASELINE_N samples before it is scored; it is "regressed" when its
 *  recent-window p95 blows past its learned baseline p95 by the factor. */
const BUDGET_BASELINE_N = 20;
const BUDGET_RECENT_N = 20;
const BUDGET_REGRESSION_FACTOR = 1.5;

/** Build per-route, chronologically-ordered (oldest→newest) duration series
 *  from the recent sample window. Label-free by construction — the route key is
 *  used ONLY to group and is never returned. `recent()` is newest-first, so we
 *  reverse each group to restore chronological order (matching the Node budget
 *  model's ordering). */
function buildRouteSeries(): RouteSeriesLike[] {
  const buf = recent({ limit: SAMPLE_WINDOW });
  const byRoute = new Map<string, number[]>();
  // Iterate oldest→newest so pushes preserve chronological order.
  for (let i = buf.length - 1; i >= 0; i--) {
    const s = buf[i];
    let durs = byRoute.get(s.name);
    if (!durs) {
      durs = [];
      byRoute.set(s.name, durs);
    }
    durs.push(s.duration_ms);
  }
  return [...byRoute.values()].map((durations) => ({ durations }));
}

/** Derive a learned-budget verdict for one route's ordered durations. A route
 *  is "scored" only once it has BUDGET_BASELINE_N samples; "regressed" when its
 *  recent-window p95 exceeds the learned baseline p95 by REGRESSION_FACTOR. */
function budgetVerdictFor(durations: number[]): RouteBudgetVerdict {
  if (durations.length < BUDGET_BASELINE_N) {
    return { scored: false, regressed: false };
  }
  const baseline = durations.slice(0, BUDGET_BASELINE_N);
  const recentSlice = durations.slice(-BUDGET_RECENT_N);
  const baseP95 = percentile(baseline.slice().sort((a, b) => a - b), 0.95);
  const recentP95 = percentile(recentSlice.slice().sort((a, b) => a - b), 0.95);
  if (baseP95 <= 0) return { scored: true, regressed: false };
  return {
    scored: true,
    regressed: recentP95 >= baseP95 * BUDGET_REGRESSION_FACTOR,
  };
}

/** Least-sampled SCORED route's sample count — the weakest scored unit backing
 *  confidence. A route is "scored" once it clears the kit's own axis-scoring
 *  gate (MIN_SAMPLES_FOR_AXES — same as Node/Python/Go/Java); routes below
 *  that are excluded. 0 when nothing is scored yet. Mirrors RN's
 *  leastScorableMounts (min, not total). */
function leastScoredRouteSamples(series: RouteSeriesLike[]): number {
  const counts = series
    .map((r) => r.durations.length)
    .filter((c) => c >= MIN_SAMPLES_FOR_AXES);
  return counts.length ? Math.min(...counts) : 0;
}

/** Group the recent sample window by page into worst-first rows. `recent()`
 *  returns newest-first, so the FIRST sample seen for a page is its latest —
 *  that becomes `last`. */
function buildRows(): WebSnapshotRow[] {
  const buf = recent({ limit: SAMPLE_WINDOW });
  const durationsByRoute = new Map<string, number[]>();
  const lastByRoute = new Map<string, number>();
  for (const s of buf) {
    let durs = durationsByRoute.get(s.name);
    if (!durs) {
      durs = [];
      durationsByRoute.set(s.name, durs);
      lastByRoute.set(s.name, s.duration_ms);
    }
    durs.push(s.duration_ms);
  }
  const rows: WebSnapshotRow[] = [];
  for (const [route, durs] of durationsByRoute) {
    const asc = durs.slice().sort((a, b) => a - b);
    rows.push({
      key: `${SCREEN_PREFIX}${route}`,
      count: durs.length,
      p50: percentile(asc, 0.5),
      p95: percentile(asc, 0.95),
      max: asc[asc.length - 1],
      last: lastByRoute.get(route) ?? 0,
    });
  }
  rows.sort((a, b) => b.p95 - a.p95);
  return rows.slice(0, MAX_SNAPSHOT_ROWS);
}

/** Honest, display-only meters mapped into the server's axis allowlist:
 *   - responsiveness: page-load p75 (LCP-rated samples) vs the LCP bands.
 *   - frustration: the worst observed interaction (INP proxy) vs INP bands —
 *     only when an interaction has actually been measured this session.
 *   - resilience: latency-tail stability (p99/p50 blowup blended with the
 *     poor-sample rate) — mirrors the Node/Python resilience axis.
 *   - stability: main-thread long tasks per minute (browser Long Tasks API) —
 *     only in supporting browsers.
 *  All are ADDITIVE + display-only — none feeds the composite Speed score.
 *  Axes that cannot be computed honestly are simply omitted — the server
 *  renders absent axes as pending. */
function buildAxes(
  sum: Summary,
  now: number,
  startedAt: number,
  identity: BuildIdentity,
): Record<string, WebAxisResult> {
  const axes: Record<string, WebAxisResult> = {};

  // Take one async memory-trend reading off this cadence (fire-and-forget, the
  // same precedent as sampleTimerHealth). No-op unless the API exists AND the
  // context is cross-origin-isolated; the reading lands before a later capture.
  sampleMemoryTrendNow();

  if (sum.total >= MIN_SAMPLES_FOR_AXES && sum.p75_ms != null) {
    const score = linearScore(
      sum.p75_ms,
      SCORE_THRESHOLDS.lcp.good,
      SCORE_THRESHOLDS.lcp.poor,
    );
    axes.responsiveness = {
      score,
      rating: ratingFor(score),
      p75Ms: sum.p75_ms,
      count: sum.total,
    };
  }

  const vitals = getVitals();
  if (vitals.inpMs != null) {
    const score = linearScore(
      vitals.inpMs,
      SCORE_THRESHOLDS.inp.good,
      SCORE_THRESHOLDS.inp.poor,
    );
    axes.frustration = {
      score,
      rating: ratingFor(score),
      worstDelayMs: vitals.inpMs,
    };
  }

  // Resilience — latency-tail stability: the p99/p50 "tail blowup" ratio blended
  // with the poor-sample rate. Mirrors the Node/Python resilience axis (tail
  // good ≤3× · poor ≥8×), minus the budget/regression term the web runtime has
  // no equivalent for. Additive + display-only — never feeds the Speed score.
  // The tail term obeys the cross-kit contract in thresholds.ts: an absolute
  // floor, a sample floor, and an honest abstention when the median is below
  // the capture resolution — where this used to hand out a free 100.
  {
    const tail = readResilienceTail(sum.p50_ms, sum.p99_ms, sum.total);
    if (tail.state !== "insufficient") {
      const p50 = sum.p50_ms!;
      const p99 = sum.p99_ms!;
      if (tail.state === "unmeasurable") {
        // 0.6 of the score is the tail — withhold rather than publish a
        // verdict whose dominant term was never measured.
        axes.resilience = {
          score: null,
          rating: "pending",
          tailRatio: null,
          p50Ms: p50,
          p99Ms: p99,
          sampleCount: sum.total,
        };
      } else {
        const tailScore =
          tail.state === "flat"
            ? 100
            : linearScore(tail.ratio, TAIL_RATIO_THRESHOLDS.good, TAIL_RATIO_THRESHOLDS.poor);
        const poorRate = sum.total > 0 ? sum.poor / sum.total : 0;
        const poorRateScore = linearScore(poorRate, 0.05, 0.25);
        const score = Math.round(0.6 * tailScore + 0.4 * poorRateScore);
        axes.resilience = {
          score,
          rating: ratingFor(score),
          tailRatio: tail.ratio == null ? null : Math.round(tail.ratio * 10) / 10,
          p50Ms: p50,
          p99Ms: p99,
          // The server meter page renders "n samples" from sampleCount (RN
          // sends it too) — without it the web axis renders a dishonest
          // 0-sample count.
          sampleCount: sum.total,
        };
      }
    }
  }

  // Stability — the RN-parity scored rate of ≥50ms main-thread freezes.
  // Unsupported browsers and windows shorter than the scorer's real 10-second
  // floor stay absent. A supported quiet page earns 0/min only after that floor.
  // Additive + display-only — never feeds the Speed score.
  const longTaskStats = getLongTaskStats();
  const stability = computeStabilityScore(longTaskStats);
  if (stability.score !== null && stability.longTasksPerMin !== null) {
    axes.stability = {
      score: stability.score,
      rating: stability.rating as Rating,
      longTaskCount: stability.longTaskCount,
      longTasksPerMin: stability.longTasksPerMin,
      worstBlockMs: stability.worstBlockMs,
      windowMin: stability.windowMin ?? 0,
    };
  }

  // Layout stability — cumulative layout shift (CLS) accumulated on the current
  // page view, rated against Google's published CLS bands (good <=0.1 · poor
  // >=0.25). Only when a layout-shift value has been observed. Additive +
  // display-only — never feeds the Speed score.
  const clsNow = getVitals().cls;
  if (clsNow != null) {
    const cls = Math.round(clsNow * 1000) / 1000;
    const score = linearScore(cls, CLS_THRESHOLDS.good, CLS_THRESHOLDS.poor);
    axes.layoutStability = {
      score,
      rating: ratingFor(score),
      cls,
      caption: `${cls.toFixed(3)} CLS this page`,
    };
  }

  // Memory — current JS heap in use vs the tab's hard limit (Chromium's
  // non-standard performance.memory; omitted on Firefox/Safari/SSR so it is
  // never faked). Rated by how close the page sits to the limit, where the tab
  // OOM-crashes: good <=50% · poor >=90%. Additive + display-only.
  const heap = getHeapStats();
  if (heap != null) {
    const score = linearScore(
      heap.pct,
      MEMORY_PCT_THRESHOLDS.good,
      MEMORY_PCT_THRESHOLDS.poor,
    );
    axes.memory = {
      score,
      rating: ratingFor(score),
      usedMb: heap.usedMb,
      limitMb: heap.limitMb,
      pct: heap.pct,
      caption: `${heap.usedMb} MB of ${heap.limitMb} MB heap (${heap.pct}%)`,
    };
  }

  // Resource efficiency — share of sizable subresources served cached or
  // compressed (the entry URL is NEVER read, only its numeric sizes + initiator
  // type). HIGHER is better, so the score maps efficiency% directly. Omitted
  // until enough eligible resources exist. Additive + display-only.
  const re = getResourceEfficiency();
  if (re != null) {
    const score = linearScoreHigh(
      re.efficientPct,
      RESOURCE_EFF_THRESHOLDS.good,
      RESOURCE_EFF_THRESHOLDS.poor,
    );
    axes.resourceEfficiency = {
      score,
      rating: ratingFor(score),
      eligible: re.eligible,
      efficient: re.efficient,
      efficientPct: re.efficientPct,
      caption: `${re.efficientPct}% of ${re.eligible} resources cached or compressed`,
    };
  }

  // Paint readiness — first-contentful-paint (ms) from the browser's `paint`
  // observer, rated against Google's FCP bands (good ≤1800ms · poor ≥3000ms).
  // Omitted until an FCP entry has actually been observed. Additive +
  // display-only — never feeds the Speed score.
  const fcp = getFcpMs();
  if (fcp != null) {
    const fcpMs = Math.round(fcp);
    const score = linearScore(fcpMs, FCP_THRESHOLDS.good, FCP_THRESHOLDS.poor);
    axes.paintReadiness = {
      score,
      rating: ratingFor(score),
      fcpMs,
    };
  }

  // Interaction to Next Paint — the Core Web Vital (see meterAxes.ts for the
  // documented bands + warm-up gate). Fed by the existing event-timing
  // observer; omitted (never faked) until enough distinct interactions were
  // measured or when interactionId is unsupported. Additive + display-only —
  // never feeds the Speed score. Distinct from the frustration axis, which
  // keeps the single worst delay.
  const inp = computeInteractionToNextPaint(getInpStats());
  if (inp.score !== null && inp.inpMs !== null) {
    axes.interactionToNextPaint = {
      score: inp.score,
      rating: inp.rating as Rating,
      inpMs: inp.inpMs,
      interactionCount: inp.interactionCount,
    };
  }

  // Animation smoothness — long-animation-frames (LoAF) per minute from the
  // browser's `long-animation-frame` observer, rated good ≤2/min · poor
  // ≥30/min. Omitted when LoAF is unsupported (never faked to 0) and while the
  // observer is still warming (<30s observed). Additive + display-only — never
  // feeds the Speed score.
  const loaf = getLoafStats();
  if (loaf.supported && loaf.elapsedMs >= LOAF_MIN_ELAPSED_MS) {
    const score = linearScore(
      loaf.loafPerMin,
      LOAF_THRESHOLDS.good,
      LOAF_THRESHOLDS.poor,
    );
    axes.animationSmoothness = {
      score,
      rating: ratingFor(score),
      loafPerMin: loaf.loafPerMin,
      worstMs: loaf.worstMs,
      count: loaf.count,
      // The observation the rate came from, so the caption can state it.
      windowMin: windowMinOf(loaf.elapsedMs),
    };
  }

  // Crash-free (universal) — crashes per hour from the crash-hook counter,
  // rated good 0/hr · poor ≥3/hr. Warming up until the window reaches the
  // minimum, but a crash surfaces IMMEDIATELY even during warm-up. Additive +
  // display-only — never feeds the Speed score.
  const crashes = crashCount();
  // Eligibility is judged on the RAW elapsed time, never on the reported
  // two-decimal window (see windowMinOf in rateHonesty.ts).
  const crashWindowMs = now - startedAt;
  const windowMin = windowMinOf(crashWindowMs);
  // The earned-rate contract (rateHonesty.ts): the projection and the score it
  // feeds wait for a window that earned them. A crash still surfaces the moment
  // it happens, as a count over the window actually observed.
  const crashPerHour = earnedPerHour(crashes, crashWindowMs);
  if (crashPerHour !== null) {
    const score = linearScore(
      crashPerHour,
      CRASHFREE_RATE_THRESHOLDS.good,
      CRASHFREE_RATE_THRESHOLDS.poor,
    );
    axes.crashFree = {
      score,
      rating: ratingFor(score),
      crashes,
      windowMin,
    };
  } else if (crashes > 0) {
    axes.crashFree = { score: null, rating: "pending", crashes, windowMin };
  }

  // ─── New parity axes (RN-mirrored) ────────────────────────────────────
  // Every one is ADDITIVE + display-only, honest-gated (omitted until it has
  // enough real data), and label-free (counts/durations/ratios only).

  const series = buildRouteSeries();

  // Confidence — the least-sampled SCORED route (weakest scored unit). Emitted
  // as FOUR scalar top-level axes keys (not an object), per the shared
  // contract. Level "none" => omit entirely (nothing scored yet).
  const confSamples = leastScoredRouteSamples(series);
  const level = confidenceLevel(confSamples);
  if (level !== "none") {
    // FOUR scalar sibling keys (not an object), per the shared contract. The
    // axes map is typed for object axes, so the bare scalars are written
    // through a narrow cast — the wire still carries plain string/number values.
    const scalar = axes as unknown as Record<string, WebAxisValue>;
    scalar.confidence = level;
    scalar.confidenceMounts = confSamples;
    scalar.confidenceRating = confidenceRatingFor(level);
    scalar.confidenceCaption = confidenceCaptionFor(level);
  }

  // Budget — share of scored routes on their learned load budget. Pending
  // (omitted) until at least one route has enough samples to be scored.
  const budget = computeBudgetCompliance(
    series.map((r) => budgetVerdictFor(r.durations)),
  );
  if (budget.score != null && budget.pct != null) {
    axes.budget = {
      score: budget.score,
      rating: budget.rating as Rating,
      onBudget: budget.onBudget,
      total: budget.total,
      pct: budget.pct,
    };
  }

  // Baseline — each route graded against its OWN earlier history. Pending
  // (omitted) until a route has enough mounts to split baseline vs. recent.
  const baseline = computeBaselineScore(series);
  // Emitted whenever there is anything to say: a real verdict, OR an abstention
  // because every route with enough samples was too fast to divide by. Omitting
  // the abstention would leave the tile reading "warming up" forever.
  if (baseline.score != null || baseline.unscoredRoutes > 0) {
    axes.baseline = {
      score: baseline.score,
      rating: baseline.rating as Rating,
      worstRatio: baseline.worstRatio ?? 0,
      worstBaselineMs: baseline.worstBaselineMs ?? 0,
      worstCurrentMs: baseline.worstCurrentMs ?? 0,
      anomalyCount: baseline.anomalyCount,
      scoredRoutes: baseline.scoredRoutes,
      unscoredRoutes: baseline.unscoredRoutes,
      measurable: baseline.measurable,
    };
  }

  // Network — outbound-call reliability. Where the kit could wrap the app's
  // own fetch/XHR it watches calls at their START, so a hang, a timeout and an
  // abort are each visible and told apart, and each call carries a group label
  // from a closed vocabulary saying WHICH part of the backend it went to.
  // Where wrapping was impossible it falls back to the passive completion-only
  // resource-timing reading: same numbers as before, honest zeroes for the
  // outcomes that mode genuinely cannot see, and no groups (never guessed).
  // Pending (omitted) until >=3 attempts observed; stays absent forever if the
  // app makes no outbound calls (correct — never faked).
  const net = readNetwork();
  const netGroups = readCallGroups();
  const netScored = net.score != null && net.p75Ms != null && net.stallPct != null;
  // The score keeps its warm-up floor — a number drawn from one or two calls
  // would be noise. The EVIDENCE does not: an app whose only call hangs and
  // never returns is precisely the app this reading exists for, and holding
  // the whole axis back until three calls complete would make that app look
  // like one that never called anything. So when watching produced something
  // a developer can act on — a hang, a timeout, a failure, or any group at
  // all — the axis is emitted unscored: counts and groups, an explicit
  // pending rating, and no invented score, percentage or p75.
  const netActionable =
    net.stallCount > 0 ||
    net.timeoutCount > 0 ||
    net.failedCount > 0 ||
    netGroups.length > 0;
  if (netScored || netActionable) {
    const network: WebAxisResult = {
      score: netScored ? net.score : null,
      rating: netScored ? (net.rating as Rating) : "pending",
      attemptCount: net.attemptCount,
      failedCount: net.failedCount,
      timeoutCount: net.timeoutCount,
      stallCount: net.stallCount,
      worstMs: net.worstMs,
      // 1 = calls watched where they start (hangs knowable), 0 = completion-
      // only fallback. Without this the reader cannot tell "no hangs" from
      // "hangs cannot be seen here".
      watching: isCallWatchActive() ? 1 : 0,
      abortedCount: getAbortedCount(),
      inFlightCount: getInFlightCount(),
    };
    // Derived numbers ride only when they are really derived, so an unscored
    // reading never carries a 0% stall rate or a 0ms p75 it did not measure.
    if (net.stallPct != null) network.stallPct = net.stallPct;
    if (net.p75Ms != null) network.p75Ms = net.p75Ms;
    if (netGroups.length > 0) {
      network.groups = netGroups;
      network.groupsOverflow = getGroupOverflowCount();
    }
    axes.network = network;
  }

  // Data distance — how far this app is from its own backend, judged ONLY on
  // how long a NEW connection takes to open and secure. That is pure distance:
  // it does not move when the backend gets slower or the query gets heavier,
  // which is why a round trip is never used for this.
  //
  // Not the same reading as `handshakeCost`: that one times the PAGE's own
  // document load once. This one watches the app's calls to its backend and
  // says what the distance costs on a typical call.
  //
  // OWN BACKEND ONLY. Every other origin hides its connection phases unless it
  // opts in, and almost nobody does — so the browser abstains for everything
  // else instead of reporting a zero. Absent entirely when no new connection
  // was opened at all (every call reused a warm one): honest absence, not a
  // warming meter.
  const distStats = getDependencyDistanceStats();
  if (distStats.newConnections > 0) {
    const dist = computeDependencyDistance(distStats);
    const distance: WebAxisResult = {
      score: dist.score,
      rating: dist.rating as Rating,
      measurable: dist.measurable,
      newConnections: dist.newConnections,
      minConnections: dist.minConnections,
      judgedKinds: dist.judgedKinds,
      worstKind: dist.worstKind,
      worstSetupMs: dist.worstSetupMs,
      // An excluded reading LARGER than the published worst, so no surface
      // can print a "worst" smaller than a number this axis is holding.
      unjudgedWorstSetupMs: dist.unjudgedWorstSetupMs,
      unjudgedWorstKind: dist.unjudgedWorstKind,
      unjudgedWorstConnections: dist.unjudgedWorstConnections,
      unjudgedKinds: dist.unjudgedKinds,
      typicalRequestMs: dist.typicalRequestMs,
      hostArea: dist.hostArea,
      ownBackendOnly: dist.ownBackendOnly,
      kinds: dist.kinds,
    } as unknown as WebAxisResult;
    // Rides only when it is really derived — never a 0% share we did not
    // measure.
    if (dist.worstSharePct != null) {
      (distance as Record<string, unknown>).worstSharePct = dist.worstSharePct;
    }
    axes.dependencyDistance = distance;
  }

  // Idle efficiency — main-thread busyness while the page is NOT being driven.
  // Pending (omitted) until enough idle wall time has been observed.
  const idle = readIdle();
  if (idle.score != null && idle.idleBusyPct != null) {
    axes.idle = {
      score: idle.score,
      rating: idle.rating as Rating,
      idleBusyPct: idle.idleBusyPct,
      idleLongTaskCount: idle.idleLongTaskCount,
    };
  }

  // Scroll — frame jank while actively scrolling. Pending (omitted) until
  // enough scroll frames are sampled; stays absent if the user never scrolls.
  const scroll = computeScrollHealth(getScrollStats());
  if (scroll.score != null && scroll.jankPct != null) {
    axes.scroll = {
      score: scroll.score,
      rating: scroll.rating as Rating,
      jankPct: scroll.jankPct,
      frameCount: scroll.frameCount,
      blankEvents: scroll.blankEvents,
      worstBlankPx: scroll.worstBlankPx,
    };
  }

  // Frame Floor — worst completed 1s window's effective FPS while scrolling, so
  // one frozen second is never averaged away. Pending until enough windows.
  const frameFloor = computeFrameFloor(getFrameFloorStats());
  if (frameFloor.score != null && frameFloor.floorFps != null) {
    axes.frameFloor = {
      score: frameFloor.score,
      rating: frameFloor.rating as Rating,
      floorFps: frameFloor.floorFps,
      windowCount: frameFloor.windowCount,
    };
  }

  // Timer Health — growth trend of outstanding set/interval handles (a leak
  // signal). Sample the count off THIS snapshot cadence (an existing hook, not
  // a new timer), then read. Pending (omitted) until enough samples span
  // enough time, and absent entirely when wrapping was impossible.
  sampleTimerHealth();
  const timer = readTimerHealth();
  if (timer.score != null && timer.growthPerMin != null) {
    axes.timerHealth = {
      score: timer.score,
      rating: timer.rating as Rating,
      timers: timer.timers,
      growthPerMin: timer.growthPerMin,
    };
  }

  const blockingAsync = computeBlockingAsync({
    supported: longTaskStats.supported,
    count: longTaskStats.longTaskCount,
    worstMs: longTaskStats.worstBlockMs,
    windowMs: longTaskStats.observedWindowMs,
  });
  if (
    blockingAsync.score != null &&
    blockingAsync.perMin != null &&
    blockingAsync.windowMin != null
  ) {
    axes.blockingAsync = {
      score: blockingAsync.score,
      rating: blockingAsync.rating as Rating,
      count: blockingAsync.count,
      perMin: blockingAsync.perMin,
      worstMs: blockingAsync.worstMs,
      windowMin: blockingAsync.windowMin,
    };
  }

  const slowCallbacks = readAsyncSlowCallbacks();
  if (
    slowCallbacks.score != null &&
    slowCallbacks.perMin != null &&
    slowCallbacks.windowMin != null
  ) {
    axes.asyncSlowCallbacks = {
      score: slowCallbacks.score,
      rating: slowCallbacks.rating as Rating,
      count: slowCallbacks.count,
      perMin: slowCallbacks.perMin,
      windowMin: slowCallbacks.windowMin,
    };
  }

  const eventLoopLag = readEventLoopLag();
  if (
    eventLoopLag.score != null &&
    eventLoopLag.lagMs != null &&
    eventLoopLag.p95Ms != null &&
    eventLoopLag.windowMin != null
  ) {
    axes.eventLoopLag = {
      score: eventLoopLag.score,
      rating: eventLoopLag.rating as Rating,
      lagMs: eventLoopLag.lagMs,
      p95Ms: eventLoopLag.p95Ms,
      sampleCount: eventLoopLag.sampleCount,
      windowMin: eventLoopLag.windowMin,
    };
  }

  // Swallowed errors (Near-Miss Rate) — errors the host app LOGGED at error
  // level (chained `console.error`) but did NOT crash on, rated good 1/hr ·
  // poor 60/hr. Observed timestamps only (never the message/args). Pending
  // (omitted) until the trailing window reaches the 5-minute minimum, and
  // absent entirely when chaining `console.error` was impossible. Additive +
  // display-only — never feeds the Speed score.
  const swallowed = readSwallowedErrors();
  if (
    swallowed.score != null &&
    swallowed.perHour != null &&
    swallowed.windowMin != null
  ) {
    axes.swallowedErrors = {
      score: swallowed.score,
      rating: swallowed.rating as Rating,
      count: swallowed.count,
      perHour: swallowed.perHour,
      windowMin: swallowed.windowMin,
    };
  }

  // Leak Watch — is the host app leaking stack traces, secrets/keys, or
  // personal details to its OWN users (error-level log/console output, and
  // stack traces rendered onto the page)? COUNTS + category split only —
  // never the matched text/value, the log line, or the page/route name. No
  // routeClassCount on web (there is no response path). Pending (omitted)
  // until the trailing window reaches the 5-minute minimum, and absent
  // entirely when installation was impossible. Additive + display-only —
  // never feeds the Speed score. The caption is rebuilt server-side (never on
  // the wire), exactly like the swallowed-errors axis.
  // Failing Calls — of the calls this page made and the watcher already
  // judged, how many did not work. COUNTS ONLY: the route labels these are
  // keyed by stay in the page and never reach the wire (routeOutcomes.ts
  // explains why a third party's path is not this project's route). Carries
  // no verdict by design — nothing scores or alerts on this reading — so the
  // rating is permanently "not-scored".
  //
  // OMITTED, NEVER ZEROED, when nothing has been watched: a page whose fetch
  // and XHR could not be wrapped at all, or one that has made no call yet,
  // reports no reading rather than a clean bill of health.
  const routeFailures = getRouteOutcomeTotals();
  if (routeFailures !== null) {
    axes.routeFailures = {
      score: null,
      rating: "not-scored",
      parts: routeFailures.parts,
      observed: routeFailures.observed,
      failed: routeFailures.failed,
      untracked: routeFailures.untracked,
      // NO CAPTION, deliberately. Route labels never leave this page and
      // neither does any other prose: what travels is a closed set of counts.
      // A string field is exactly how such a shape grows to hold an address.
      // The panel words these numbers itself, in the page that has them.
    };
  }

  const leak = readLeakWatch();
  if (leak.score != null && leak.perHour != null && leak.windowMin != null) {
    axes.leakWatch = {
      score: leak.score,
      rating: leak.rating as Rating,
      count: leak.count,
      perHour: leak.perHour,
      windowMin: leak.windowMin,
      stackCount: leak.stackCount,
      secretCount: leak.secretCount,
      piiCount: leak.piiCount,
    };
  }

  // Dev Posture (shared additive axis, wave 2) — whether the app is still
  // wearing its development clothes, read ONCE at startup (frozen). EXPOSURE,
  // never safety: a clean tile means none of the development settings we can
  // read were on — NOT that the deployment is hardened. On web the readable
  // checks are debugFlag (process.env.NODE_ENV, when evaluable after bundler
  // inlining) and sourceMaps (one bounded startup HEAD probe for a script's
  // .map). verboseErrors/profilingOpen are not readable → omitted. Each flag
  // rides ONLY when actually read; `checks` counts present flags, `findings`
  // counts the ON ones. Numbers + rating/caption only — never an env value,
  // path, URL, or config string. Omitted entirely when ZERO checks were
  // readable (honest absence). ADDITIVE + display-only — never feeds Speed.
  const posture = readDevPosture();
  if (posture !== null) {
    const dp: WebAxisResult = {
      score: posture.score,
      rating: posture.rating,
      caption: posture.caption,
      findings: posture.findings,
      checks: posture.checks,
      measurable: posture.measurable,
    };
    if (posture.debugFlag !== undefined) dp.debugFlag = posture.debugFlag;
    if (posture.verboseErrors !== undefined) {
      dp.verboseErrors = posture.verboseErrors;
    }
    if (posture.sourceMaps !== undefined) dp.sourceMaps = posture.sourceMaps;
    if (posture.profilingOpen !== undefined) {
      dp.profilingOpen = posture.profilingOpen;
    }
    axes.devPosture = dp;
  }

  // ─── Additive web-meter batch (9 new axes) ─────────────────────────────
  // Every one is ADDITIVE + display-only, honest-gated, and numeric-only on
  // the wire (score + rating + finite-number fields; captions rebuilt
  // server-side).

  // Slow Frame Cause — blocking ms/min plus a coarse dominant-cause split
  // (third-party script / first-party script / style+layout) from the LoAF
  // script attribution. Gate: LoAF supported AND ≥30s observed (zero LoAFs
  // after the gate emits 0s — a supported browser's honest 0).
  const cause = getLoafCauseStats();
  if (cause.supported && cause.elapsedMs >= LOAF_MIN_ELAPSED_MS) {
    const minutes = cause.elapsedMs / 60_000;
    const blockingMsPerMin =
      minutes > 0 ? Math.round((cause.blockingMs / minutes) * 10) / 10 : 0;
    const scriptMs = cause.scriptFirstPartyMs + cause.scriptThirdPartyMs;
    const attributed = scriptMs + cause.styleLayoutMs;
    const scriptPct =
      attributed > 0 ? Math.round((scriptMs / attributed) * 100) : 0;
    const styleLayoutPct =
      attributed > 0 ? Math.round((cause.styleLayoutMs / attributed) * 100) : 0;
    const thirdPartyPct =
      scriptMs > 0
        ? Math.round((cause.scriptThirdPartyMs / scriptMs) * 100)
        : 0;
    const score = linearScore(
      blockingMsPerMin,
      FRAME_CAUSE_THRESHOLDS.good,
      FRAME_CAUSE_THRESHOLDS.poor,
    );
    axes.frameCause = {
      score,
      rating: ratingFor(score),
      blockingMsPerMin,
      scriptPct,
      thirdPartyPct,
      styleLayoutPct,
    };
  }

  // Third-Party Cost — main-thread ms/min burned by scripts from other origins
  // (same LoAF script attribution). Gate: LoAF supported AND ≥30s observed.
  if (cause.supported && cause.elapsedMs >= LOAF_MIN_ELAPSED_MS) {
    const minutes = cause.elapsedMs / 60_000;
    const thirdPartyMsPerMin =
      minutes > 0
        ? Math.round((cause.scriptThirdPartyMs / minutes) * 10) / 10
        : 0;
    const firstPartyMsPerMin =
      minutes > 0
        ? Math.round((cause.scriptFirstPartyMs / minutes) * 10) / 10
        : 0;
    const scriptMs = cause.scriptFirstPartyMs + cause.scriptThirdPartyMs;
    const thirdPartyPct =
      scriptMs > 0
        ? Math.round((cause.scriptThirdPartyMs / scriptMs) * 100)
        : 0;
    const score = linearScore(
      thirdPartyMsPerMin,
      THIRD_PARTY_THRESHOLDS.good,
      THIRD_PARTY_THRESHOLDS.poor,
    );
    axes.thirdPartyCost = {
      score,
      rating: ratingFor(score),
      thirdPartyMsPerMin,
      firstPartyMsPerMin,
      thirdPartyPct,
    };
  }

  // Back/Forward Cache — restored vs blocked back/forward navigations. Gate:
  // ≥1 bf nav observed (event-dependent — never a warming tile).
  const bf = getBfCacheStats();
  if (bf.bfNavCount >= 1) {
    const score = Math.round((100 * bf.restoredCount) / bf.bfNavCount);
    axes.bfcache = {
      score,
      rating: ratingFor(score),
      bfNavCount: bf.bfNavCount,
      restoredCount: bf.restoredCount,
      blockedCount: bf.blockedCount,
      blockerUnload: bf.blockerUnload,
      blockerCacheControl: bf.blockerCacheControl,
      // Named causes the old two-bucket list could only call "other", which
      // told a developer their page was excluded and nothing about why.
      blockerInFlight: bf.blockerInFlight,
      blockerHeldConnection: bf.blockerHeldConnection,
      blockerBrowser: bf.blockerBrowser,
      blockerOther: bf.blockerOther,
      // The browser named NOTHING — a different answer from "we have no word
      // for what it named", and the difference decides whether the developer
      // can go and read the reason themselves.
      blockerUnnamed: bf.blockerUnnamed,
    };
  }

  // Input Readiness — discrete inputs (taps) that landed before first-
  // contentful-paint (dropped early taps). Gate: FCP has been observed.
  //
  // NOTE ON FIELD NAMING: the shared, byte-parity-locked PII guard
  // (`no-pii.ts`) denies any field/axis KEY containing the fragment "input"
  // (it is on PII_DENYLIST alongside value/query/search), so the spec's
  // `inputReadiness` / `inputCount` / `earlyInputCount` names would be rejected
  // at transmit and drop the ENTIRE snapshot. To stay PII-clean AND honest we
  // emit the axis under "tap"-based names that carry the identical numbers:
  //   inputReadiness -> tapReadiness, inputCount -> tapCount,
  //   earlyInputCount -> earlyTapCount.
  // The server side must mirror these names (see the subagent report).
  const readiness = getInputReadinessStats();
  if (readiness.fcpKnown) {
    const score = linearScore(
      readiness.earlyInputCount,
      EARLY_INPUT_THRESHOLDS.good,
      EARLY_INPUT_THRESHOLDS.poor,
    );
    axes.tapReadiness = {
      score,
      rating: ratingFor(score),
      earlyTapCount: readiness.earlyInputCount,
      tapCount: readiness.inputCount,
    };
  }

  // Soft Navigation — responsiveness (soft-nav start → next LCP) p75 + worst.
  // Gate: ≥1 completed measurement (observer attach alone is not enough).
  const softNav = getSoftNavStats();
  if (softNav.samples.length >= 1) {
    const p75Ms = Math.round(p75Of(softNav.samples));
    const worstMs = Math.round(Math.max(...softNav.samples));
    const score = linearScore(
      p75Ms,
      SOFT_NAV_THRESHOLDS.good,
      SOFT_NAV_THRESHOLDS.poor,
    );
    axes.softNav = {
      score,
      rating: ratingFor(score),
      navCount: softNav.navCount,
      p75Ms,
      worstMs,
    };
  }

  // Press → screen usable — the WHOLE wait, from the press to the view it
  // opened producing its own reading. Emitted unconditionally, counts and
  // all: a press that reached no timed screen is a fact about this app, and
  // the server has the words for "nothing joined yet"
  // (docs/press-to-screen-contract.md).
  // ONE derivation, whatever surface asks. The kit's own panel reads the same
  // function, so "this browser can never measure it" cannot be true of the
  // upload and absent from the panel a developer is looking at.
  axes.pressToScreen = { ...readPressToScreen() };

  // Blocking Time — the Total Blocking Time field cousin: long-task overage
  // ms/min. Gate: longtask observer attached AND ≥30s observed (0 = good).
  const bt = getBlockingTimeStats();
  if (bt.supported && bt.elapsedMs >= BLOCKING_MIN_ELAPSED_MS) {
    const score = linearScore(
      bt.blockingMsPerMin,
      BLOCKING_TIME_THRESHOLDS.good,
      BLOCKING_TIME_THRESHOLDS.poor,
    );
    axes.blockingTime = {
      score,
      rating: ratingFor(score),
      totalBlockingMs: bt.totalBlockingMs,
      blockingMsPerMin: bt.blockingMsPerMin,
      longTaskCount: bt.longTaskCount,
    };
  }

  // Memory Trend — heap growth slope (MB/min) via
  // measureUserAgentSpecificMemory (COI-only; absent everywhere else). Gate:
  // ≥3 readings spanning ≥2 minutes. Score clamps growth at a 0 floor.
  const mem = getMemoryTrendStats();
  if (
    mem.supported &&
    mem.sampleCount >= MEMORY_TREND_MIN_SAMPLES &&
    mem.spanMs >= MEMORY_TREND_MIN_SPAN_MS
  ) {
    const growthForScore = Math.max(0, mem.growthMbPerMin);
    const score = linearScore(
      growthForScore,
      MEMORY_TREND_THRESHOLDS.good,
      MEMORY_TREND_THRESHOLDS.poor,
    );
    axes.memoryTrend = {
      score,
      rating: ratingFor(score),
      usedMb: mem.usedMb,
      growthMbPerMin: mem.growthMbPerMin,
      sampleCount: mem.sampleCount,
    };
  }

  // Resource Weight — total transferred KB by initiator-type bucket (entry
  // URLs never read). Gate: ≥5 sized resources.
  const bloat = getResourceBloat();
  if (bloat != null) {
    const score = linearScore(
      bloat.totalKb,
      RESOURCE_BLOAT_THRESHOLDS.good,
      RESOURCE_BLOAT_THRESHOLDS.poor,
    );
    axes.resourceBloat = {
      score,
      rating: ratingFor(score),
      totalKb: bloat.totalKb,
      scriptKb: bloat.scriptKb,
      cssKb: bloat.cssKb,
      imageKb: bloat.imageKb,
      fetchKb: bloat.fetchKb,
      otherKb: bloat.otherKb,
      resourceCount: bloat.resourceCount,
    };
  }

  // Rage Clicks — privacy-safe frustration: click bursts/min + worst post-burst
  // response. Gate: ≥30s observed (0 bursts = good; the click listener is
  // universal, so this axis warms on every page).
  const rage = getRageClickStats();
  if (rage.elapsedMs >= RAGE_MIN_ELAPSED_MS) {
    const score = linearScore(
      rage.ragePerMin,
      RAGE_CLICK_THRESHOLDS.good,
      RAGE_CLICK_THRESHOLDS.poor,
    );
    axes.rageClicks = {
      score,
      rating: ratingFor(score),
      burstCount: rage.burstCount,
      ragePerMin: rage.ragePerMin,
      worstDelayMs: rage.worstDelayMs,
      // The observation the rate came from, so the caption can state it.
      windowMin: windowMinOf(rage.elapsedMs),
    };
  }

  // ─── 2026-08 web platform batch ─────────────────────────────────────────
  // Every axis below is ADDITIVE + display-only, honest-gated (absent, not
  // zero, until it has a real reading), feature-detected (a missing API means
  // the axis is absent), and numeric-only on the wire. None feeds the Speed
  // score. Most are snapshot-time reads of PerformanceNavigationTiming /
  // PerformanceResourceTiming / navigator / document — no new sampler.

  // Take one async storage-estimate reading off this cadence (fire-and-forget,
  // same precedent as sampleMemoryTrendNow). No-op unless the API exists; the
  // reading lands before a later capture (absent until then).
  sampleStorageHeadroomNow();
  // Same precedent, for the service-worker REGISTRATION read: knowing an app
  // never registered one is what stops "not controlled" being reported as a
  // fault on every install that simply does not use service workers.
  sampleSwControlNow();

  // Navigation Readiness — domInteractive / domComplete / loadEventEnd (ms from
  // navigation start). ONE axis with the three load-phase milestones (more
  // honest than three near-identical tiles). Score on domInteractive. Absent
  // until the page has fully loaded.
  const navR = getNavReadinessStats();
  if (navR != null) {
    const score = linearScore(
      navR.domInteractiveMs,
      NAV_READINESS_THRESHOLDS.good,
      NAV_READINESS_THRESHOLDS.poor,
    );
    axes.navReadiness = {
      score,
      rating: ratingFor(score),
      domInteractiveMs: navR.domInteractiveMs,
      domCompleteMs: navR.domCompleteMs,
      loadEventMs: navR.loadEventMs,
    };
  }

  // Redirect Overhead — redirect time (ms) + count on the main navigation.
  const redirect = getRedirectStats();
  if (redirect != null) {
    const score = linearScore(
      redirect.redirectMs,
      REDIRECT_THRESHOLDS.good,
      REDIRECT_THRESHOLDS.poor,
    );
    axes.redirectOverhead = {
      score,
      rating: ratingFor(score),
      redirectMs: redirect.redirectMs,
      redirectCount: redirect.redirectCount,
    };
  }

  // Server Processing Time — worst Server-Timing duration reported (ms). Absent
  // when the server sent no Server-Timing entries (never faked).
  const serverT = getServerTimingStats();
  if (serverT != null) {
    const score = linearScore(
      serverT.serverMs,
      SERVER_TIMING_THRESHOLDS.good,
      SERVER_TIMING_THRESHOLDS.poor,
    );
    axes.serverTiming = {
      score,
      rating: ratingFor(score),
      serverMs: serverT.serverMs,
      serverEntryCount: serverT.serverEntryCount,
    };
  }

  // Connection/Handshake Cost — DNS+TCP+TLS negotiation (ms) on the main
  // navigation. Renamed from the Node kit's connectionSetup key to dodge the
  // global collision. A warm reused connection is an honest 0.
  const handshake = getHandshakeStats();
  if (handshake != null) {
    const score = linearScore(
      handshake.handshakeMs,
      HANDSHAKE_THRESHOLDS.good,
      HANDSHAKE_THRESHOLDS.poor,
    );
    axes.handshakeCost = {
      score,
      rating: ratingFor(score),
      handshakeMs: handshake.handshakeMs,
      dnsMs: handshake.dnsMs,
      tcpMs: handshake.tcpMs,
      tlsMs: handshake.tlsMs,
    };
  }

  // Protocol Mix — % of timed resources on a modern protocol (HTTP/2 or
  // HTTP/3). HIGHER is better. Absent until ≥3 entries expose the protocol.
  const proto = getProtocolMixStats();
  if (proto != null) {
    const score = linearScoreHigh(
      proto.modernPct,
      PROTOCOL_MIX_THRESHOLDS.good,
      PROTOCOL_MIX_THRESHOLDS.poor,
    );
    axes.protocolMix = {
      score,
      rating: ratingFor(score),
      modernPct: proto.modernPct,
      h2Pct: proto.h2Pct,
      h3Pct: proto.h3Pct,
      protoResourceCount: proto.protoResourceCount,
    };
  }

  // Render-Blocking Pressure — count of observed resources marked
  // render-blocking. Absent when renderBlockingStatus is unsupported.
  const rb = getRenderBlockingStats();
  if (rb != null) {
    const score = linearScore(
      rb.renderBlockingCount,
      RENDER_BLOCKING_THRESHOLDS.good,
      RENDER_BLOCKING_THRESHOLDS.poor,
    );
    axes.renderBlocking = {
      score,
      rating: ratingFor(score),
      renderBlockingCount: rb.renderBlockingCount,
      observedCount: rb.observedCount,
    };
  }

  // Transfer Waste — % of transferred bytes beyond decoded size (over-transfer)
  // + absolute waste KB. Distinct from resourceEfficiency (cached/compressed
  // SHARE). Absent until ≥5 sized resources.
  const waste = getTransferWasteStats();
  if (waste != null) {
    const score = linearScore(
      waste.overheadPct,
      TRANSFER_WASTE_THRESHOLDS.good,
      TRANSFER_WASTE_THRESHOLDS.poor,
    );
    axes.transferWaste = {
      score,
      rating: ratingFor(score),
      overheadPct: waste.overheadPct,
      wasteKb: waste.wasteKb,
      sizedCount: waste.sizedCount,
    };
  }

  // Cache Revalidation Rate — % of cache-eligible resources that still needed a
  // network revalidation vs a fresh cache hit. Absent until ≥5 eligible.
  const reval = getCacheRevalidationStats();
  if (reval != null) {
    const score = linearScore(
      reval.revalidatedPct,
      CACHE_REVALIDATION_THRESHOLDS.good,
      CACHE_REVALIDATION_THRESHOLDS.poor,
    );
    axes.cacheRevalidation = {
      score,
      rating: ratingFor(score),
      revalidatedPct: reval.revalidatedPct,
      revalidatedCount: reval.revalidatedCount,
      cachedCount: reval.cachedCount,
    };
  }

  // Cache Reuse — of everything this page loaded, how much the browser served
  // from its own cache and how much crossed the network again, plus what that
  // re-fetching cost in bytes and time. Exact where the browser states the
  // delivery method outright (`deliveryType`), byte-derived otherwise. A
  // resource whose sizes the browser will not report is left out of BOTH
  // sides — a guess there would invent waste. Absent until ≥5 judgeable
  // resources. Additive: never feeds the Speed score.
  const reuse = getCacheReuseStats();
  if (reuse != null) {
    const score = linearScoreHigh(
      reuse.hitPct,
      CACHE_REUSE_THRESHOLDS.good,
      CACHE_REUSE_THRESHOLDS.poor,
    );
    axes.cacheReuse = {
      score,
      rating: ratingFor(score),
      hitPct: reuse.hitPct,
      cachedCount: reuse.cachedCount,
      fetchedCount: reuse.fetchedCount,
      networkKb: reuse.networkKb,
      networkMs: reuse.networkMs,
      exactCount: reuse.exactCount,
    };
  }

  // Platform Cache — the hosting platform's OWN verdict on the replies THIS
  // SITE served (Vercel, Cloudflare, Netlify and anything speaking RFC 9211
  // all declare hit or miss in the reply itself). Only replies from the page's
  // own origin count: a cache verdict from someone else's API or CDN is that
  // service's business, and attributing it to this project's hosting would be
  // a confident answer to a question nobody asked. Replies the platform
  // deliberately bypasses are counted but kept out of the share — a
  // personalised page SHOULD be served fresh. Absent (never a zero) until ≥5
  // cacheable replies from this site have declared themselves.
  const edge = getEdgeCacheStats();
  if (edge != null) {
    const score = linearScoreHigh(
      edge.hitPct,
      EDGE_CACHE_THRESHOLDS.good,
      EDGE_CACHE_THRESHOLDS.poor,
    );
    axes.edgeCache = {
      score,
      rating: ratingFor(score),
      hitPct: edge.hitPct,
      checked: edge.checked,
      hits: edge.hits,
      misses: edge.misses,
      stale: edge.stale,
      bypass: edge.bypass,
    };
  }

  // Initiator Balance — which broad initiator class dominates resource count
  // (script / css / img / fetch). Display-only balance signal (no good/poor
  // "failure" — rated by how concentrated the dominant share is). Absent until
  // ≥5 resources.
  const initiator = getInitiatorBalanceStats();
  if (initiator != null) {
    const dominantPct =
      initiator.resourceCount > 0
        ? Math.round((initiator.dominantCount / initiator.resourceCount) * 100)
        : 0;
    const score = linearScore(dominantPct, 60, 95);
    axes.initiatorBalance = {
      score,
      rating: ratingFor(score),
      scriptPct: initiator.scriptPct,
      cssPct: initiator.cssPct,
      imgPct: initiator.imgPct,
      fetchPct: initiator.fetchPct,
      dominantCount: initiator.dominantCount,
      resourceCount: initiator.resourceCount,
      // Requests made AFTER the page finished loading, excluded from the
      // percentages above. A deliberate live-updating section is not bad
      // page construction, and the excluded work is published rather than
      // silently dropped.
      postLoadCount: initiator.postLoadCount,
    };
  }

  // Storage Headroom — origin storage used vs quota. Filled by the async
  // estimate reading above; absent until the first reading lands / when the
  // Storage API is unsupported.
  const storage = getStorageHeadroomStats();
  if (storage != null) {
    const score = linearScore(
      storage.usedPct,
      STORAGE_HEADROOM_THRESHOLDS.good,
      STORAGE_HEADROOM_THRESHOLDS.poor,
    );
    axes.storageHeadroom = {
      score,
      rating: ratingFor(score),
      usedPct: storage.usedPct,
      usedMb: storage.usedMb,
      quotaMb: storage.quotaMb,
    };
  }

  // Storage Speed / Storage Failures — the page's OWN local store (Web
  // Storage), behind the BOOSTHIS_STORAGE_METER opt-in. TWO readings from ONE
  // observation point, because a slow store and a failing store are different
  // problems: a store that answers in a microsecond and throws on every write
  // reads as perfectly healthy if only the speed is shown.
  //
  // ABSENT (both keys omitted) when the opt-in is off, when this runtime has
  // no Web Storage, or before the warm-up gate — never a fabricated zero for a
  // store nobody watched. Where storage access itself throws (a sandboxed
  // frame, blocked site data) the readers return the declared-silence shape so
  // the tile says "not available here · where this app runs blocks the
  // reading" instead of going quiet. IndexedDB is deliberately never watched
  // (docs/decisions/browser-local-store-observation.md). Additive +
  // display-only — neither ever feeds the Speed score.
  const storeLatency = readWebStorageLatency();
  if (storeLatency) {
    axes.storageLatency = { ...storeLatency } as WebAxisResult;
  }
  const storeFailures = readWebStorageFailures();
  if (storeFailures) {
    axes.storageFailures = { ...storeFailures } as WebAxisResult;
  }

  // Service Worker Control — whether the page is controlled by a service
  // worker (1/0). Absent when serviceWorker is unsupported.
  //
  // WHY THERE ARE NOW THREE ANSWERS. "Controlled = 100, otherwise 60" gave
  // the same orange verdict to every install that had simply never
  // registered a service worker — which is most apps, and is not a defect.
  // An axis whose only answer is orange carries no information. So:
  //
  //   controlled                    → 100, the worker is serving the page
  //   registered but not in control → 60, a REAL finding: the app asked for
  //                                   a worker and this navigation is not
  //                                   getting it (first load before the
  //                                   worker claims, or a failed claim)
  //   no registration at all        → no verdict. The app does not use
  //                                   service workers; there is nothing to
  //                                   compare it against and nothing to fix.
  //   registration unknown          → no verdict either. The async read has
  //                                   not landed or the browser refused it,
  //                                   and guessing "none" invents the fact
  //                                   the verdict turns on.
  const sw = getSwControlStats();
  if (sw != null) {
    if (sw.controlled) {
      axes.swControl = {
        score: 100,
        rating: ratingFor(100),
        controlled: 1,
        registered: 1,
      };
    } else if (sw.registered === 1) {
      axes.swControl = {
        score: 60,
        rating: ratingFor(60),
        controlled: 0,
        registered: 1,
      };
    } else {
      // "not available here", NOT the never-scored word: that one says no
      // verdict is EVER coming for this AXIS, and this axis scores perfectly
      // well on any app that registers a service worker. What is absent is
      // the thing to measure, in THIS app — a different fact, different word.
      axes.swControl = {
        score: null,
        rating: "not-available",
        measurable: 0,
        reasonCode: REASON_NOTHING_TO_COMPARE,
        controlled: 0,
        registered: sw.registered,
      };
    }
  }

  // Connection Quality — browser-reported RTT / downlink / data-saver.
  //
  // CARRIES NO VERDICT, BY DESIGN. This describes the VISITOR'S own connection, not
  // the app: a phone on a slow mobile link reported 300 ms RTT and 1.5 Mbps
  // and the tile called that a poor score, as though the developer had built
  // something wrong. Nothing here is the app's doing and nothing here is
  // actionable, so the numbers stay — they explain a slow page — and the
  // grade goes. Same shape as routeFailures: permanently "not-scored", no
  // reason code, because this is not a reading that could not be taken.
  const conn = getConnectionQualityStats();
  if (conn != null) {
    axes.connectionQuality = {
      score: null,
      rating: "not-scored",
      rttMs: conn.rttMs,
      downlinkMbps: conn.downlinkMbps,
      saveData: conn.saveData,
    };
  }

  // Device Capacity — coarse CPU-core + memory bucket the page runs on. A
  // display-only capacity tile (not a pass/fail): rated by how low-end the
  // device is (fewer cores / less memory → lower score). Absent when neither
  // hint is exposed.
  const device = getDeviceCapacityStats();
  if (device != null) {
    // Higher is better on both; blend the two coarse capacity signals.
    const coreScore = linearScoreHigh(device.cpuCores, 8, 2);
    const memScore = linearScoreHigh(device.memoryGb, 8, 2);
    const score = Math.round((coreScore + memScore) / 2);
    axes.deviceCapacity = {
      score,
      rating: ratingFor(score),
      cpuCores: device.cpuCores,
      memoryGb: device.memoryGb,
    };
  }

  // Prerender Activation — whether this navigation was prerendered before
  // activation (1/0) + prerender lead time (ms). Prerendered = instant, so
  // prerendered is "good". Absent when neither signal is observable.
  const prerender = getPrerenderStats();
  if (prerender != null) {
    const score = prerender.prerendered ? 100 : 85;
    axes.prerender = {
      score,
      rating: ratingFor(score),
      prerendered: prerender.prerendered,
      activationMs: prerender.activationMs,
    };
  }

  // Font Readiness — whether document fonts finished loading (1/0) + ms to
  // readiness. Rated on the readiness time; absent when the Font Loading API is
  // unsupported.
  const font = getFontReadinessStats();
  if (font != null) {
    // Judged on the interval the page's own font decisions control — from the
    // moment the HTML finished arriving to font readiness — not on the whole
    // wait since navigation start, which carries the server round trip in
    // front of it. See FontReadinessStats.fontDelayMs.
    //
    // And while fonts are STILL LOADING there is no readiness time: both ms
    // figures are the elapsed-so-far floor, so the verdict is withheld rather
    // than scoring an unfinished load as the best possible one.
    if (font.fontLoaded !== 1) {
      axes.fontReadiness = {
        score: null,
        rating: "pending",
        fontLoaded: font.fontLoaded,
        fontMs: font.fontMs,
        fontDelayMs: font.fontDelayMs,
      };
    } else {
      const score = linearScore(
        font.fontDelayMs,
        FONT_READINESS_THRESHOLDS.good,
        FONT_READINESS_THRESHOLDS.poor,
      );
      axes.fontReadiness = {
        score,
        rating: ratingFor(score),
        fontLoaded: font.fontLoaded,
        fontMs: font.fontMs,
        fontDelayMs: font.fontDelayMs,
      };
    }
  }

  // Script/Style Footprint — script + stylesheet element count (O(1) length
  // reads). Rated on the combined count; absent in SSR.
  const asset = getAssetFootprintStats();
  if (asset != null) {
    const score = linearScore(
      asset.scriptCount + asset.styleCount,
      ASSET_FOOTPRINT_THRESHOLDS.good,
      ASSET_FOOTPRINT_THRESHOLDS.poor,
    );
    axes.assetFootprint = {
      score,
      rating: ratingFor(score),
      scriptCount: asset.scriptCount,
      styleCount: asset.styleCount,
    };
  }

  // Cross-Origin Isolation — whether the page is cross-origin isolated (1/0).
  // Isolation unlocks high-precision timers + precise memory measurement, so
  // isolated is "good". Absent when crossOriginIsolated is not exposed.
  const coi = getCrossOriginIsolationStats();
  if (coi != null) {
    const score = coi.isolated ? 100 : 85;
    axes.crossOriginIsolation = {
      score,
      rating: ratingFor(score),
      isolated: coi.isolated,
    };
  }

  // Deprecation/Intervention Pressure — browser reports per hour (type counts
  // only). Absent when ReportingObserver is unsupported; warming until the
  // 5-minute minimum window (a report surfaces immediately regardless).
  const report = readReportPressure();
  if (report.installed) {
    const total = report.deprecationCount + report.interventionCount;
    if (report.perHour !== null) {
      const score = linearScore(
        report.perHour,
        REPORT_PRESSURE_THRESHOLDS.good,
        REPORT_PRESSURE_THRESHOLDS.poor,
      );
      axes.reportPressure = {
        score,
        rating: ratingFor(score),
        deprecationCount: report.deprecationCount,
        interventionCount: report.interventionCount,
        perHour: report.perHour,
        windowMin: report.windowMin,
      };
    } else if (total > 0) {
      // A report fired — say so immediately, over the window we actually have.
      // Only the projection and the score wait (the earned-rate contract).
      axes.reportPressure = {
        score: null,
        rating: "pending",
        deprecationCount: report.deprecationCount,
        interventionCount: report.interventionCount,
        perHour: null,
        windowMin: report.windowMin,
      };
    }
  }

  // Unhandled Failure Rate — uncaught errors + unhandled rejections per hour.
  // Distinct from swallowedErrors (survived console.error near-miss). Absent
  // when the listeners never attached; warming until the 5-minute minimum (a
  // failure surfaces immediately regardless).
  const unh = readUnhandled();
  if (unh.installed) {
    const total = unh.errorCount + unh.rejectionCount;
    if (unh.perHour !== null) {
      const score = linearScore(
        unh.perHour,
        UNHANDLED_THRESHOLDS.good,
        UNHANDLED_THRESHOLDS.poor,
      );
      // NOTE: emitted as "unhandledFailures", NOT "unhandledErrors" — the RN
      // kit already owns "unhandledErrors" with a DIFFERENT field shape
      // (count/perHour/windowMin, rejections split into its own axis). Axis
      // keys are global across runtimes, so the browser's combined
      // errors+rejections reading needs its own name.
      axes.unhandledFailures = {
        score,
        rating: ratingFor(score),
        errorCount: unh.errorCount,
        rejectionCount: unh.rejectionCount,
        perHour: unh.perHour,
        windowMin: unh.windowMin,
      };
    } else if (total > 0) {
      // A failure happened — never hidden by a short window. Only the per-hour
      // projection and the score it feeds wait (the earned-rate contract).
      axes.unhandledFailures = {
        score: null,
        rating: "pending",
        errorCount: unh.errorCount,
        rejectionCount: unh.rejectionCount,
        perHour: null,
        windowMin: unh.windowMin,
      };
    }

    // Rejections remain a distinct contract answer even though the recorded
    // browser fold above continues to publish its combined historical axis.
    const rejectionPerHour = unh.rejectionPerHour;
    if (rejectionPerHour !== null) {
      const score = linearScore(rejectionPerHour, 0, 5);
      axes.promiseRejections = {
        count: unh.rejectionCount || null,
        windowMin: unh.windowMin,
        perHour: rejectionPerHour,
        score,
        rating: ratingFor(score),
      };
      axes.rejectionPressure = {
        lateHandled: unh.lateHandled,
        unhandled: unh.rejectionCount || null,
        perHour: rejectionPerHour,
        score,
        rating: ratingFor(score),
      };
    } else if (unh.rejectionCount > 0 || (unh.lateHandled ?? 0) > 0) {
      axes.promiseRejections = {
        count: unh.rejectionCount || null,
        windowMin: unh.windowMin,
        perHour: null,
        score: null,
        rating: "pending",
      };
      axes.rejectionPressure = {
        lateHandled: unh.lateHandled,
        unhandled: unh.rejectionCount || null,
        perHour: null,
        score: null,
        rating: "pending",
      };
    }
  } else {
    axes.promiseRejections = {
      score: null,
      rating: "not-available",
      measurable: 0,
      reasonCode: REASON_NOT_WIRED_BY_HOST,
    };
    axes.rejectionPressure = {
      score: null,
      rating: "not-available",
      measurable: 0,
      reasonCode: REASON_NOT_WIRED_BY_HOST,
    };
  }

  const background = readBackgroundWork();
  if (background) axes.backgroundWork = background;

  // Interaction Volume — distinct interactions observed (count only, no
  // timing). Distinct from interactionToNextPaint (which times them). Absent
  // until ≥1 interaction with an id (feature-detected).
  const iv = getInteractionVolumeStats();
  if (iv != null) {
    // A count-only volume tile: always "good" (informational), never a
    // pass/fail — but scored so the tile renders consistently.
    axes.interactionVolume = {
      score: 100,
      rating: "good",
      interactionCount: iv.interactionCount,
    };
  }

  // Layout-Shift Source Count — worst single shift's contributing-source count
  // (how many nodes moved together). Absent until ≥1 shift with sources.
  const shift = getShiftSourceStats();
  if (shift != null) {
    const score = linearScore(
      shift.worstSourceCount,
      SHIFT_SOURCE_THRESHOLDS.good,
      SHIFT_SOURCE_THRESHOLDS.poor,
    );
    axes.shiftSources = {
      score,
      rating: ratingFor(score),
      worstSourceCount: shift.worstSourceCount,
      shiftCount: shift.shiftCount,
    };
  }

  // Media Playback Health — dropped-video-frame share across playing media.
  // Absent when no <video> exists or the quality API is unsupported (so a page
  // with no video never shows a permanent warming tile).
  const media = getMediaPlaybackStats();
  if (media != null) {
    const score = linearScore(
      media.droppedPct,
      MEDIA_PLAYBACK_THRESHOLDS.good,
      MEDIA_PLAYBACK_THRESHOLDS.poor,
    );
    axes.mediaPlayback = {
      score,
      rating: ratingFor(score),
      droppedPct: media.droppedPct,
      droppedFrames: media.droppedFrames,
      totalFrames: media.totalFrames,
    };
  }

  // DOM Footprint — total element count. OPT-IN ONLY (BOOSTHIS_DOM_FOOTPRINT)
  // because the count is O(DOM). ABSENT (not zero) when the opt-in is off.
  const dom = getDomFootprintStats();
  if (dom != null) {
    const score = linearScore(
      dom.elementCount,
      DOM_FOOTPRINT_THRESHOLDS.good,
      DOM_FOOTPRINT_THRESHOLDS.poor,
    );
    axes.domFootprint = {
      score,
      rating: ratingFor(score),
      elementCount: dom.elementCount,
    };
  }

  // Idle Opportunity — % of requestIdleCallback probes that fired only because
  // their deadline was hit (a starved main thread). OPT-IN ONLY
  // (BOOSTHIS_IDLE_OPPORTUNITY). ABSENT (not zero) when the opt-in is off or
  // until ≥5 probes have fired.
  const idleOpp = getIdleOpportunityStats();
  if (idleOpp != null) {
    const score = linearScore(
      idleOpp.missedPct,
      IDLE_OPPORTUNITY_THRESHOLDS.good,
      IDLE_OPPORTUNITY_THRESHOLDS.poor,
    );
    axes.idleOpportunity = {
      score,
      rating: ratingFor(score),
      idleCalls: idleOpp.idleCalls,
      missedDeadlines: idleOpp.missedDeadlines,
      missedPct: idleOpp.missedPct,
    };
  }

  // Patch Lag — how stale the currently-deployed build is (build age vs the
  // patch-lag bands: good <7d · needs-work <30d · poor otherwise). Scored ONLY
  // when a build time is honestly known (see buildIdentity.ts); otherwise the
  // axis says it cannot be measured here, with the reason — never a fake age
  // and never a silently absent expected axis. ADDITIVE + display-only — it
  // NEVER feeds the composite Speed score. HONESTY: this reports LAG only; it
  // never implies the app is patched or safe. Web scope carries no depCount (no
  // dependency inventory in a browser).
  if (identity.buildTimeMs !== undefined) {
    const buildAgeMs = Math.max(0, now - identity.buildTimeMs);
    axes.patchLag = {
      score: patchLagScore(buildAgeMs),
      rating: ratePatchLag(buildAgeMs),
      buildAgeMs,
      buildTimeMs: identity.buildTimeMs,
    };
  } else {
    // NO BUILD TIME, SO NO AGE — and saying so is the whole point. Until this
    // branch existed the kit fell back to `document.lastModified`, which a
    // server-rendered page fabricates as the response time: every web install
    // in production reported a build between 0.0 and 0.2 SECONDS old and every
    // one was rated good. A meter that cannot fail cannot warn anyone.
    //
    // buildIdentity.ts now refuses a stamp it cannot tell apart from the
    // response, which leaves this axis with nothing to measure. Omitting it
    // would be no better: patchLag is an EXPECTED web axis, so an absent key
    // renders "warming up" — a promise that a reading is coming, made about one
    // that never will. Instead the kit says out loud that it cannot take this
    // reading, with the reason, and the tile reads "not available here · the
    // app has not switched this on". The fix is one line of the customer's
    // config, and now the tile can ask for it.
    axes.patchLag = {
      score: null,
      rating: "pending",
      measurable: 0,
      reasonCode: REASON_NOT_WIRED_BY_HOST,
    };
  }

  // Live Connections — the sockets and streams that stay open for minutes
  // (chat, live dashboards, presence, streamed answers). Every other reading
  // here is driven by work that finishes, so an open connection that has died
  // is otherwise indistinguishable from a calm page. Absent entirely when the
  // page opened none, or before the minimum observation window closes, so an
  // app with no realtime shows nothing rather than a wall of zeros. Additive
  // + display-only — never feeds the Speed score.
  const live = readLiveConnections();
  if (live) {
    const liveAxis: WebAxisResult = {
      score: live.score,
      rating: live.rating,
      caption: live.caption,
      open: live.open,
      peakOpen: live.peakOpen,
      opened: live.opened,
      closed: live.closed,
      drops: live.drops,
      reconnects: live.reconnects,
      reconnectsPerHour: live.reconnectsPerHour,
      medianLifeMs: live.medianLifeMs,
      longestMs: live.longestMs,
      stormCount: live.stormCount,
      quiet: live.quiet,
      stalled: live.stalled,
      undecided: live.undecided,
      neverClosed: live.neverClosed,
      perScreen: live.perScreen,
      flowMeasurable: live.flowMeasurable,
      windowMin: live.windowMin,
      measurable: live.measurable,
    };
    // A rate or a backlog we could not read is OMITTED, never sent as 0 — a
    // zero here would claim "nothing is queued" when the truth is "the
    // transport never said".
    if (live.msgsPerMin != null) liveAxis.msgsPerMin = live.msgsPerMin;
    if (live.backlog != null) liveAxis.backlog = live.backlog;
    axes.liveConnections = liveAxis;
  }

  // AI calls — the model calls this page makes itself. An AI product built as
  // a front end usually calls the provider FROM THE PAGE, and until now that
  // was the slowest and most expensive thing the kit said nothing about. One
  // read of the detector feeds all three axes below, so the wait, the money
  // and the ceiling can never disagree with each other.
  //
  // The unit of work is the person's TURN, not a server request: a page has no
  // inbound request to divide by, and the honest denominator is the wait a
  // person actually sat through. Every field name matches the Node kit, so the
  // tile, the AI answers and the rules need no browser special case.
  //
  // Absent — not zeroed — when this page calls no provider.
  const aiStats = getAiCallStats();
  const aiCalls = computeAiCalls(aiStats);
  if (aiCalls) {
    axes.aiCalls = {
      score: aiCalls.score,
      rating: aiCalls.rating,
      measurable: aiCalls.measurable,
      waitPct: aiCalls.waitPct,
      callCount: aiCalls.callCount,
      watchedRequests: aiCalls.watchedRequests,
      providerCount: aiCalls.providerCount,
      topProvider: aiCalls.topProvider,
      p75Ms: aiCalls.p75Ms,
      worstMs: aiCalls.worstMs,
      streamCount: aiCalls.streamCount,
      ttftP75Ms: aiCalls.ttftP75Ms,
      stallCount: aiCalls.stallCount,
      failCount: aiCalls.failCount,
      rateLimitedCount: aiCalls.rateLimitedCount,
      quotaCount: aiCalls.quotaCount,
      timeoutCount: aiCalls.timeoutCount,
      truncatedCount: aiCalls.truncatedCount,
      filteredCount: aiCalls.filteredCount,
      serverMsP75: aiCalls.serverMsP75,
      serverMsCalls: aiCalls.serverMsCalls,
      unwatchedClients: aiCalls.unwatchedClients,
      // AI-shaped traffic this page sent to a host on no provider list. Left
      // off the wire, a page whose whole AI stack is a private gateway
      // uploaded a row of zeros indistinguishable from a page with no AI at
      // all — the reading existed, and the one number explaining why it was
      // empty stayed in the browser.
      unclassifiedCalls: aiCalls.unclassifiedCalls,
    } as unknown as WebAxisResult;
  }

  // What it cost: tokens in and out, how many the provider served from its own
  // cache, and the money — provider-reported where the provider reports it,
  // otherwise priced from a published list whose DATE ships with the number so
  // no surface can show an estimate without being able to show its age. Calls
  // no price could be put on are counted, never treated as free.
  const aiSpend = computeAiSpend(aiStats, AI_PRICE_TABLE_DAY);
  if (aiSpend) {
    const spend: Record<string, number | string | null> = {
      score: aiSpend.score,
      rating: aiSpend.rating,
      measurable: aiSpend.measurable,
      calls: aiSpend.calls,
      tokensIn: aiSpend.tokensIn,
      tokensOut: aiSpend.tokensOut,
      cachedIn: aiSpend.cachedIn,
      cacheHitPct: aiSpend.cacheHitPct,
      costMicros: aiSpend.costMicros,
      reportedCostCalls: aiSpend.reportedCostCalls,
      pricedCalls: aiSpend.pricedCalls,
      unpricedCalls: aiSpend.unpricedCalls,
      costPerRequestMicros: aiSpend.costPerRequestMicros,
      worstRequestCostMicros: aiSpend.worstRequestCostMicros,
      windowMs: aiSpend.windowMs,
      usageMissingCalls: aiSpend.usageMissingCalls,
      usageMissingPct: aiSpend.usageMissingPct,
      streamUsageMissingCalls: aiSpend.streamUsageMissingCalls,
      repeatWorst: aiSpend.repeatWorst,
      repeatRequests: aiSpend.repeatRequests,
      priceTableDay: aiSpend.priceTableDay,
    };
    axes.aiSpend = spend as unknown as WebAxisResult;
  }

  // How close the project is to its provider's published limits. A browser can
  // be REFUSED a look at those limits — a cross-origin reply shows only what
  // the provider chose to expose — so `headersHidden` rides along: it is the
  // difference between "the ceiling is fine" and "we were not allowed to see
  // the ceiling", and without it a hidden header would render as a comfortable
  // zero. Absent entirely when nothing was read, nothing was hidden and
  // nothing was refused.
  const aiHeadroom = computeAiHeadroom(aiStats);
  if (aiHeadroom) {
    axes.aiHeadroom = {
      score: aiHeadroom.score,
      rating: aiHeadroom.rating,
      measurable: aiHeadroom.measurable,
      reads: aiHeadroom.reads,
      worstRequestsPct: aiHeadroom.worstRequestsPct,
      worstTokensPct: aiHeadroom.worstTokensPct,
      refusals: aiHeadroom.refusals,
      worstRetryAfterMs: aiHeadroom.worstRetryAfterMs,
      headersHidden: aiHeadroom.headersHidden,
    } as unknown as WebAxisResult;
  }

  return axes;
}

/** Rough page "boot" time so the meter can show "running for N" without
 *  collecting any timestamp of user activity. */
function pageStartedAt(now: number): number {
  try {
    if (typeof performance !== "undefined" && performance.timeOrigin) {
      return Math.round(performance.timeOrigin);
    }
  } catch {
    // fall through
  }
  return now;
}

/** Build the current privacy-safe snapshot from live in-process state. Pure —
 *  reads only the sample ring + current vitals; performs no I/O. */
export function capturePerfSnapshot(): WebSnapshotPayload {
  const sum = summary();
  const now = Date.now();
  const startedAt = pageStartedAt(now);
  const identity = resolveBuildIdentity(now);
  const build = buildBuildObject(identity, now);
  const payload: WebSnapshotPayload = {
    capturedAt: now,
    runtimeVersion: RUNTIME_VERSION,
    platform: "web",
    startedAt,
    totalEvents: sum.total,
    rows: buildRows(),
    axes: buildAxes(sum, now, startedAt, identity),
    events: buildEvents(),
  };
  // Include the build object only when at least one component is known — an
  // absent build means "not known", never a fabricated stamp.
  if (build !== undefined) payload.build = build;
  // The route list is read HERE, when a snapshot asks — never at start-up —
  // so handing the kit a router costs the page nothing and never delays a
  // paint or a first request.
  const routeList = routeListForSnapshot(getRegisteredWebRoutes());
  if (routeList !== undefined) payload.routeList = routeList;
  // The control census is read HERE too, for the same reason: asking the
  // page what controls it has is one bounded query plus a bounded walk per
  // match, and it happens when a snapshot asks rather than in front of a
  // paint.
  const controlCensus = controlCensusForSnapshot(currentRouteLabel());
  if (controlCensus !== undefined) payload.controlCensus = controlCensus;
  // ...and what the visit itself consisted of. Read here for the same reason,
  // and ALWAYS included: the whole point of the block is to explain a small
  // number of readings, so it has to travel on exactly the snapshots that
  // carry a small number of readings.
  try {
    payload.pageActivity = pageActivityFacts();
  } catch {
    // A block that cannot be read is omitted rather than guessed at.
  }
  return payload;
}

/** A snapshot worth uploading has at least one recorded sample or route row —
 *  an empty snapshot from a just-loaded page is skipped so we never ship a
 *  content-free payload.
 *
 *  Outbound-call evidence counts as data in its own right. An app with no
 *  backend of its own can sit on one screen and talk straight to a hosted
 *  database: no second page view, no route row, and a query that hangs and
 *  never returns. Requiring a route sample there would mean the one finding
 *  worth uploading is the one thing that never gets uploaded — the page would
 *  read as an app that was never watched. The network axis is itself only
 *  emitted once real calls were observed, so this cannot resurrect the empty
 *  just-loaded page the guard exists for. */
function hasData(snap: WebSnapshotPayload): boolean {
  if (snap.totalEvents > 0 || snap.rows.length > 0) return true;
  // An app used in place is the case this guard was silently swallowing. A
  // drawer-based storefront can take a hundred orders and produce its FIRST
  // reading only when the tab is closed, so every snapshot before that looked
  // content-free and never left — which is exactly the install that most
  // needs explaining. Observed activity is content in its own right. An
  // untouched page still has none of it (no press, no address change, nothing
  // untimed), so the empty just-loaded page this guard exists for is
  // unaffected.
  const act = snap.pageActivity;
  if (
    act &&
    (act.presses > 0 || act.viewChanges > 0 || act.viewChangesUntimed > 0)
  ) {
    return true;
  }
  const net = snap.axes?.network;
  if (!net || typeof net !== "object") return false;
  const count = (v: unknown): number =>
    typeof v === "number" && Number.isFinite(v) ? v : 0;
  return (
    count(net.attemptCount) > 0 ||
    count(net.stallCount) > 0 ||
    count(net.timeoutCount) > 0 ||
    count(net.failedCount) > 0 ||
    count(net.abortedCount) > 0 ||
    (Array.isArray(net.groups) && net.groups.length > 0)
  );
}

export type SnapshotSubmitter = (
  snapshot: WebSnapshotPayload,
  opts?: { keepalive?: boolean },
) => Promise<number>;

let submitter: SnapshotSubmitter | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let inFlight = false;

/** Register (or clear) the function that actually ships a snapshot. The
 *  telemetry client sets this to `transmitSnapshot`; clearing it (not just
 *  stopping the timer) is required because `uploadPerfSnapshotNow()` calls the
 *  submitter DIRECTLY, so a stale submitter could leak a snapshot on a manual
 *  flush after the timer stops. */
export function setSnapshotSubmitter(fn: SnapshotSubmitter | null): void {
  submitter = fn;
}

/** Capture + submit one snapshot now. No-op without a submitter or data.
 *  Never throws — instrumentation must stay silent. Pass `keepalive: true`
 *  from a pagehide handler so the request outlives the dying page (a normal
 *  fetch started there is cancelled by the navigation and the data is lost). */
export async function uploadPerfSnapshotNow(opts?: {
  keepalive?: boolean;
}): Promise<number> {
  if (!submitter || inFlight) return 0;
  const snap = capturePerfSnapshot();
  if (!hasData(snap)) return 0;
  inFlight = true;
  try {
    return await submitter(snap, opts);
  } catch {
    return 0;
  } finally {
    inFlight = false;
  }
}

function scheduleNext(): void {
  if (timer !== null) return;
  // Age-driven turbo ramp: the delay until the NEXT upload is a pure function
  // of how long this session/page has been alive, so a skipped/empty upload
  // never burns a ramp step. `pageStartedAt` reads the browser's own boot time
  // (performance.timeOrigin) — no user activity is timed.
  const now = Date.now();
  const delay = nextSnapshotDelayMs(now - pageStartedAt(now));
  timer = setTimeout(() => {
    timer = null;
    void uploadPerfSnapshotNow().finally(() => {
      // Only keep the loop alive while a submitter is still registered.
      if (submitter) scheduleNext();
    });
  }, delay);
  // In a Node context (tests, SSR) never keep the process alive for the timer.
  const t = timer as { unref?: () => void };
  if (typeof t.unref === "function") t.unref();
}

/** Start the periodic snapshot mirror. Fires one immediate flush (so the AI
 *  has something to read the instant sharing is enabled) then re-uploads every
 *  {@link SNAPSHOT_FLUSH_MS}. Idempotent — a second call while running is a
 *  no-op. */
export function startSnapshotAutoUpload(): void {
  void uploadPerfSnapshotNow();
  scheduleNext();
}

/** Stop the periodic mirror. The submitter is left as-is (the caller clears it
 *  via setSnapshotSubmitter(null) when tearing down) so this only cancels the
 *  timer. */
export function stopSnapshotAutoUpload(): void {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
}

/** @internal test hook — reset module state between hermetic runs. */
export const _snapshotInternals = {
  reset(): void {
    submitter = null;
    inFlight = false;
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  },
  get hasSubmitter(): boolean {
    return submitter !== null;
  },
  get isRunning(): boolean {
    return timer !== null;
  },
};
