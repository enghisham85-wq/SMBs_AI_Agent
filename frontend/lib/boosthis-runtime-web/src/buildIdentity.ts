/** Build identity for the web "Patch Lag" meter (build-identity / exposure
 *  window). Resolves WHAT build is running and WHEN it was built, so the
 *  additive `patchLag` axis can report how stale the currently-deployed code
 *  is. Pure + display-only: the numbers never feed the composite Speed score.
 *
 * HONEST SCOPE (web): build stamp ONLY. There is no dependency inventory on the
 * web runtime (no filesystem / package manifest in a browser) and no env vars
 * (a browser cannot read process env), so this module resolves identity from
 * exactly two honest sources, first known wins:
 *   a. developer-supplied init/config options (`buildTimeMs` / `buildCommit`),
 *      validated before they are trusted; and
 *   b. `Date.parse(document.lastModified)` — the server-reported HTML
 *      timestamp, a deploy-freshness proxy for STATIC pages — but only when it
 *      yields a finite epoch > 2000-01-01, is in the past, AND can be told
 *      apart from the moment the response itself arrived.
 *
 * WHY (b) IS NARROWER THAN IT LOOKS. `document.lastModified` has no honest
 * absent value: with no `Last-Modified` header the DOM returns the CURRENT
 * time. A server-rendered page therefore hands back the moment the response was
 * generated, and reading that as a build time made every web install in
 * production report a build 0.0–0.2 seconds old, rated good, for ever. The two
 * cases are separated by distance from the navigation response — see
 * RESPONSE_TIME_MARGIN_MS. When they cannot be separated the answer is UNKNOWN,
 * and the snapshot says so out loud rather than omitting the axis (an expected
 * axis that never arrives renders as "warming up", which is a promise).
 *
 * NEVER fabricate: an unknown field is omitted; when nothing is known the whole
 * build object is omitted. Every browser/DOM access is guarded in try/catch so
 * one unsupported API can never cost the snapshot (kit-guest-safety rule).
 */

/** A resolved build identity. Every field is optional — only KNOWN fields are
 *  populated, so an absent field means "not known", never a fabricated 0. */
export interface BuildIdentity {
  /** Lowercase hex commit sha (7–40 chars), when known. */
  commit?: string;
  /** Build time as epoch ms, when known. */
  buildTimeMs?: number;
}

/** Config-supplied build hints the developer passes to `startWebVitals`. */
export interface BuildIdentityOptions {
  /** Epoch ms the running build was produced. */
  buildTimeMs?: number;
  /** Commit sha of the running build (validated lowercase hex, 7–40 chars). */
  buildCommit?: string;
}

/**
 * Boosthis's closed "why this reading cannot be taken here" code for a source
 * the HOST APP has to supply and has not: the dashboard renders it as
 * "not available here · the app has not switched this on".
 *
 * The number is the contract (`axisAvailability.ts`, server-side); a kit may
 * never send prose, because a kit runs inside a customer's app and anything it
 * types could carry the customer's data. Kept as a local constant for the same
 * reason every other kit does: a kit cannot import from the server.
 */
export const REASON_NOT_WIRED_BY_HOST = 1;

/** The platform does not share this number with apps, so no amount of
 *  waiting will produce it here. Sent only after a LOOK that found nothing —
 *  the browser was asked for the mechanism and does not have it — never
 *  inferred from an absent value, which is also what a kit that has not
 *  started yet looks like. Same number as every other kit's
 *  `REASON_PLATFORM_DOES_NOT_EXPOSE`. */
export const REASON_PLATFORM_DOES_NOT_EXPOSE = 3;

/** The reading compares things this app has only one of — or has none of
 *  yet. There is no edge to measure against, so "how close are you" has no
 *  answer and inventing a ceiling would be the invented number the reading
 *  refuses. Same number as every other kit's `REASON_NOTHING_TO_COMPARE`:
 *  the vocabulary is shared and the codes are permanent. */
export const REASON_NOTHING_TO_COMPARE = 6;

/** A commit sha is trusted only when it is lowercase hex, 7–40 chars. The
 *  server allowlist applies the identical rule — reject anything else. */
const COMMIT_RE = /^[0-9a-f]{7,40}$/;

/** Epoch-ms floor: 2000-01-01T00:00:00Z. Any earlier timestamp is treated as
 *  bogus (clock not set / parse artifact) and rejected. */
const MIN_BUILD_TIME_MS = 946_684_800_000;

/** Validate + normalize a commit string. Lowercases first (a sha is
 *  case-insensitive hex), then enforces the 7–40 hex shape. Returns undefined
 *  for anything that does not match — never a partial/fabricated value. */
export function normalizeCommit(raw: unknown): string | undefined {
  if (typeof raw !== "string") return undefined;
  const lowered = raw.trim().toLowerCase();
  return COMMIT_RE.test(lowered) ? lowered : undefined;
}

/** Validate a build-time value supplied as a number (epoch ms). Rejects
 *  non-finite values and anything at/below the year-2000 floor. */
function normalizeBuildTimeMs(raw: unknown): number | undefined {
  if (typeof raw !== "number" || !Number.isFinite(raw)) return undefined;
  const ms = Math.round(raw);
  return ms > MIN_BUILD_TIME_MS ? ms : undefined;
}

/** Config identity supplied by the developer through init options. Overridable
 *  for tests via the `_set*ForTests` seam so resolution is deterministic. */
let configOptions: BuildIdentityOptions = {};

/** Record the developer's build hints from `startWebVitals`. Called once at
 *  init; values are validated at RESOLVE time (not here) so a bad value simply
 *  falls through to the next source rather than throwing. */
export function setBuildIdentityOptions(opts: BuildIdentityOptions | undefined): void {
  configOptions = opts ? { ...opts } : {};
}

/**
 * How far BEFORE the navigation response `document.lastModified` must sit
 * before we believe it is a build time.
 *
 * `document.lastModified` falls back to the CURRENT time whenever the server
 * sent no `Last-Modified` header — which is what every server-rendered page
 * does. The stamp is then the moment the property was READ, not the moment the
 * app was built, and reading it as a build time makes every deploy look newborn
 * for ever. A genuinely static file's mtime is minutes-to-months BEHIND the
 * response and never moves; a fabricated one lands on the response at page load
 * and then walks forward with the clock, which is why the test is one-sided: a
 * stamp at or after the response is refused however far past it lands. A
 * two-sided distance test passes a page that has simply been open a minute.
 *
 * Sized well above `document.lastModified`'s one-second resolution and any
 * plausible clock skew between the server writing the header and the browser
 * reading the clock. Costs us a build shipped in the half-minute before the
 * page loaded — for which we say "unknown" instead of "brand new", which is the
 * honest of the two mistakes.
 */
const RESPONSE_TIME_MARGIN_MS = 30_000;

/** When the navigation response for this document arrived, as epoch ms, from
 *  Navigation Timing. Undefined when the page has no navigation entry to read
 *  (SSR, a worker, a browser that does not expose it) — in which case a
 *  last-modified stamp cannot be told apart from the response time at all. */
function navigationResponseMs(): number | undefined {
  try {
    if (typeof performance === "undefined") return undefined;
    const origin = performance.timeOrigin;
    if (!Number.isFinite(origin) || origin <= MIN_BUILD_TIME_MS) return undefined;
    // Modern: the PerformanceNavigationTiming entry.
    const entries = performance.getEntriesByType?.("navigation");
    const first = Array.isArray(entries) ? entries[0] : undefined;
    const responseStart = (first as { responseStart?: unknown } | undefined)
      ?.responseStart;
    if (typeof responseStart === "number" && Number.isFinite(responseStart)) {
      return origin + responseStart;
    }
    // No entry (or a zero-length navigation list) — the page start is still a
    // sound stand-in for "when this document arrived".
    return origin;
  } catch {
    return undefined;
  }
}

/** The HTML document's server-reported last-modified time, as epoch ms, when it
 *  is a finite epoch > 2000-01-01, in the past, AND distinguishable from the
 *  moment the response itself arrived. This is a deploy-freshness proxy: a
 *  STATIC page's `Last-Modified` reflects when the server wrote the HTML. On a
 *  server-RENDERED page there is no such header, the DOM fabricates "now", and
 *  we refuse it — see RESPONSE_TIME_MARGIN_MS. All access is guarded — SSR /
 *  no-DOM / a bogus string all yield undefined (honest absence), never a throw
 *  and never a fake value. */
function documentLastModifiedMs(nowMs: number): number | undefined {
  try {
    if (typeof document === "undefined") return undefined;
    const raw = document.lastModified;
    if (typeof raw !== "string" || raw.length === 0) return undefined;
    const parsed = Date.parse(raw);
    if (!Number.isFinite(parsed)) return undefined;
    if (parsed <= MIN_BUILD_TIME_MS) return undefined;
    // Must be in the past — a future stamp is a clock/parse artifact, not a
    // deploy time, so we refuse to trust it.
    if (parsed > nowMs) return undefined;
    // Must be TELLABLE APART from the response. Without a navigation time we
    // cannot make that judgement, so we decline rather than guess: an unknown
    // build age is a real answer, a fabricated zero-second one is not.
    const responseMs = navigationResponseMs();
    if (responseMs === undefined) return undefined;
    // One-sided on purpose: a build happened BEFORE the response that carried
    // it. A stamp at or after the response is the DOM's fabricated "now" —
    // which keeps moving, so on a page left open it drifts past any two-sided
    // window and would be believed again as a brand-new build.
    if (parsed > responseMs - RESPONSE_TIME_MARGIN_MS) return undefined;
    return parsed;
  } catch {
    return undefined;
  }
}

/** Resolve the running build's identity, first known source wins:
 *   a. developer-supplied init/config options (validated), then
 *   b. `document.lastModified` (buildTime only — no commit from the DOM).
 *  Never fabricates: an unresolved field is omitted. `nowMs` anchors the
 *  "in the past" guard on the DOM fallback. */
export function resolveBuildIdentity(nowMs: number): BuildIdentity {
  const out: BuildIdentity = {};

  // (a) config options — commit + buildTime, each validated independently so a
  // bad commit does not discard a good buildTime (and vice-versa).
  const cfgCommit = normalizeCommit(configOptions.buildCommit);
  if (cfgCommit !== undefined) out.commit = cfgCommit;
  const cfgTime = normalizeBuildTimeMs(configOptions.buildTimeMs);
  if (cfgTime !== undefined) out.buildTimeMs = cfgTime;

  // (b) DOM fallback for buildTime ONLY when config did not supply one.
  if (out.buildTimeMs === undefined) {
    const domTime = documentLastModifiedMs(nowMs);
    if (domTime !== undefined) out.buildTimeMs = domTime;
  }

  return out;
}

/** @internal test hook — override the config identity between hermetic runs. */
export function _setBuildIdentityOptionsForTests(
  opts: BuildIdentityOptions | undefined,
): void {
  setBuildIdentityOptions(opts);
}

/** @internal test hook — reset build-identity state between hermetic runs. */
export function _resetBuildIdentityForTests(): void {
  configOptions = {};
}
