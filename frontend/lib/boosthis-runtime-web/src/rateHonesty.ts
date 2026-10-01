/* ─── Boosthis: the earned-rate contract (Web) ────────────────────────────
 *
 * ONE rule, shared by every rate-per-time reading in this kit:
 *
 *   NO READING MAY EXTRAPOLATE MORE THAN TWELVEFOLD. A projection to a unit
 *   may only be published from an observation window of at least a twelfth of
 *   that unit — five minutes for a per-hour figure, five seconds for a
 *   per-minute one. Below that the projection is withheld, and so is any score
 *   or rating derived from it, while the raw count and the window actually
 *   observed are still reported.
 *
 * WHY: a per-hour projection multiplies what it saw by 3600/observed. At 96
 * seconds that factor is 37x, so a handful of errors becomes a confident-
 * sounding "308 errors/hour" the observation never earned. Capping the
 * extrapolation — rather than fixing one wall-clock minimum for every unit —
 * is what makes the same rule honest for an hourly crash rate and for a
 * per-minute reading on a page that is only open for two minutes.
 *
 * EVENTS ARE NEVER HIDDEN BY THIS RULE. A crash, an unhandled error, a hang —
 * the COUNT surfaces the moment it happens. What a short window withholds is
 * the projection and the judgement, never the event itself.
 *
 * NOT for slopes fitted over samples (heap MB/min) or ratios over a completed
 * sample (error %) — nothing is extrapolated there. See
 * docs/rate-honesty-contract.md, and the guard in
 * scripts/src/__tests__/rate-honesty-contract.test.ts.
 *
 * This module is duplicated verbatim in every kit on purpose: a kit ships to
 * the customer as standalone source and may not import a sibling package.
 */

/** The one constant. No published rate may claim more than this multiple of
 *  what it actually observed. */
export const MAX_RATE_EXTRAPOLATION = 12;

/** Minimum observation for a PER-HOUR projection: 3_600_000 / 12 = 5 minutes. */
export const MIN_RATE_WINDOW_MS = 3_600_000 / MAX_RATE_EXTRAPOLATION;

/** The same minimum in minutes (5), for readings whose window is in minutes. */
export const MIN_RATE_WINDOW_MIN = MIN_RATE_WINDOW_MS / 60_000;

/** Minimum observation for a PER-MINUTE projection: 60_000 / 12 = 5 seconds. */
export const MIN_RATE_WINDOW_MS_PER_MIN = 60_000 / MAX_RATE_EXTRAPOLATION;

/** Has this window (ms) earned the right to publish a PER-HOUR projection? */
export function rateEarnedMs(windowMs: number): boolean {
  return (
    typeof windowMs === "number" &&
    Number.isFinite(windowMs) &&
    windowMs >= MIN_RATE_WINDOW_MS
  );
}

/** Has this window (minutes) earned the right to publish a PER-HOUR projection? */
export function rateEarnedMin(windowMin: number): boolean {
  return (
    typeof windowMin === "number" &&
    Number.isFinite(windowMin) &&
    windowMin >= MIN_RATE_WINDOW_MIN
  );
}

/** `count` per hour, or null when the window has not earned the projection.
 *  Never floors the denominator: an unearned window returns null instead. */
export function earnedPerHour(count: number, windowMs: number, dp = 1): number | null {
  if (!rateEarnedMs(windowMs)) return null;
  const n = typeof count === "number" && Number.isFinite(count) ? Math.max(0, count) : 0;
  const p = Math.pow(10, dp);
  return Math.round((n / (windowMs / 3_600_000)) * p) / p;
}

/** `count` per minute, or null when the window has not earned the projection. */
export function earnedPerMin(count: number, windowMs: number, dp = 1): number | null {
  if (
    typeof windowMs !== "number" ||
    !Number.isFinite(windowMs) ||
    windowMs < MIN_RATE_WINDOW_MS_PER_MIN
  ) {
    return null;
  }
  const n = typeof count === "number" && Number.isFinite(count) ? Math.max(0, count) : 0;
  const p = Math.pow(10, dp);
  return Math.round((n / (windowMs / 60_000)) * p) / p;
}

/** The observation window in MINUTES, at the precision that decides honestly.
 *  Never rounded UP to time we did not watch: "1m observed" after thirty
 *  seconds is the same fabrication as a rate the window has not earned, one
 *  level down. The wording layer decides how to SAY it; this only refuses to
 *  lose the truth.
 *
 *  FLOORED TO A WHOLE SECOND. The far side re-judges eligibility from the
 *  window we report, so this one number has two jobs, and only a second-
 *  grained floor does both:
 *
 *    • It may never overstate the observation. A window rounded UP across the
 *      line (4m59.7s → "5.00") hands the reader four minutes fifty-nine
 *      seconds dressed as an earned five.
 *    • It must decide exactly as the raw milliseconds do. Both minimums are a
 *      whole number of seconds — 300s for a per-hour projection, 5s for a
 *      per-minute one — so flooring to a second changes no verdict: 5.000s
 *      reports 5/60 of a minute and earns, 4.999s reports 4/60 and does not.
 *      A two-decimal minute cannot manage that. A twelfth of a minute is
 *      0.0833…, so an earned five-second look truncates to "0.08", falls
 *      short of the threshold on arrival, and is refused by the very rule it
 *      satisfied — a rate the observation DID earn, withheld at the edge.
 *
 *  Eligibility on this side is still decided on the raw elapsed milliseconds.
 *  This value exists to be REPORTED, and to let the far side reach the same
 *  verdict from what it receives. */
export function windowMinOf(windowMs: number): number {
  const ms =
    typeof windowMs === "number" && Number.isFinite(windowMs) && windowMs > 0
      ? windowMs
      : 0;
  return Math.floor(ms / 1000) / 60;
}

/** The observation stated in words, for a caption written inside the kit.
 *  A window that rounds to nothing is SAID, never printed as "0m": a reader
 *  cannot tell "we watched for ten seconds" from "we measured zero minutes",
 *  and only one of those is true. Mirrors observedPhrase() on the server so
 *  the same window reads the same way wherever it is rendered. */
export function minuteText(windowMin: number): string {
  // Below ten minutes a rounding is large next to the window itself — "2.5m"
  // rendered as "3m" claims half a minute we never watched — so keep a decimal
  // and truncate. A window may read shorter than it was, never longer.
  if (windowMin < 10) return `${Math.floor(windowMin * 10) / 10}m`;
  return `${Math.round(windowMin)}m`;
}

export function windowPhrase(windowMin: number, suffix = "observed"): string {
  if (typeof windowMin !== "number" || !Number.isFinite(windowMin)) {
    return `observation window not reported`;
  }
  if (windowMin < 1) return `under a minute ${suffix}`.trim();
  return `${minuteText(windowMin)} ${suffix}`.trim();
}

/** The window as it reads INSIDE a rate phrase — "over 5s", "over 12m".
 *  Mirrors ratePhrase() on the server, and exists because a per-minute
 *  projection can rest on a window of seconds: windowPhrase() would render
 *  that as "over under a minute", which is not English and hides how short the
 *  look was. Seconds say it exactly. */
export function overWords(windowMin: number): string {
  if (typeof windowMin !== "number" || !Number.isFinite(windowMin)) {
    return "an unreported window";
  }
  return windowMin >= 1 ? minuteText(windowMin) : `${Math.round(windowMin * 60)}s`;
}
