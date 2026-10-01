/** Outbound-call sampler — the input for the Network reliability axis.
 *
 * TWO OBSERVATION POINTS, ONE AT A TIME
 *
 * 1. CALL WATCH (preferred, callWatch.ts). The kit wraps the app's own `fetch`
 *    and `XMLHttpRequest`, so a call is recorded when it STARTS. That is the
 *    only way a browser can see a call that never comes back: Resource Timing
 *    fires on completion, so a hang is invisible to it by construction.
 *    Outcomes are told apart — completed, failed, aborted, timed out, and
 *    still hanging at the end of the window.
 *
 * 2. RESOURCE TIMING (fallback, vitals.ts). Where wrapping is impossible —
 *    `fetch` is missing or frozen, an exotic embedding, a browser that refuses
 *    the assignment — the passive resource observer keeps feeding this module
 *    exactly as it always has. In that mode the readings are byte-for-byte the
 *    ones this kit produced before call watching existed: durations plus a
 *    coarse ok/failed outcome, and honest ZEROES for hangs and timeouts,
 *    because that runtime genuinely cannot see them.
 *
 * The two never run together. Once the wrapper attaches, the resource path is
 * suppressed, or every call would be counted twice.
 *
 * HONESTY RULES
 * - A call still in flight for less than HUNG_AFTER_MS is neither good nor bad:
 *   it is excluded from BOTH the numerator and the denominator, so a busy page
 *   never scores itself down for calls that simply have not finished yet.
 * - "Hung" is recomputed from the live in-flight set on every read, never
 *   accumulated. A slow call that eventually returns stops being a hang.
 * - An aborted call has no verdict — the app cancelled it — so it is counted
 *   and shown but kept out of the score's denominator.
 *
 * PRIVACY: counts, durations, and group labels drawn from a closed
 * code-defined vocabulary (callGroups.ts). The URL, host, path, query, headers
 * and payload are never read into anything that leaves the browser.
 */

import {
  computeNetworkScore,
  percentileOf,
  type NetworkStatsLike,
  type NetworkResult,
  type DependencyDistanceStatsLike,
} from "./meterAxes";
import { DEP_KIND_OWN_BACKEND } from "./depKinds";
import {
  CALL_GROUP_OVERFLOW_KEY,
  CALL_GROUP_UNNAMED_KEY,
  MAX_TRACKED_GROUPS,
  isSafeCallGroupKey,
} from "./callGroups";

/** Re-exported so callers that think in terms of the sampler keep working; the
 *  constant itself lives with the group vocabulary the server mirrors. */
export { MAX_TRACKED_GROUPS };

/** Fixed-size ring of attempt durations (ms) for the p75/worst computation. */
const MAX_DURATIONS = 500;
/** Per-group ring — smaller, since a group only needs a stable p75. */
const MAX_GROUP_DURATIONS = 120;
/** How long a call must have been in flight before we are willing to call it
 *  hung. Well past the axis's own "poor" latency budget (3s), so a merely slow
 *  call is never reported as a hang. */
export const HUNG_AFTER_MS = 15_000;
/** Safety valve on the in-flight registry so a runaway page cannot grow it
 *  without bound. Beyond this, calls are still counted when they settle; they
 *  just cannot be reported as hanging. */
const MAX_IN_FLIGHT = 1000;
/** Cap on how many distinct overflow group names we remember, purely to report
 *  how many there were. */
const MAX_OVERFLOW_NAMES = 64;

/** How a watched call ended. */
export type CallOutcome = "completed" | "failed" | "aborted" | "timed-out";

/** Handle returned by noteCallStart and handed back on settle. */
export interface CallHandle {
  readonly id: number;
  readonly group: string;
  readonly startedAt: number;
}

interface GroupAgg {
  completed: number;
  failed: number;
  aborted: number;
  timedOut: number;
  durations: number[];
  worstMs: number;
}

/** One per-group row as it rides the wire. Counts and durations only; `key`
 *  comes from the closed vocabulary in callGroups.ts. */
export interface CallGroupRow {
  key: string;
  attempts: number;
  completed: number;
  failed: number;
  aborted: number;
  timedOut: number;
  hung: number;
  p75Ms: number;
  worstMs: number;
}

let durations: number[] = [];
let completedCount = 0;
let failedCount = 0;
let abortedCount = 0;
let timedOutCount = 0;
let worstMs = 0;

let watching = false;
let nextCallId = 1;
let inFlight = new Map<number, CallHandle>();
let groups = new Map<string, GroupAgg>();
let overflowNames = new Set<string>();
/** Ids already accounted for, so a client that reports two endings for one
 *  call (XHR fires `abort` after `error` in some engines) cannot count twice. */
let settled = new Set<number>();

/** Clock seam so tests can drive the hang threshold deterministically. */
let nowFn: () => number = () => Date.now();

function emptyAgg(): GroupAgg {
  return {
    completed: 0,
    failed: 0,
    aborted: 0,
    timedOut: 0,
    durations: [],
    worstMs: 0,
  };
}

/** Resolve the STORAGE key a call's group lands under, applying the group cap,
 *  and make sure that row exists. A key that arrives once the cap is full folds
 *  into the visible overflow row and its name is remembered so the row can say
 *  how many groups it stands for.
 *
 *  Called when a call STARTS, not when it settles: a group whose only calls are
 *  still hanging must have a row, or the one thing this feature exists to show
 *  would be missing from the very table meant to show it. */
function foldKey(rawKey: string): string {
  const key = isSafeCallGroupKey(rawKey) ? rawKey : CALL_GROUP_UNNAMED_KEY;
  if (groups.has(key)) return key;
  if (groups.size >= MAX_TRACKED_GROUPS && key !== CALL_GROUP_OVERFLOW_KEY) {
    if (overflowNames.size < MAX_OVERFLOW_NAMES) overflowNames.add(key);
    if (!groups.has(CALL_GROUP_OVERFLOW_KEY)) {
      groups.set(CALL_GROUP_OVERFLOW_KEY, emptyAgg());
    }
    return CALL_GROUP_OVERFLOW_KEY;
  }
  groups.set(key, emptyAgg());
  return key;
}

/** The aggregate behind an ALREADY-FOLDED key (the one carried on a handle). */
function aggFor(key: string): GroupAgg {
  return groups.get(key) ?? groups.get(foldKey(key))!;
}

function pushDuration(list: number[], cap: number, ms: number): void {
  list.push(ms);
  if (list.length > cap) list.splice(0, list.length - cap);
}

/** Called by callWatch once it has successfully wrapped at least one client.
 *  Switches this module from the resource-timing fallback to watched calls. */
export function setCallWatchActive(active: boolean): void {
  watching = active;
}

/** Whether calls are being watched where they START (so hangs are visible). */
export function isCallWatchActive(): boolean {
  return watching;
}

/** Record the START of an outbound call. Guarded; never throws. Returns null
 *  when the call could not be registered, in which case the caller simply does
 *  not report a settle. */
export function noteCallStart(group: string): CallHandle | null {
  try {
    const handle: CallHandle = {
      id: nextCallId++,
      // Folded once, here, so the handle always names a row that exists — the
      // settle path and the hang recount can never disagree about where a call
      // belongs, even if the cap filled up while it was in flight.
      group: foldKey(group),
      startedAt: nowFn(),
    };
    if (inFlight.size < MAX_IN_FLIGHT) inFlight.set(handle.id, handle);
    return handle;
  } catch {
    return null;
  }
}

/** Record how a watched call ended. Guarded; never throws. */
export function noteCallSettled(
  handle: CallHandle | null,
  outcome: CallOutcome,
): void {
  try {
    if (handle === null) return;
    if (settled.has(handle.id)) return;
    settled.add(handle.id);
    if (settled.size > MAX_IN_FLIGHT * 2) {
      // Bounded memory: the set only exists to reject a double settle, and a
      // double settle always arrives close behind the first.
      settled = new Set<number>();
    }
    inFlight.delete(handle.id);
    const agg = aggFor(handle.group);
    if (outcome === "completed" || outcome === "failed") {
      const dur = Math.max(0, Math.round(nowFn() - handle.startedAt));
      pushDuration(durations, MAX_DURATIONS, dur);
      pushDuration(agg.durations, MAX_GROUP_DURATIONS, dur);
      if (dur > worstMs) worstMs = dur;
      if (dur > agg.worstMs) agg.worstMs = dur;
    }
    if (outcome === "completed") {
      completedCount++;
      agg.completed++;
    } else if (outcome === "failed") {
      failedCount++;
      agg.failed++;
    } else if (outcome === "aborted") {
      abortedCount++;
      agg.aborted++;
    } else {
      timedOutCount++;
      agg.timedOut++;
    }
  } catch {
    // observation must never break the page
  }
}

/** Move an already-completed call into the failed bucket. Used ONLY by the
 *  opt-in body-error check: a 200 response whose payload carries an error is a
 *  failure the transport never reported. Guarded; never throws. */
export function noteBodyFailure(handle: CallHandle | null): void {
  try {
    if (handle === null) return;
    if (completedCount <= 0) return;
    const agg = groups.get(handle.group);
    if (!agg || agg.completed <= 0) return;
    completedCount--;
    failedCount++;
    agg.completed--;
    agg.failed++;
  } catch {
    // observation must never break the page
  }
}

/** Minimal numeric view of a resource-timing entry — the ONLY fields ever read
 *  (no URL/name). */
export interface ResourceEntryLike {
  duration: number;
  responseStatus?: number;
  responseEnd?: number;
  transferSize?: number;
}

/** Record one completed outbound attempt from a resource-timing entry.
 *
 *  FALLBACK PATH ONLY. Once callWatch has attached, this returns immediately —
 *  the same call would otherwise be counted twice, once at start and once at
 *  completion. Guarded; never throws. */
export function recordResourceEntry(e: ResourceEntryLike): void {
  try {
    if (watching) return;
    const dur = e.duration;
    if (!Number.isFinite(dur) || dur < 0) return;
    const rounded = Math.round(dur);
    pushDuration(durations, MAX_DURATIONS, rounded);
    if (rounded > worstMs) worstMs = rounded;

    // Loud failure: an HTTP error status, OR a request that produced no
    // response at all (aborted/network error the browser still logged as a
    // zero-timing entry). Cross-origin opaque entries report status 0 with a
    // real duration — those are NOT failures, so we require BOTH a zero
    // responseEnd AND a zero duration before calling it failed.
    const status = e.responseStatus;
    const httpError = typeof status === "number" && status >= 400;
    const noResponse =
      (e.responseEnd === 0 || e.responseEnd == null) && rounded === 0;
    if (httpError || noResponse) failedCount++;
    else completedCount++;
  } catch {
    // observation must never break the page
  }
}

/** How many in-flight calls have been running long enough to call hung, and
 *  how many are simply still going. Recomputed on every read. */
function inFlightSplit(): { hung: number; young: number } {
  let hung = 0;
  let young = 0;
  const cutoff = nowFn() - HUNG_AFTER_MS;
  for (const handle of inFlight.values()) {
    if (handle.startedAt <= cutoff) hung++;
    else young++;
  }
  return { hung, young };
}

function hungByGroup(): Map<string, number> {
  const out = new Map<string, number>();
  const cutoff = nowFn() - HUNG_AFTER_MS;
  for (const handle of inFlight.values()) {
    if (handle.startedAt > cutoff) continue;
    // handle.group was folded at start, so it always names an existing row.
    out.set(handle.group, (out.get(handle.group) ?? 0) + 1);
  }
  return out;
}

/** Current network stats (label-free).
 *
 *  In the fallback (resource-timing) mode there are no in-flight calls to read,
 *  so `timeoutCount`/`stallCount` are the honest zeroes this kit has always
 *  reported and `attemptCount` is exactly the old completed+failed total. */
export function getNetworkStats(): NetworkStatsLike {
  const p75 = percentileOf(durations, 0.75);
  const { hung } = inFlightSplit();
  return {
    attemptCount: completedCount + failedCount + timedOutCount + hung,
    completedCount,
    failedCount,
    timeoutCount: timedOutCount,
    stallCount: hung,
    p75Ms: p75,
    worstMs,
  };
}

/** Calls the app cancelled itself. Counted and shown, never scored. */
export function getAbortedCount(): number {
  return abortedCount;
}

/** Calls still running but not yet old enough to be called hung. Reported so a
 *  quiet-looking window is never mistaken for an idle one. */
export function getInFlightCount(): number {
  return inFlightSplit().young;
}

/** How many distinct groups folded into the "everything else" row. */
export function getGroupOverflowCount(): number {
  return overflowNames.size;
}

/** Per-group rows, busiest first, with the overflow row last. Empty in the
 *  fallback mode — resource timing never reads a destination, so a group
 *  cannot be derived and we say nothing rather than guess. */
export function readCallGroups(): CallGroupRow[] {
  const hung = hungByGroup();
  const rows: CallGroupRow[] = [];
  for (const [key, agg] of groups) {
    const h = hung.get(key) ?? 0;
    const attempts = agg.completed + agg.failed + agg.timedOut + h;
    if (attempts === 0 && agg.aborted === 0) continue;
    rows.push({
      key,
      attempts,
      completed: agg.completed,
      failed: agg.failed,
      aborted: agg.aborted,
      timedOut: agg.timedOut,
      hung: h,
      p75Ms: Math.round(percentileOf(agg.durations, 0.75)),
      worstMs: agg.worstMs,
    });
  }
  rows.sort((a, b) => {
    const aOver = a.key === CALL_GROUP_OVERFLOW_KEY ? 1 : 0;
    const bOver = b.key === CALL_GROUP_OVERFLOW_KEY ? 1 : 0;
    if (aOver !== bOver) return aOver - bOver;
    return b.attempts - a.attempts || a.key.localeCompare(b.key);
  });
  return rows;
}

/** Current Network axis reading (pending until >=3 attempts observed). */
export function readNetwork(): NetworkResult {
  return computeNetworkScore(getNetworkStats());
}

/** @internal test hook — seed attempts deterministically without live resource
 *  timing. Each entry mirrors a resource-timing outcome. */
export function _setNetworkSamplesForTests(
  entries: ResourceEntryLike[],
): void {
  _resetNetworkSamplerForTests();
  for (const e of entries) recordResourceEntry(e);
}

/** @internal test hook — drive the sampler's clock. */
export function _setNetworkClockForTests(fn: (() => number) | null): void {
  nowFn = fn ?? (() => Date.now());
}

/* ─── Dependency distance: how long a NEW connection takes to OPEN ───────
 *
 * Distance is judged on connection SETUP only — connect + secure, before a
 * single byte of the request is sent. A round trip would fold in however long
 * the backend spent thinking, and a slow backend would then be reported as a
 * distant one.
 *
 * BROWSER LIMIT, STATED NOT HIDDEN: these phases are only readable for the
 * app's OWN origin unless the other side opts in (Timing-Allow-Origin), and
 * almost nobody does. So this kit measures the app's own backend and says
 * nothing at all about anything else — never a zero standing in for a reading
 * it cannot take.
 *
 * Nothing but numbers is kept: the entry's address is examined by the caller
 * to answer "is this our own origin?" and is never passed in here.
 */

/** Retained setup samples / own-backend call durations (bounded). */
const MAX_SETUP_SAMPLES = 200;
/** A setup longer than this is a hung socket, not a distance reading. */
const MAX_SETUP_MS = 60_000;

let ownBackendSetups: number[] = [];
let ownBackendDurations: number[] = [];

/** The only fields ever read from a resource-timing entry for distance. */
export interface ResourceSetupLike {
  /** Did this call go to the app's OWN origin? Decided by the caller. */
  sameOrigin: boolean;
  connectStart?: number;
  connectEnd?: number;
  duration?: number;
}

/**
 * Record one same-origin resource-timing entry's connection setup. A REUSED
 * connection reports no connect phase (connectEnd === connectStart) and is
 * skipped — nothing was set up, so there is nothing to time. Guarded; never
 * throws, and runs whether or not call-watching attached (the wrapper cannot
 * see connection phases, only this observer can).
 */
export function recordResourceSetup(e: ResourceSetupLike): void {
  try {
    if (!e || !e.sameOrigin) return;
    const dur = e.duration;
    if (typeof dur === "number" && Number.isFinite(dur) && dur > 0) {
      pushDuration(ownBackendDurations, MAX_SETUP_SAMPLES, Math.round(dur));
    }
    const cs = e.connectStart;
    const ce = e.connectEnd;
    if (typeof cs !== "number" || typeof ce !== "number") return;
    if (!(ce > cs)) return;
    const setup = ce - cs;
    if (!Number.isFinite(setup) || setup <= 0 || setup > MAX_SETUP_MS) return;
    pushDuration(
      ownBackendSetups,
      MAX_SETUP_SAMPLES,
      Math.round(setup * 10) / 10,
    );
  } catch {
    // observation must never break the page
  }
}

/** Label-free distance aggregate: the app's own backend, and nothing else. */
export function getDependencyDistanceStats(): DependencyDistanceStatsLike {
  const kinds =
    ownBackendSetups.length > 0
      ? [
          {
            kind: DEP_KIND_OWN_BACKEND,
            connections: ownBackendSetups.length,
            setupMs: percentileOf(ownBackendSetups, 0.5),
          },
        ]
      : [];
  return {
    kinds,
    newConnections: ownBackendSetups.length,
    // A typical call to this app's own backend, as felt on the page.
    typicalRequestMs:
      ownBackendDurations.length > 0
        ? percentileOf(ownBackendDurations, 0.5)
        : null,
    // A browser has no platform region to declare — honestly unknown.
    hostArea: 0,
    ownBackendOnly: 1,
  };
}

/** @internal test hook — reset all network state. */
export function _resetNetworkSamplerForTests(): void {
  durations = [];
  completedCount = 0;
  failedCount = 0;
  abortedCount = 0;
  timedOutCount = 0;
  worstMs = 0;
  watching = false;
  nextCallId = 1;
  inFlight = new Map<number, CallHandle>();
  groups = new Map<string, GroupAgg>();
  overflowNames = new Set<string>();
  settled = new Set<number>();
  nowFn = () => Date.now();
  ownBackendSetups = [];
  ownBackendDurations = [];
}
