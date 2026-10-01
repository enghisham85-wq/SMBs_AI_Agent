/** Outstanding-timer tracker — the input for the Timer Health axis.
 *
 * Wraps the host page's `window.setTimeout` / `setInterval` / `clearTimeout` /
 * `clearInterval` to count how many timer handles are currently OUTSTANDING
 * (created and not yet fired/cleared), then samples that count over time so the
 * axis can score its GROWTH TREND (a leak signal).
 *
 * GUEST SAFETY (this runs inside a customer's page):
 *   - the original functions are kept and ALWAYS called through unconditionally,
 *   - the wrapper never throws (all bookkeeping is guarded),
 *   - return values and `this` are preserved exactly,
 *   - the originals are restored on uninstall,
 *   - installation is idempotent (install-only-once).
 * If wrapping is impossible (no window, non-function timers), install() is a
 * no-op and the axis stays absent (readTimerHealth reports pending) — we never
 * guess.
 *
 * PRIVACY: counts + timestamps only. No timer callback, argument, URL, or any
 * customer value is ever read or stored.
 */

import {
  computeTimerHealth,
  TIMER_HEALTH_MIN_SAMPLES,
  TIMER_HEALTH_MIN_ELAPSED_MS,
  ASYNC_SLOW_CALLBACK_MS,
  computeAsyncSlowCallbacks,
  computeEventLoopLag,
  type AsyncSlowCallbacksResult,
  type EventLoopLagResult,
  type TimerHealthResult,
} from "./meterAxes";

type TimeoutId = ReturnType<typeof setTimeout>;
type IntervalId = ReturnType<typeof setInterval>;

let installed = false;
// When true, sampleTimerHealth() is a no-op so a test's seeded samples stay
// intact across a snapshot capture (which samples then reads).
let sampleSuppressedForTests = false;
let origSetTimeout: typeof window.setTimeout | null = null;
let origSetInterval: typeof window.setInterval | null = null;
let origClearTimeout: typeof window.clearTimeout | null = null;
let origClearInterval: typeof window.clearInterval | null = null;

// Outstanding one-shot timeouts (removed when they fire or are cleared) and
// outstanding intervals (removed only when cleared). Both are numeric-id sets
// so memory is bounded by the app's real live-timer count.
const liveTimeouts = new Set<TimeoutId>();
const liveIntervals = new Set<IntervalId>();

/** Fixed-size ring of (t, outstanding) samples for the growth-trend fit. */
const MAX_SAMPLES = 64;
export const EVENT_LOOP_TICK_MS = 2_000;
let sampleTimes: number[] = [];
let sampleCounts: number[] = [];
let lagSamples: number[] = [];
let slowCallbackCount = 0;
let trackingStartedAt = 0;

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

function outstanding(): number {
  return liveTimeouts.size + liveIntervals.size;
}

function runTimedCallback(
  handler: (...args: unknown[]) => unknown,
  receiver: unknown,
  args: unknown[],
): unknown {
  const startedAt = nowMs();
  try {
    return handler.apply(receiver, args);
  } finally {
    try {
      if (nowMs() - startedAt > ASYNC_SLOW_CALLBACK_MS) slowCallbackCount++;
    } catch {
      // timing must never change callback behavior
    }
  }
}

/** Install the timer wrappers once. Guest-safe + idempotent. Returns true when
 *  wrapping succeeded (or was already installed); false when it was impossible
 *  (the axis then stays absent). */
export function installTimerTracking(): boolean {
  if (installed) return true;
  try {
    if (typeof window === "undefined") return false;
    const w = window as Window &
      typeof globalThis & {
        setTimeout: typeof setTimeout;
        setInterval: typeof setInterval;
        clearTimeout: typeof clearTimeout;
        clearInterval: typeof clearInterval;
      };
    if (
      typeof w.setTimeout !== "function" ||
      typeof w.setInterval !== "function" ||
      typeof w.clearTimeout !== "function" ||
      typeof w.clearInterval !== "function"
    ) {
      return false;
    }

    origSetTimeout = w.setTimeout;
    origSetInterval = w.setInterval;
    origClearTimeout = w.clearTimeout;
    origClearInterval = w.clearInterval;

    const realSetTimeout = origSetTimeout;
    const realSetInterval = origSetInterval;
    const realClearTimeout = origClearTimeout;
    const realClearInterval = origClearInterval;

    // setTimeout: wrap the handler so we drop the id from the live set when it
    // fires (a fired one-shot is no longer outstanding), then track its id.
    w.setTimeout = function (
      this: unknown,
      handler: TimerHandler,
      timeout?: number,
      ...args: unknown[]
    ): TimeoutId {
      let id: TimeoutId;
      if (typeof handler === "function") {
        const wrapped = function (this: unknown, ...cbArgs: unknown[]): unknown {
          try {
            liveTimeouts.delete(id);
          } catch {
            // never let bookkeeping break the timer
          }
          return runTimedCallback(
            handler as (...a: unknown[]) => unknown,
            this,
            cbArgs,
          );
        };
        id = realSetTimeout.apply(this, [
          wrapped,
          timeout,
          ...args,
        ] as unknown as Parameters<typeof setTimeout>);
      } else {
        // String-source timers can't be un-hooked safely; still track the id.
        id = realSetTimeout.apply(this, [
          handler,
          timeout,
          ...args,
        ] as unknown as Parameters<typeof setTimeout>);
      }
      try {
        liveTimeouts.add(id);
      } catch {
        // ignore
      }
      return id;
    } as typeof setTimeout;

    w.setInterval = function (
      this: unknown,
      handler: TimerHandler,
      timeout?: number,
      ...args: unknown[]
    ): IntervalId {
      const wrapped =
        typeof handler === "function"
          ? function (this: unknown, ...cbArgs: unknown[]): unknown {
              return runTimedCallback(
                handler as (...a: unknown[]) => unknown,
                this,
                cbArgs,
              );
            }
          : handler;
      const id = realSetInterval.apply(this, [
        wrapped,
        timeout,
        ...args,
      ] as unknown as Parameters<typeof setInterval>);
      try {
        liveIntervals.add(id);
      } catch {
        // ignore
      }
      return id;
    } as typeof setInterval;

    w.clearTimeout = function (this: unknown, id?: TimeoutId): void {
      try {
        if (id !== undefined) liveTimeouts.delete(id);
      } catch {
        // ignore
      }
      return realClearTimeout.apply(this, [id] as unknown as Parameters<
        typeof clearTimeout
      >);
    } as typeof clearTimeout;

    w.clearInterval = function (this: unknown, id?: IntervalId): void {
      try {
        if (id !== undefined) liveIntervals.delete(id);
      } catch {
        // ignore
      }
      return realClearInterval.apply(this, [id] as unknown as Parameters<
        typeof clearInterval
      >);
    } as typeof clearInterval;

    installed = true;
    // Seed the first sample so the trend has an anchor from t0.
    sampleTimes = [nowMs()];
    sampleCounts = [outstanding()];
    lagSamples = [];
    slowCallbackCount = 0;
    trackingStartedAt = sampleTimes[0];
    return true;
  } catch {
    // Wrapping failed — restore whatever we swapped and stay absent.
    uninstallTimerTracking();
    return false;
  }
}

/** Restore the original timer functions and clear state. Safe to call
 *  repeatedly; called when telemetry is disabled/disconnected or in tests. */
export function uninstallTimerTracking(): void {
  try {
    if (typeof window !== "undefined") {
      const w = window as unknown as Record<string, unknown>;
      if (origSetTimeout) w.setTimeout = origSetTimeout;
      if (origSetInterval) w.setInterval = origSetInterval;
      if (origClearTimeout) w.clearTimeout = origClearTimeout;
      if (origClearInterval) w.clearInterval = origClearInterval;
    }
  } catch {
    // ignore
  }
  origSetTimeout = null;
  origSetInterval = null;
  origClearTimeout = null;
  origClearInterval = null;
  liveTimeouts.clear();
  liveIntervals.clear();
  sampleTimes = [];
  sampleCounts = [];
  lagSamples = [];
  slowCallbackCount = 0;
  trackingStartedAt = 0;
  installed = false;
  sampleSuppressedForTests = false;
}

/** Record one sample of the current outstanding-timer count. Cheap; meant to be
 *  driven off an EXISTING kit cadence (the bubble refresh / snapshot flush),
 *  never a new timer. Guarded; never throws. */
export function sampleTimerHealth(): void {
  if (!installed || sampleSuppressedForTests) return;
  try {
    const now = nowMs();
    const previous = sampleTimes[sampleTimes.length - 1];
    if (Number.isFinite(previous)) {
      lagSamples.push(Math.max(0, now - previous - EVENT_LOOP_TICK_MS));
      if (lagSamples.length > MAX_SAMPLES) {
        lagSamples.splice(0, lagSamples.length - MAX_SAMPLES);
      }
    }
    sampleTimes.push(now);
    sampleCounts.push(outstanding());
    if (sampleTimes.length > MAX_SAMPLES) {
      sampleTimes.splice(0, sampleTimes.length - MAX_SAMPLES);
      sampleCounts.splice(0, sampleCounts.length - MAX_SAMPLES);
    }
  } catch {
    // ignore
  }
}

/** Least-squares growth slope (handles per ms) of the sampled outstanding
 *  count. 0 when fewer than 2 samples or a degenerate time span. */
function growthPerMs(): number {
  const n = sampleTimes.length;
  if (n < 2) return 0;
  const t0 = sampleTimes[0];
  let sx = 0;
  let sy = 0;
  let sxx = 0;
  let sxy = 0;
  for (let i = 0; i < n; i++) {
    const x = sampleTimes[i] - t0;
    const y = sampleCounts[i];
    sx += x;
    sy += y;
    sxx += x * x;
    sxy += x * y;
  }
  const denom = n * sxx - sx * sx;
  if (denom === 0) return 0;
  return (n * sxy - sx * sy) / denom;
}

/** Current Timer Health reading. Pending (null score) until tracking is
 *  installed AND enough samples span enough time — never a fabricated reading.
 *  When tracking was never installed (wrapping impossible) it stays pending, so
 *  the snapshot omits the axis. */
export function readTimerHealth(): TimerHealthResult {
  if (!installed) {
    return { score: null, rating: "pending", timers: 0, growthPerMin: null };
  }
  const elapsedMs =
    sampleTimes.length >= 2
      ? sampleTimes[sampleTimes.length - 1] - sampleTimes[0]
      : 0;
  const growthPerMin = growthPerMs() * 60_000;
  return computeTimerHealth({
    outstanding: outstanding(),
    growthPerMin,
    sampleCount: sampleTimes.length,
    elapsedMs,
  });
}

export function readAsyncSlowCallbacks(): AsyncSlowCallbacksResult {
  return computeAsyncSlowCallbacks({
    installed,
    count: slowCallbackCount,
    windowMs: trackingStartedAt > 0 ? Math.max(0, nowMs() - trackingStartedAt) : 0,
  });
}

export function readEventLoopLag(): EventLoopLagResult {
  const windowMs =
    sampleTimes.length >= 2
      ? sampleTimes[sampleTimes.length - 1] - sampleTimes[0]
      : 0;
  return computeEventLoopLag({ samples: lagSamples, windowMs });
}

/** True once the wrappers are installed (guards double-install at the caller). */
export function isTimerTrackingInstalled(): boolean {
  return installed;
}

/** @internal test hook — drive the sampler deterministically without live
 *  timers or wall-clock. Marks tracking "installed" so readTimerHealth scores.
 *  Each entry is a (relative-ms, outstanding-count) sample. */
export function _setTimerSamplesForTests(
  samples: Array<{ tMs: number; count: number }>,
): void {
  installed = true;
  sampleSuppressedForTests = true;
  sampleTimes = samples.map((s) => s.tMs);
  sampleCounts = samples.map((s) => s.count);
}

export function _setSchedulingSamplesForTests(opts: {
  lagSamples: number[];
  windowMs: number;
  slowCallbackCount: number;
}): void {
  installed = true;
  sampleSuppressedForTests = true;
  lagSamples = opts.lagSamples.slice(-MAX_SAMPLES);
  const end = nowMs();
  sampleTimes = [end - opts.windowMs, end];
  sampleCounts = [0, 0];
  trackingStartedAt = end - opts.windowMs;
  slowCallbackCount = opts.slowCallbackCount;
}

/** @internal test hook — count of currently-outstanding tracked handles. */
export function _outstandingForTests(): number {
  return outstanding();
}
