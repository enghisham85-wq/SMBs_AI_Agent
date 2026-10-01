/** Idle-efficiency tracker — the input for the Idle axis.
 *
 * "When the page is NOT being driven, does the main thread go quiet?" We treat
 * the page as IDLE once no user input has arrived for IDLE_AFTER_INPUT_MS, and
 * accumulate two quantities over idle stretches:
 *   - idleWallMs — wall time observed while idle,
 *   - idleBusyMs — main-thread busy time (long-task / LoAF blocked ms) that
 *     landed inside an idle stretch.
 * idleBusyPct = idleBusyMs / idleWallMs * 100. A quiescent app parks its timers
 * and animations, so almost no busy time lands while idle; a wasteful one keeps
 * the main thread busy with polling/animation even when untouched.
 *
 * Reuses EXISTING signals: the last-input timestamp is stamped from the same
 * passive listeners the kit already needs, and busy ms comes from the long-task
 * / LoAF observers vitals.ts already runs — no new sampling timer is added.
 * Idle wall time is folded forward lazily on each read/observation using a
 * monotonic clock, so nothing polls in the background.
 *
 * PRIVACY: durations + counts only — never an input target, key, coordinate,
 * or any customer value.
 */

import {
  computeIdleEfficiency,
  type IdleStatsLike,
  type IdleResult,
} from "./meterAxes";

/** Consider the page idle once this long has passed since the last input. */
export const IDLE_AFTER_INPUT_MS = 5_000;

let lastInputTs = 0;
let idleWallMs = 0;
let idleBusyMs = 0;
let idleLongTaskCount = 0;
// Anchor for folding idle wall time forward: the moment we last accounted time.
let lastAccountedTs = 0;
let started = false;

function nowMs(): number {
  try {
    if (
      typeof performance !== "undefined" &&
      typeof performance.now === "function"
    ) {
      return performance.now();
    }
  } catch {
    // fall through
  }
  return Date.now();
}

/** Fold wall time up to `ts` into idleWallMs when the page is currently idle,
 *  then advance the accounting anchor. Cheap + guarded. */
function accountWallTime(ts: number): void {
  if (!started) return;
  if (lastAccountedTs === 0) {
    lastAccountedTs = ts;
    return;
  }
  const delta = ts - lastAccountedTs;
  if (delta > 0) {
    const idleNow = ts - lastInputTs >= IDLE_AFTER_INPUT_MS;
    // Only the portion of `delta` that lies in the idle zone counts. If input
    // was recent, none of it does; if input is old, all of it does (the whole
    // delta is at least IDLE_AFTER_INPUT_MS past the last input).
    if (idleNow) idleWallMs += delta;
  }
  lastAccountedTs = ts;
}

/** Stamp a user input. Resets the idle clock so busy work right after input is
 *  never counted as idle waste. Guarded; called from passive input listeners. */
export function noteUserInput(): void {
  const ts = nowMs();
  accountWallTime(ts);
  lastInputTs = ts;
}

/** Record a main-thread busy block (long task / LoAF) of `ms`. Counts toward
 *  idleBusyMs only when it landed while the page was idle. Guarded. */
export function noteBusyBlock(ms: number): void {
  if (!started) return;
  if (!Number.isFinite(ms) || ms <= 0) return;
  const ts = nowMs();
  accountWallTime(ts);
  if (ts - lastInputTs >= IDLE_AFTER_INPUT_MS) {
    idleBusyMs += ms;
    idleLongTaskCount++;
  }
}

/** Begin idle accounting (called once from startWebVitals). Idempotent. */
export function startIdleTracking(): void {
  if (started) return;
  started = true;
  const ts = nowMs();
  // Treat page start as "just had input" so early boot work isn't idle waste.
  lastInputTs = ts;
  lastAccountedTs = ts;
}

/** Current idle stats (label-free). Folds wall time forward to now first. */
export function getIdleStats(): IdleStatsLike {
  accountWallTime(nowMs());
  return { idleBusyMs, idleWallMs, idleLongTaskCount };
}

/** Current Idle axis reading (pending until enough idle wall time observed). */
export function readIdle(): IdleResult {
  return computeIdleEfficiency(getIdleStats());
}

/** @internal test hook — seed idle stats deterministically. */
export function _setIdleStatsForTests(s: IdleStatsLike): void {
  started = true;
  idleBusyMs = s.idleBusyMs;
  idleWallMs = s.idleWallMs;
  idleLongTaskCount = s.idleLongTaskCount;
  // Freeze the accounting anchor at "now" so getIdleStats folds in ~0 extra.
  lastAccountedTs = nowMs();
  lastInputTs = lastAccountedTs; // not idle right now → no extra wall time
}

/** @internal test hook — reset all idle state. */
export function _resetIdleTrackerForTests(): void {
  started = false;
  lastInputTs = 0;
  idleWallMs = 0;
  idleBusyMs = 0;
  idleLongTaskCount = 0;
  lastAccountedTs = 0;
}
