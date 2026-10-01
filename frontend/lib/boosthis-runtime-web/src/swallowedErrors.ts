/** Swallowed-errors tracker — the input for the Near-Miss Rate axis.
 *
 * Web parity for the Python kit's `swallowedErrors` axis
 * (`lib/boosthis-py/boosthis/extra_meters.py`): errors the HOST app LOGGED at
 * error level but did NOT crash on — a near miss. Every band, gate, and
 * rounding rule mirrors that canonical source so the same number means the
 * same thing in every runtime.
 *
 * OBSERVATION POINT (browser): the only honest analogue of Python's ERROR-level
 * log record is `console.error` — the browser's error-level log call. We CHAIN
 * it (never replace): the host's original `console.error` is captured and
 * ALWAYS called first, its return value is passed straight back, and we only
 * record a TIMESTAMP on the side. We never add or remove a host log handler,
 * never change formatting or output, and never read the arguments. Window
 * `error` / `unhandledrejection` events are NOT counted here — those are
 * crashes and the crash reporter already owns them.
 *
 * GUEST SAFETY (this runs inside a customer's page):
 *   - the original `console.error` is kept and ALWAYS called through first,
 *   - the wrapper never throws (all bookkeeping is guarded),
 *   - the return value and `this` are preserved exactly,
 *   - the original is restored on uninstall,
 *   - installation is idempotent (install-only-once).
 * If wrapping is impossible (no console, non-function `console.error`),
 * install() is a no-op and the axis stays absent (the snapshot omits it) — we
 * never guess.
 *
 * We EXCLUDE Boosthis's own `console.error` calls so we never count ourselves:
 * every call the kit routes through the captured original is invisible to the
 * counter (only the wrapped, host-facing `console.error` records a timestamp).
 *
 * PRIVACY: TIMESTAMPS ONLY. The message, any argument, logger name, or stack is
 * never read or stored — the ring holds at most SWALLOWED_RING_CAP numbers.
 */

import { computeSwallowedErrors, type SwallowedErrorsResult } from "./meterAxes";

/* ─── Gates + bands (copied from Python, unchanged) ──────────────────────── */

/** good band: 1 logged-error/hour. */
export const SWALLOWED_GOOD_PER_HOUR = 1.0;
/** poor band: 60 logged-errors/hour. */
export const SWALLOWED_POOR_PER_HOUR = 60.0;
/** No clean bill until at least 5 minutes have been watched. */
export const SWALLOWED_MIN_WINDOW_MS = 5 * 60_000;
/** Trailing window capped at 60 minutes. */
export const SWALLOWED_WINDOW_MS = 60 * 60_000;
/** Ring holds at most this many timestamps (timestamps ONLY). */
export const SWALLOWED_RING_CAP = 400;

let installed = false;
let armed = false;
let startedAt = 0;
let origConsoleError: ((...args: unknown[]) => unknown) | null = null;
let ourConsoleError: ((...args: unknown[]) => unknown) | null = null;

/** Fixed-size ring of error-record timestamps (ms). TIMESTAMPS ONLY. */
let swallowedTs: number[] = [];

/** Args observers piggybacking the SAME wrapper (e.g. leakWatch's bounded
 *  scan). We NEVER add a second console.error wrapper — additional monitors
 *  subscribe here so the kit's own logging exclusion + reentrancy are reused.
 *  Observers get the host-facing args ONLY (never our own passthrough calls),
 *  are called on the side AFTER the host's original, and must never throw. */
let argsObservers: Array<(args: unknown[]) => void> = [];

function nowMs(): number {
  return Date.now();
}

/** Subscribe to host-facing `console.error` arg lists observed by the SAME
 *  chained wrapper (no second hook). Returns an unsubscribe function; a no-op
 *  unsubscribe when the chain is not installed. The observer is invoked on the
 *  side after the host original, wrapped so it can never throw into the host. */
export function onConsoleErrorArgs(
  observer: (args: unknown[]) => void,
): () => void {
  argsObservers.push(observer);
  return () => {
    argsObservers = argsObservers.filter((o) => o !== observer);
  };
}

/** Fan an observed host-facing arg list out to the side observers. Guarded so a
 *  misbehaving observer can never break the host's logging call. */
function notifyArgsObservers(args: unknown[]): void {
  try {
    if (!armed) return;
    for (const o of argsObservers) {
      try {
        o(args);
      } catch {
        // one observer failing must never affect the host or other observers
      }
    }
  } catch {
    // never let bookkeeping break the host's logging call
  }
}

/** Record one host `console.error` call — a TIMESTAMP only. Never the message,
 *  argument, logger name, or stack. Never raises into the host's call. */
function noteError(): void {
  try {
    if (!armed) return;
    swallowedTs.push(nowMs());
    if (swallowedTs.length > SWALLOWED_RING_CAP) {
      swallowedTs.splice(0, swallowedTs.length - SWALLOWED_RING_CAP);
    }
  } catch {
    // never let bookkeeping break the host's logging call
  }
}

/** Install the `console.error` chain once. Guest-safe + idempotent. Returns
 *  true when chaining succeeded (or was already installed); false when it was
 *  impossible (the axis then stays absent). */
export function installSwallowedErrors(): boolean {
  if (installed) return true;
  try {
    const c =
      typeof console !== "undefined"
        ? (console as unknown as Record<string, unknown>)
        : undefined;
    if (!c || typeof c.error !== "function") {
      return false;
    }

    const orig = c.error as (...args: unknown[]) => unknown;
    const realConsoleError = orig;

    // Chain, never replace: call the host's original FIRST, observe on the
    // side (timestamp only), then return exactly what the host expected.
    const wrapped = function (this: unknown, ...args: unknown[]): unknown {
      const result = realConsoleError.apply(this, args);
      noteError();
      // Fan the SAME observed args out to side observers (e.g. leakWatch's
      // bounded scan) — never a second wrapper. Our own passthrough calls go
      // through the captured original and so are excluded here too.
      notifyArgsObservers(args);
      return result;
    };

    c.error = wrapped;
    origConsoleError = orig;
    ourConsoleError = wrapped;
    installed = true;
    armed = true;
    startedAt = nowMs();
    swallowedTs = [];
    return true;
  } catch {
    // Chaining failed — restore whatever we swapped and stay absent.
    uninstallSwallowedErrors();
    return false;
  }
}

/** Restore the host's `console.error` and clear state. If another library
 *  chained ON TOP of ours we leave the chain alone — unhooking would drop THEIR
 *  wrapper too; ours is already inert because `armed` is false. Safe to call
 *  repeatedly. */
export function uninstallSwallowedErrors(): void {
  try {
    if (
      typeof console !== "undefined" &&
      origConsoleError !== null &&
      ourConsoleError !== null
    ) {
      const c = console as unknown as Record<string, unknown>;
      if (c.error === ourConsoleError) {
        c.error = origConsoleError;
      }
    }
  } catch {
    // ignore
  }
  origConsoleError = null;
  ourConsoleError = null;
  swallowedTs = [];
  startedAt = 0;
  installed = false;
  armed = false;
}

/** Call the HOST's original `console.error` (never our wrapper), so Boosthis's
 *  own error logging is EXCLUDED from the near-miss counter. Falls back to the
 *  live `console.error` when the chain was never installed. Never throws. */
export function ourConsoleErrorPassthrough(...args: unknown[]): void {
  try {
    if (origConsoleError) {
      origConsoleError.apply(console, args);
      return;
    }
    if (typeof console !== "undefined" && typeof console.error === "function") {
      // Chain not installed — nothing is counting, so a direct call is safe.
      (console.error as (...a: unknown[]) => unknown).apply(console, args);
    }
  } catch {
    // logging must never break the caller
  }
}

/** Current Near-Miss reading. Pending (null score) until the chain is armed AND
 *  the trailing window reaches the 5-minute minimum — never a fabricated
 *  reading. When chaining was never installed (impossible) it stays pending, so
 *  the snapshot omits the axis. */
export function readSwallowedErrors(): SwallowedErrorsResult {
  if (!armed) {
    return { score: null, rating: "pending", count: 0, perHour: null, windowMin: null };
  }
  const now = nowMs();
  const windowMs = Math.min(now - startedAt, SWALLOWED_WINDOW_MS);
  const cutoff = now - windowMs;
  const count = swallowedTs.reduce((acc, t) => (t >= cutoff ? acc + 1 : acc), 0);
  return computeSwallowedErrors({ count, windowMs });
}

/** True once the chain is installed (guards double-install at the caller). */
export function isSwallowedErrorsInstalled(): boolean {
  return installed;
}

/** @internal test hook — seed the ring + window deterministically without a
 *  live `console.error` or wall-clock. Marks the axis "armed" so
 *  readSwallowedErrors scores. `tsAgoMs` entries are ms-before-now. */
export function _setSwallowedErrorsForTests(opts: {
  windowElapsedMs: number;
  tsAgoMs: number[];
}): void {
  armed = true;
  installed = true;
  const now = nowMs();
  startedAt = now - opts.windowElapsedMs;
  swallowedTs = opts.tsAgoMs.map((ago) => now - ago);
}

/** @internal test hook — number of timestamps currently held in the ring. */
export function _swallowedRingSizeForTests(): number {
  return swallowedTs.length;
}
