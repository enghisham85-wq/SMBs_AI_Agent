/** Unhandled-failure + browser-report trackers — the input for two additive
 *  web axes:
 *
 *   - unhandledErrors — uncaught `error` events + `unhandledrejection` events
 *     per hour. This is the browser's LOUD failure surface, DISTINCT from the
 *     console-error-based `swallowedErrors` axis (a survived, logged near-miss).
 *   - reportPressure — deprecation + intervention reports per hour, via the
 *     `ReportingObserver` API (report TYPE counts only).
 *
 * GUEST SAFETY (this runs inside a customer's page):
 *   - both trackers use ADDITIVE `addEventListener` / a fresh `ReportingObserver`
 *     — they NEVER replace `window.onerror`, `window.onunhandledrejection`, or
 *     any host handler, and never call `preventDefault`, so what the host sees
 *     is unchanged and every other listener still fires,
 *   - every callback is guarded and never throws into the host,
 *   - listeners are removed on uninstall,
 *   - installation is idempotent.
 *
 * PRIVACY: COUNTS + TIMESTAMPS ONLY. The error message, stack, filename,
 * rejection reason, report body, and report URL are NEVER read or stored.
 */

import { earnedPerHour, windowMinOf } from "./rateHonesty";

const WINDOW_MS = 60 * 60_000; // trailing 60-minute window
const RING_CAP = 500;

/* ─── Unhandled failures (error + unhandledrejection) ────────────────────── */

let unhandledInstalled = false;
let unhandledStartedAt = 0;
let errorTs: number[] = [];
let rejectionTs: number[] = [];
let errorListener: (() => void) | null = null;
let rejectionListener: (() => void) | null = null;
let rejectionHandledListener: (() => void) | null = null;
let lateHandledTs: number[] = [];
let lateHandlingObservable = false;

function nowMs(): number {
  return Date.now();
}

function pushTs(ring: number[]): void {
  try {
    ring.push(nowMs());
    if (ring.length > RING_CAP) ring.splice(0, ring.length - RING_CAP);
  } catch {
    // never break the host's event dispatch
  }
}

/** Install ADDITIVE `error` + `unhandledrejection` listeners. Never replaces a
 *  host handler; never calls preventDefault. Idempotent. Returns true when the
 *  listeners attached (or were already installed); false when impossible. */
export function installUnhandledTracking(): boolean {
  if (unhandledInstalled) return true;
  try {
    if (
      typeof window === "undefined" ||
      typeof window.addEventListener !== "function"
    ) {
      return false;
    }
    errorListener = (): void => pushTs(errorTs);
    rejectionListener = (): void => pushTs(rejectionTs);
    rejectionHandledListener = (): void => pushTs(lateHandledTs);
    // Passive, non-capturing: we only observe, never intercept.
    window.addEventListener("error", errorListener);
    window.addEventListener("unhandledrejection", rejectionListener);
    lateHandlingObservable = "onrejectionhandled" in window;
    if (lateHandlingObservable) {
      window.addEventListener("rejectionhandled", rejectionHandledListener);
    }
    unhandledInstalled = true;
    unhandledStartedAt = nowMs();
    errorTs = [];
    rejectionTs = [];
    lateHandledTs = [];
    return true;
  } catch {
    uninstallUnhandledTracking();
    return false;
  }
}

export function uninstallUnhandledTracking(): void {
  try {
    if (
      typeof window !== "undefined" &&
      typeof window.removeEventListener === "function"
    ) {
      if (errorListener) window.removeEventListener("error", errorListener);
      if (rejectionListener)
        window.removeEventListener("unhandledrejection", rejectionListener);
      if (rejectionHandledListener)
        window.removeEventListener("rejectionhandled", rejectionHandledListener);
    }
  } catch {
    // ignore
  }
  errorListener = null;
  rejectionListener = null;
  rejectionHandledListener = null;
  errorTs = [];
  rejectionTs = [];
  lateHandledTs = [];
  lateHandlingObservable = false;
  unhandledStartedAt = 0;
  unhandledInstalled = false;
}

export interface UnhandledStats {
  errorCount: number;
  rejectionCount: number;
  /** Null until the window has EARNED the projection — the earned-rate
   *  contract (rateHonesty.ts). The counts stand either way. */
  perHour: number | null;
  rejectionPerHour: number | null;
  windowMin: number;
  installed: boolean;
  lateHandled: number | null;
}

/** Trailing-window counts for uncaught errors + unhandled rejections. Returns
 *  installed=false when the listeners never attached, so the axis stays absent
 *  (never a fabricated 0). */
export function readUnhandled(): UnhandledStats {
  if (!unhandledInstalled) {
    return {
      errorCount: 0,
      rejectionCount: 0,
      perHour: null,
      rejectionPerHour: null,
      windowMin: 0,
      installed: false,
      lateHandled: null,
    };
  }
  const now = nowMs();
  const windowMs = Math.min(now - unhandledStartedAt, WINDOW_MS);
  const cutoff = now - windowMs;
  const errs = errorTs.reduce((a, t) => (t >= cutoff ? a + 1 : a), 0);
  const rejs = rejectionTs.reduce((a, t) => (t >= cutoff ? a + 1 : a), 0);
  const late = lateHandledTs.reduce((a, t) => (t >= cutoff ? a + 1 : a), 0);
  const total = errs + rejs;
  // The earned-rate contract (rateHonesty.ts): null until the window earns it.
  const perHour = earnedPerHour(total, windowMs);
  return {
    errorCount: errs,
    rejectionCount: rejs,
    perHour,
    rejectionPerHour: earnedPerHour(rejs, windowMs),
    windowMin: windowMinOf(windowMs),
    installed: true,
    lateHandled: lateHandlingObservable ? late : null,
  };
}

/** @internal test hook — seed the unhandled rings + window deterministically. */
export function _setUnhandledForTests(opts: {
  windowElapsedMs: number;
  errorAgoMs: number[];
  rejectionAgoMs: number[];
  lateHandledAgoMs?: number[] | null;
}): void {
  unhandledInstalled = true;
  const now = nowMs();
  unhandledStartedAt = now - opts.windowElapsedMs;
  errorTs = opts.errorAgoMs.map((a) => now - a);
  rejectionTs = opts.rejectionAgoMs.map((a) => now - a);
  lateHandlingObservable = opts.lateHandledAgoMs !== null;
  lateHandledTs = (opts.lateHandledAgoMs ?? []).map((a) => now - a);
}

/* ─── Report pressure (deprecation + intervention) ───────────────────────── */

let reportInstalled = false;
let reportStartedAt = 0;
let deprecationTs: number[] = [];
let interventionTs: number[] = [];
let reportObserver: { disconnect?: () => void } | null = null;

/** Install a fresh `ReportingObserver` for deprecation + intervention reports.
 *  A brand-new observer — it never touches any host observer. Idempotent.
 *  Returns true when it attached; false when the API is unsupported. */
export function installReportTracking(): boolean {
  if (reportInstalled) return true;
  try {
    const g = globalThis as unknown as {
      ReportingObserver?: new (
        cb: (reports: Array<{ type?: string }>) => void,
        opts?: { types?: string[]; buffered?: boolean },
      ) => { observe: () => void; disconnect?: () => void };
    };
    if (typeof g.ReportingObserver !== "function") return false;
    const obs = new g.ReportingObserver(
      (reports) => {
        try {
          for (const r of reports) {
            // TYPE counts only — the report body (which can carry a URL,
            // source line, or column) is NEVER read.
            if (r?.type === "deprecation") pushTs(deprecationTs);
            else if (r?.type === "intervention") pushTs(interventionTs);
          }
        } catch {
          // never break the browser's report dispatch
        }
      },
      { types: ["deprecation", "intervention"], buffered: true },
    );
    obs.observe();
    reportObserver = obs;
    reportInstalled = true;
    reportStartedAt = nowMs();
    deprecationTs = [];
    interventionTs = [];
    return true;
  } catch {
    uninstallReportTracking();
    return false;
  }
}

export function uninstallReportTracking(): void {
  try {
    if (reportObserver && typeof reportObserver.disconnect === "function") {
      reportObserver.disconnect();
    }
  } catch {
    // ignore
  }
  reportObserver = null;
  deprecationTs = [];
  interventionTs = [];
  reportStartedAt = 0;
  reportInstalled = false;
}

export interface ReportPressureStats {
  deprecationCount: number;
  interventionCount: number;
  /** Null until the window has EARNED the projection — the earned-rate
   *  contract (rateHonesty.ts). The counts stand either way. */
  perHour: number | null;
  windowMin: number;
  installed: boolean;
}

/** Trailing-window deprecation + intervention report counts. installed=false
 *  when the ReportingObserver API is unsupported, so the axis stays absent. */
export function readReportPressure(): ReportPressureStats {
  if (!reportInstalled) {
    return {
      deprecationCount: 0,
      interventionCount: 0,
      perHour: null,
      windowMin: 0,
      installed: false,
    };
  }
  const now = nowMs();
  const windowMs = Math.min(now - reportStartedAt, WINDOW_MS);
  const cutoff = now - windowMs;
  const deps = deprecationTs.reduce((a, t) => (t >= cutoff ? a + 1 : a), 0);
  const ints = interventionTs.reduce((a, t) => (t >= cutoff ? a + 1 : a), 0);
  const total = deps + ints;
  // The earned-rate contract (rateHonesty.ts): null until the window earns it.
  const perHour = earnedPerHour(total, windowMs);
  return {
    deprecationCount: deps,
    interventionCount: ints,
    perHour,
    windowMin: windowMinOf(windowMs),
    installed: true,
  };
}

/** @internal test hook — seed the report rings + window deterministically. */
export function _setReportPressureForTests(opts: {
  windowElapsedMs: number;
  deprecationAgoMs: number[];
  interventionAgoMs: number[];
}): void {
  reportInstalled = true;
  const now = nowMs();
  reportStartedAt = now - opts.windowElapsedMs;
  deprecationTs = opts.deprecationAgoMs.map((a) => now - a);
  interventionTs = opts.interventionAgoMs.map((a) => now - a);
}

/** @internal reset both trackers (tests). */
export function _resetWebReportsForTests(): void {
  uninstallUnhandledTracking();
  uninstallReportTracking();
}
