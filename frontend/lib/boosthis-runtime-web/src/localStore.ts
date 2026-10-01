/** Boosthis: the PAGE'S OWN local store — speed + failure share (browser).
 *
 * Two ADDITIVE, display-only readings about the store a browser app actually
 * waits on when there is no database in the picture: saved sessions, cached
 * content, offline data held in **Web Storage** (`localStorage` /
 * `sessionStorage`).
 *
 *   • storageLatency  — p75 duration of wrapped get/set/remove operations. No
 *     key, no value, no size, no store name is ever recorded — only how LONG
 *     each call took.
 *   • storageFailures — the share of those same operations that THREW,
 *     separate from their timing. A store that answers in a microsecond and
 *     refuses every write reads as perfectly healthy if only speed is shown
 *     (a full quota, a cleared-site-data policy, a frame that lost access
 *     mid-session all look exactly like that).
 *
 * WHAT IS AND IS NOT WATCHED. Web Storage only. IndexedDB is deliberately not
 * observed, and that is a recorded decision with its reason —
 * docs/decisions/browser-local-store-observation.md — not a silent blank. The
 * shared rules these readings obey across every kit that takes them are
 * written down once in docs/local-store-reading-contract.md and read back out
 * of this file by scripts/src/__tests__/local-store-reading-contract.test.ts.
 * Change the contract first; a number changed only here is reported as drift,
 * by name.
 *
 * OPT-IN. Wrapping a page's own data path is intrusive enough to be asked for,
 * so both readings are behind `BOOSTHIS_STORAGE_METER` (the shared flag
 * reader, so the browser spellings `globalThis.BOOSTHIS_STORAGE_METER` /
 * `globalThis.__BOOSTHIS_STORAGE_METER__` work in a real page). Off ⇒ nothing
 * is wrapped and both axes are ABSENT.
 *
 * WRAP, NEVER REPLACE OR SWALLOW: the wrappers sit on `Storage.prototype`
 * (which is what both `localStorage` and `sessionStorage` share), call the
 * page's original method with the page's own arguments and `this`, and return
 * exactly what it returned. Web Storage is SYNCHRONOUS, so a failure is a
 * THROW: it is timed, counted, and RE-THROWN unchanged — the page's own
 * `try/catch` around a quota-exceeded write still sees the error it expected.
 *
 * DEGRADES WHERE STORAGE IS WALLED OFF: in a sandboxed frame or with site data
 * blocked, merely touching `window.localStorage` throws. Install probes that
 * ONCE, guarded, and on a throw wires nothing and records an honest
 * "blocked here" verdict rather than dying inside the host page.
 *
 * HONESTY / INVARIANTS:
 *   • NEVER feeds the composite Speed score. Display-only.
 *   • OPT-IN OFF, or no Web Storage in this runtime ⇒ axes ABSENT: the readers
 *     return null so the caller omits the key — never a fabricated 0.
 *   • BLOCKED HERE (access throws) ⇒ `{ measurable: 0, reasonCode }`, which is
 *     an answer, not a blank.
 *   • WARM-UP: below the minimum operation count the readers return null, so a
 *     panel never shows one of the pair warming and the other missing.
 *   • Numbers-only wire shapes (counts + durations only).
 */

import { linearScore, ratingFor } from "./thresholds";
import type { AxisRating } from "./meterAxes";
import { readFlagValue } from "./runtimeFlags";

const TRUE_VALUES = new Set(["1", "true", "yes", "on"]);

/** The explicit opt-in. Wrapping the page's own data path is asked for. */
export const STORAGE_METER_OPT_IN = "BOOSTHIS_STORAGE_METER";

function optInEnabled(): boolean {
  try {
    const v = readFlagValue(STORAGE_METER_OPT_IN);
    if (typeof v === "boolean") return v;
    if (typeof v === "string" && v.length > 0) {
      return TRUE_VALUES.has(v.toLowerCase());
    }
  } catch {
    // an unreadable flag is an un-set flag — never disturb the host page
  }
  return false;
}

/** Latency bands (p75 ms) for the `host-sync-web-storage` observation point.
 *  Web Storage is SYNCHRONOUS, so it is paid in frames rather than in the
 *  20/200 ms an async store gets: ≤2 ms is free, ≥16 ms is a whole dropped
 *  frame at 60 Hz, every single time. */
export const WEB_STORAGE_LATENCY_THRESHOLDS = { good: 2, poor: 16 } as const;

/** Failure-share bands (throws / operations). ≤1% is noise (100); ≥10% is a
 *  chronically failing store (0). The SAME band every kit uses — a share needs
 *  no domain correction. */
export const WEB_STORAGE_FAILURE_THRESHOLDS = { good: 0.01, poor: 0.1 } as const;

/** Minimum operations observed before either axis reports. */
const STORAGE_MIN_OPS = 5;
/** Cap on retained latency samples (bounded memory). */
const STORAGE_RING_CAP = 400;

/** "The app runs somewhere that blocks the reading" — a sandboxed frame or a
 *  site-data policy that makes touching Web Storage throw. Mirrors
 *  AXIS_NOT_AVAILABLE_REASON.BLOCKED_BY_ENVIRONMENT on the server. */
export const REASON_BLOCKED_BY_ENVIRONMENT = 8;

export interface WebStorageLatencyResult {
  score: number | null;
  rating: AxisRating;
  /** p75 operation duration (ms), or null while warming. */
  p75Ms: number | null;
  /** Worst single operation duration (ms) this session, 0 if none. */
  worstMs: number;
  /** Operations observed (shown even while warming). */
  opCount: number;
  /** This reading belongs to this page's own storage area. */
  scopeCode: 5;
  /** Present ONLY on the blocked-here verdict. */
  measurable?: number;
  /** Present ONLY on the blocked-here verdict. */
  reasonCode?: number;
}

export interface WebStorageFailureResult {
  score: number | null;
  rating: AxisRating;
  /** Throw rate as a percentage (0–100), or null while warming. */
  failPct: number | null;
  /** Throws observed this session. */
  failCount: number;
  /** Operations observed (shown even while warming). */
  opCount: number;
  /** This reading belongs to this page's own storage area. */
  scopeCode: 5;
  /** Present ONLY on the blocked-here verdict. */
  measurable?: number;
  /** Present ONLY on the blocked-here verdict. */
  reasonCode?: number;
}

/* ─── The wrapped surface ───────────────────────────────────────────────── */

type StorageMethod = (...args: string[]) => unknown;

/** Only the three operations a page's own data access actually goes through.
 *  `clear()` and `key()` are left alone: `clear` is a teardown call whose
 *  duration says nothing about how the app's screens feel, and wrapping fewer
 *  methods is fewer places to be wrong inside somebody else's page. */
const WRAPPED_METHODS = ["getItem", "setItem", "removeItem"] as const;

let installed = false;
let blockedHere = false;
let proto: Record<string, unknown> | null = null;
/** Originals captured for restore on teardown. */
const originals = new Map<string, StorageMethod>();
const durations: number[] = [];
let worstMs = 0;
let opCount = 0;
let failCount = 0;

function monoNow(): number {
  try {
    const p = (globalThis as unknown as { performance?: { now?: () => number } })
      .performance;
    if (typeof p?.now === "function") return p.now();
  } catch {
    /* best-effort */
  }
  return Date.now();
}

/** Record one completed op outcome. Never throws — a slip in the kit's own
 *  bookkeeping may not reach the page's storage call. */
function observe(durMs: number, ok: boolean): void {
  try {
    opCount += 1;
    if (!ok) failCount += 1;
    if (durMs >= 0 && durMs < 60_000) {
      durations.push(durMs);
      if (durations.length > STORAGE_RING_CAP) {
        durations.splice(0, durations.length - STORAGE_RING_CAP);
      }
      if (durMs > worstMs) worstMs = durMs;
    }
  } catch {
    /* best-effort */
  }
}

/** Can this page reach Web Storage at all? Touching `localStorage` THROWS in a
 *  sandboxed frame and where site data is blocked, so the probe is guarded and
 *  its answer is remembered: reachable / walled off / not in this runtime. */
function webStorageReachable(): boolean | null {
  try {
    const g = globalThis as unknown as { localStorage?: unknown };
    if (typeof g.localStorage === "undefined") return null; // not a browser
    // Reading the property is itself the access that throws when blocked.
    void (g.localStorage as { length?: number }).length;
    return true;
  } catch {
    return false; // present, but this page may not use it
  }
}

/** Wrap a caller-supplied prototype object's get/set/remove in place. This is
 *  the exact wrapping code path; extracted so the real install and the
 *  deterministic test seam drive one identical observer chain. */
function wrapPrototype(p: Record<string, unknown>): void {
  if (!p) return;
  proto = p;
  for (const name of WRAPPED_METHODS) {
    const orig = p[name];
    if (typeof orig !== "function") continue;
    originals.set(name, orig as StorageMethod);
    const boundOrig = orig as StorageMethod;
    const wrapper = function (this: unknown, ...args: string[]): unknown {
      const t0 = monoNow();
      try {
        // Call the page's original method and return its EXACT result.
        const out = (boundOrig as (this: unknown, ...a: string[]) => unknown)
          .apply(this, args);
        try {
          observe(monoNow() - t0, true);
        } catch {
          /* observation must never affect the host's call */
        }
        return out;
      } catch (err) {
        try {
          observe(monoNow() - t0, false);
        } catch {
          /* observation must never affect the host's call */
        }
        // RE-THROW UNCHANGED. Web Storage reports a full quota, a blocked
        // origin and a serialization failure by throwing; the page's own
        // handler is entitled to the error it was going to get.
        throw err;
      }
    };
    p[name] = wrapper;
  }
  installed = originals.size > 0;
  if (!installed) proto = null;
}

/**
 * Wrap Web Storage's get/set/remove to observe duration + outcome. Idempotent,
 * best-effort, NEVER throws. Only wires up when `BOOSTHIS_STORAGE_METER` is set
 * AND the page can actually reach Web Storage. On any failure tracking stays
 * OFF and the axes read absent (or blocked, which is an answer). Call from
 * telemetry start.
 */
export function installLocalStoreTracking(): boolean {
  if (installed) return true;
  if (!optInEnabled()) return false; // opt-in only; ABSENT when off.
  try {
    const reach = webStorageReachable();
    if (reach === null) return false; // no Web Storage in this runtime — absent
    if (reach === false) {
      // A sandboxed frame or a site-data policy. The opt-in was asked for, so
      // saying nothing would read as "we never looked": record the verdict.
      blockedHere = true;
      return false;
    }
    const ctor = (globalThis as unknown as { Storage?: { prototype?: unknown } })
      .Storage;
    const p = ctor?.prototype as Record<string, unknown> | undefined;
    if (!p || typeof p.getItem !== "function") return false;
    wrapPrototype(p);
    return installed;
  } catch {
    installed = false;
    proto = null;
    originals.clear();
    return false;
  }
}

function p75(arr: number[]): number {
  if (arr.length === 0) return 0;
  const sorted = [...arr].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * 0.75))];
}

/** storageLatency — p75 operation duration over the page's OWN Web Storage.
 *  Returns null (ABSENT) when not wired (opt-in off / no Web Storage here) or
 *  too few operations observed; the blocked-here verdict when the page may not
 *  touch storage at all. */
export function readWebStorageLatency(): WebStorageLatencyResult | null {
  try {
    if (!installed) {
      if (!blockedHere) return null;
      return {
        score: null,
        rating: "pending",
        p75Ms: null,
        worstMs: 0,
        opCount: 0,
        scopeCode: 5,
        measurable: 0,
        reasonCode: REASON_BLOCKED_BY_ENVIRONMENT,
      };
    }
    if (opCount < STORAGE_MIN_OPS) return null; // warming
    const v = p75(durations);
    const score = linearScore(
      v,
      WEB_STORAGE_LATENCY_THRESHOLDS.good,
      WEB_STORAGE_LATENCY_THRESHOLDS.poor,
    );
    return {
      score,
      rating: ratingFor(score),
      p75Ms: Math.round(v * 100) / 100,
      worstMs: Math.round(worstMs * 100) / 100,
      opCount,
      scopeCode: 5,
    };
  } catch {
    return null;
  }
}

/** storageFailures — the share of those same operations that threw. Same
 *  observation point and same warm-up gate as `readWebStorageLatency`, so the
 *  two readings always agree about how many operations they are talking about.
 *  A `0` here means "zero operations failed", never "we could not look". */
export function readWebStorageFailures(): WebStorageFailureResult | null {
  try {
    if (!installed) {
      if (!blockedHere) return null;
      return {
        score: null,
        rating: "pending",
        failPct: null,
        failCount: 0,
        opCount: 0,
        scopeCode: 5,
        measurable: 0,
        reasonCode: REASON_BLOCKED_BY_ENVIRONMENT,
      };
    }
    if (opCount < STORAGE_MIN_OPS) return null; // warming
    const frac = opCount > 0 ? failCount / opCount : 0;
    const score = linearScore(
      frac,
      WEB_STORAGE_FAILURE_THRESHOLDS.good,
      WEB_STORAGE_FAILURE_THRESHOLDS.poor,
    );
    return {
      score,
      rating: ratingFor(score),
      failPct: Math.round(frac * 1000) / 10,
      failCount,
      opCount,
      scopeCode: 5,
    };
  } catch {
    return null;
  }
}

/** Whether the wrappers are in place (read by the panel's own tests + index). */
export function isLocalStoreTrackingInstalled(): boolean {
  return installed;
}

/**
 * Restore the page's original Web Storage methods and drop all state.
 * Idempotent, NEVER throws. Wired into telemetry.forget(): erasure hands the
 * page's own data path back exactly as it was found, and takes the two
 * counters with it so nothing measured before the erasure can be reported by
 * a later start. A fresh `startWebVitals()` installs again from zero.
 */
export function uninstallLocalStoreTracking(): void {
  try {
    if (proto) {
      for (const [name, orig] of originals) {
        proto[name] = orig;
      }
    }
  } catch {
    /* best-effort — never throw on teardown */
  }
  originals.clear();
  proto = null;
  installed = false;
  blockedHere = false;
  durations.length = 0;
  worstMs = 0;
  opCount = 0;
  failCount = 0;
}

/** @internal test hooks — deterministic, no dependence on a real Web Storage. */
export const _localStoreInternals = {
  WEB_STORAGE_LATENCY_THRESHOLDS,
  WEB_STORAGE_FAILURE_THRESHOLDS,
  STORAGE_MIN_OPS,
  STORAGE_RING_CAP,
  get isInstalled(): boolean {
    return installed;
  },
  get opCount(): number {
    return opCount;
  },
  /** Wrap a caller-provided Storage-shaped prototype via the REAL wrap path,
   *  so tests can drive the observer chain through actual wrapped calls. */
  installWithPrototypeForTests(p: Record<string, unknown>): void {
    if (installed) return;
    wrapPrototype(p);
  },
  /** Force the blocked-here verdict (a sandboxed frame / blocked site data). */
  setBlockedForTests(v: boolean): void {
    blockedHere = v;
  },
  /** Record synthetic op outcomes: durations (ms) with an ok flag. */
  observeForTests(durMs: number, ok: boolean): void {
    observe(durMs, ok);
  },
  reset(): void {
    uninstallLocalStoreTracking();
  },
};
