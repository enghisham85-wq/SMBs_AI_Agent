/** In-process sample ring buffer. Mirrors `lib/boosthis-py/boosthis/samples.py`. */

import type { Rating } from "./thresholds";

export interface Sample {
  name: string;
  duration_ms: number;
  rating: Rating;
  timestamp_ms: number;
}

export interface PerRouteSummary {
  count: number;
  max_ms: number;
  worst_rating: Rating;
}

export interface Summary {
  total: number;
  good: number;
  needsWork: number;
  poor: number;
  p50_ms: number | null;
  p75_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
  byRoute: Record<string, PerRouteSummary>;
}

const MAX_SAMPLES = 1000;
let buffer: Sample[] = [];

/** Optional observer notified after every recorded sample. The telemetry layer
 *  registers one (when an app has opted in) to drive issue/fix reporting and
 *  optional full-sample upload. Local-only installs never set it, so recording
 *  stays a pure in-process operation. */
export type SampleObserver = (sample: Sample) => void;
let observer: SampleObserver | null = null;

export function setSampleObserver(fn: SampleObserver | null): void {
  observer = fn;
}

/** Whether a recorded reading has anywhere to go. The telemetry layer sets the
 *  observer above EXACTLY when per-page readings are uploaded, so this answers
 *  "will a reading leave this page?" by construction rather than by guessing at
 *  the mode from somewhere else. */
export function hasSampleObserver(): boolean {
  return observer !== null;
}

/** A SECOND, additive slot, independent of the upload observer above.
 *
 *  The kit's own honesty notice needs one fact the rest of the kit never
 *  published: that a reading was actually TAKEN. That is a different fact from
 *  "the server confirmed this install", and announcing the second as if it were
 *  the first is the defect this slot exists to close. It never ships anything —
 *  the observer above owns every wire. */
let watcher: SampleObserver | null = null;

export function setSampleWatcher(fn: SampleObserver | null): void {
  watcher = fn;
}

export function record(name: string, durationMs: number, rating: Rating): Sample {
  const s: Sample = {
    name,
    duration_ms: Math.round(durationMs),
    rating,
    timestamp_ms: Date.now(),
  };
  buffer.push(s);
  if (buffer.length > MAX_SAMPLES) buffer.shift();
  // Notify the telemetry layer (if registered). Never let an observer error
  // take down the host app's request path — instrumentation must be silent.
  if (observer) {
    try {
      observer(s);
    } catch {
      // swallow
    }
  }
  if (watcher) {
    try {
      watcher(s);
    } catch {
      // swallow
    }
  }
  return s;
}

export function recent(opts: { limit?: number; name?: string } = {}): Sample[] {
  const { limit = 100, name } = opts;
  // Snapshot first to avoid mid-iteration mutation.
  const snap = buffer.slice();
  const out: Sample[] = [];
  for (let i = snap.length - 1; i >= 0 && out.length < limit; i--) {
    const s = snap[i];
    if (name && s.name !== name) continue;
    out.push(s);
  }
  return out;
}

function pct(sorted: number[], p: number): number {
  const idx = Math.min(sorted.length - 1, Math.floor(sorted.length * p));
  return sorted[idx];
}

const ORDER: Record<Rating, number> = { good: 0, "needs-work": 1, poor: 2 };

export function summary(): Summary {
  const snap = buffer.slice();
  if (snap.length === 0) {
    return {
      total: 0,
      good: 0,
      needsWork: 0,
      poor: 0,
      p50_ms: null,
      p75_ms: null,
      p95_ms: null,
      p99_ms: null,
      byRoute: {},
    };
  }
  const durations = snap.map((s) => s.duration_ms).sort((a, b) => a - b);
  let good = 0, needsWork = 0, poor = 0;
  const byRoute: Record<string, PerRouteSummary> = {};
  for (const s of snap) {
    if (s.rating === "good") good++;
    else if (s.rating === "needs-work") needsWork++;
    else poor++;
    const r = byRoute[s.name] ?? { count: 0, max_ms: 0, worst_rating: "good" as Rating };
    r.count++;
    if (s.duration_ms > r.max_ms) r.max_ms = s.duration_ms;
    if (ORDER[s.rating] > ORDER[r.worst_rating]) r.worst_rating = s.rating;
    byRoute[s.name] = r;
  }
  return {
    total: snap.length,
    good,
    needsWork,
    poor,
    p50_ms: pct(durations, 0.5),
    p75_ms: pct(durations, 0.75),
    p95_ms: pct(durations, 0.95),
    p99_ms: pct(durations, 0.99),
    byRoute,
  };
}

export function clear(): void {
  buffer = [];
}
