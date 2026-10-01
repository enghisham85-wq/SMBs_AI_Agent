/** Browser background-work observation.
 *
 * A page can delimit requestIdleCallback callbacks, so those are real job runs.
 * Worker and service-worker APIs do not expose task boundaries to the owning
 * page; when their use is detected they are published as unattached systems
 * rather than invented runs. Names are fixed platform categories and only their
 * cardinality leaves this module.
 */

import { linearScore, ratingFor } from "./thresholds";
import type { WebAxisResult } from "./snapshot";

const MIN_RUNS = 5;
const MAX_DURATIONS = 500;

let installed = false;
let originalRequestIdleCallback:
  | ((callback: IdleRequestCallback, options?: IdleRequestOptions) => number)
  | null = null;
let originalWorker: typeof Worker | null = null;
let runs = 0;
let failed = 0;
let durations: number[] = [];
let attachedIdle = false;
let sawWorker = false;
let sawServiceWorker = false;
let sawPeriodicSync = false;

function noteDuration(ms: number): void {
  durations.push(Math.max(0, Math.round(ms)));
  if (durations.length > MAX_DURATIONS) durations.shift();
}

function percentile95(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.ceil(sorted.length * 0.95) - 1];
}

/** Install guest-safe wrappers and inspect service-worker registrations once. */
export function installBackgroundWorkTracking(): boolean {
  if (installed) return true;
  try {
    if (typeof window === "undefined") return false;
    const w = window as typeof window & {
      requestIdleCallback?: (
        callback: IdleRequestCallback,
        options?: IdleRequestOptions,
      ) => number;
      Worker?: typeof Worker;
    };

    if (typeof w.requestIdleCallback === "function") {
      originalRequestIdleCallback = w.requestIdleCallback.bind(w);
      const real = originalRequestIdleCallback;
      w.requestIdleCallback = (callback, options) =>
        real(
          (deadline) => {
            const start = performance.now();
            runs++;
            try {
              callback(deadline);
            } catch (error) {
              failed++;
              throw error;
            } finally {
              noteDuration(performance.now() - start);
            }
          },
          options,
        );
      attachedIdle = true;
    }

    if (typeof w.Worker === "function") {
      originalWorker = w.Worker;
      const RealWorker = originalWorker;
      w.Worker = new Proxy(RealWorker, {
        construct(target, args, newTarget) {
          sawWorker = true;
          return Reflect.construct(target, args, newTarget) as Worker;
        },
      });
    }

    const serviceWorker = navigator?.serviceWorker;
    if (serviceWorker && typeof serviceWorker.getRegistrations === "function") {
      void serviceWorker.getRegistrations().then((registrations) => {
        if (registrations.length > 0) sawServiceWorker = true;
        for (const registration of registrations) {
          const periodicSync = (
            registration as ServiceWorkerRegistration & {
              periodicSync?: { getTags?: () => Promise<string[]> };
            }
          ).periodicSync;
          if (typeof periodicSync?.getTags === "function") {
            void periodicSync
              .getTags()
              .then((tags) => {
                if (tags.length > 0) sawPeriodicSync = true;
              })
              .catch(() => {});
          }
        }
      }).catch(() => {});
    }
    installed = true;
    return true;
  } catch {
    uninstallBackgroundWorkTracking();
    return false;
  }
}

export function uninstallBackgroundWorkTracking(): void {
  try {
    if (typeof window !== "undefined") {
      const w = window as typeof window & {
        requestIdleCallback?: typeof originalRequestIdleCallback;
        Worker?: typeof Worker;
      };
      if (originalRequestIdleCallback) {
        w.requestIdleCallback = originalRequestIdleCallback;
      }
      if (originalWorker) w.Worker = originalWorker;
    }
  } catch {
    // best effort
  }
  installed = false;
  originalRequestIdleCallback = null;
  originalWorker = null;
  runs = 0;
  failed = 0;
  durations = [];
  attachedIdle = false;
  sawWorker = false;
  sawServiceWorker = false;
  sawPeriodicSync = false;
}

/** Absent until a run or a concrete blind spot is seen. */
export function readBackgroundWork(): WebAxisResult | null {
  const unattachedSystems =
    Number(sawWorker) + Number(sawServiceWorker) + Number(sawPeriodicSync);
  if (runs === 0 && unattachedSystems === 0) return null;
  const measurable = runs >= MIN_RUNS ? 1 : 0;
  const failPct = runs > 0 ? Math.round((failed / runs) * 1000) / 10 : null;
  const score =
    measurable === 1 && failPct !== null
      ? linearScore(failPct, 1, 20)
      : null;
  return {
    score,
    rating: score === null ? "pending" : ratingFor(score),
    measurable,
    runs: runs || null,
    jobNames: runs > 0 ? 1 : null,
    otherNames: null,
    failed: failed || null,
    retried: null,
    retryWorst: null,
    overlaps: null,
    waitRuns: null,
    recurring: null,
    hostedRuns: runs || null,
    manualRuns: null,
    attachedSystems: attachedIdle ? 1 : null,
    unattachedSystems: unattachedSystems || null,
    failPct,
    p95Ms: percentile95(durations),
    worstMs: durations.length > 0 ? Math.max(...durations) : null,
    waitP95Ms: null,
    missed: null,
  };
}

/** @internal deterministic contract hook. */
export function _setBackgroundWorkForTests(state: {
  durationsMs?: number[];
  failed?: number;
  worker?: boolean;
  serviceWorker?: boolean;
  periodicSync?: boolean;
}): void {
  durations = [...(state.durationsMs ?? [])];
  runs = durations.length;
  failed = state.failed ?? 0;
  attachedIdle = runs > 0;
  sawWorker = state.worker ?? false;
  sawServiceWorker = state.serviceWorker ?? false;
  sawPeriodicSync = state.periodicSync ?? false;
}