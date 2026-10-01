/** Dev Posture collector — the input for the additive `devPosture` axis (web).
 *
 * The additive "Dev Posture" meter reports whether a live app is still wearing
 * its development clothes. It reports EXPOSURE, never safety
 * (security-meters-doctrine): a clean tile means "none of the development
 * settings we can read were on", never "you are production-hardened".
 *
 * It reports the development settings we can read; a clean tile means none of
 * them were on — not that the deployment is hardened.
 *
 * READ ONCE AT STARTUP (freeze-at-init, like the cold-start contract): the
 * checks are evaluated one time when the collector starts, cached, and the
 * SAME frozen result is returned on every snapshot even if the environment
 * changes afterwards. A test seam (_resetDevPostureForTests /
 * readDevPostureForTests) resets/overrides it.
 *
 * PER-RUNTIME READABLE CHECKS (Web browser kit):
 *   • debugFlag  — a development build shipped: `process.env.NODE_ENV !==
 *                  "production"` after bundler inlining. Bundlers inline
 *                  `process.env.NODE_ENV` as a string literal, so we read it
 *                  inside a try/catch; if `process` was erased and the read is
 *                  not evaluable, the flag is OMITTED (not readable).
 *   • sourceMaps — ONE bounded startup probe: find the first same-origin
 *                  <script src> and issue a single `HEAD` fetch for
 *                  `src + ".map"` with a ~3s timeout race. response.ok → 1,
 *                  a definite non-ok (404 etc.) → 0, any error/timeout/no
 *                  same-origin script → the flag is OMITTED (we could not
 *                  look). The probe runs once at startup asynchronously and
 *                  updates the frozen result when it resolves (before the first
 *                  snapshot in practice); it never throws and never retries.
 *   verboseErrors / profilingOpen are NOT readable on web → their flags are
 *   OMITTED.
 *
 * WIRE OBJECT (numbers + rating/caption strings only — NEVER an env value,
 * path, URL, or config string):
 *   { score, rating, caption, findings, checks, measurable,
 *     debugFlag?, verboseErrors?, sourceMaps?, profilingOpen? }
 *
 * ADDITIVE / display-only: NEVER feeds the Speed score.
 */

import { unwatchedFetch } from "./callWatch";
import { ratingFor, type Rating } from "./thresholds";

/** The reported axis wire shape. Every field is a number except rating +
 *  caption. The four flags are OPTIONAL — present only when this runtime read
 *  that check this session. */
export interface DevPostureReading {
  /** 0-100. 100 iff no development setting we could read was on. */
  score: number;
  rating: Rating;
  /** Counts + setting NAMES only — never an env value or config string. */
  caption: string;
  /** Number of present flags equal to 1. */
  findings: number;
  /** Number of flag fields present (checks this runtime actually read). */
  checks: number;
  /** 1 always when the axis is emitted. */
  measurable: 1;
  /** Present only when read. 1 = development build; 0 = production. */
  debugFlag?: number;
  /** Present only when read (never readable on web → omitted). */
  verboseErrors?: number;
  /** Present only when read. 1 = a `.map` was served for a script; 0 = 404. */
  sourceMaps?: number;
  /** Present only when read (never readable on web → omitted). */
  profilingOpen?: number;
}

/** The four flag identifiers, in the fixed order the caption lists them. */
export type DevPostureFlag =
  | "debugFlag"
  | "verboseErrors"
  | "sourceMaps"
  | "profilingOpen";

/** Setting NAMES used in the caption — never an env var value or flag string. */
const FLAG_PHRASE: Record<DevPostureFlag, string> = {
  debugFlag: "debug mode on",
  verboseErrors: "verbose error pages on",
  sourceMaps: "source maps served",
  profilingOpen: "profiling port open",
};

/** Short timeout for the one-shot source-map HEAD probe (ms). */
const SOURCEMAP_PROBE_TIMEOUT_MS = 3_000;

/** The flags read so far, frozen at init. Populated once by start(); the async
 *  source-map probe adds/updates its flag in place when it resolves. */
let flags: Partial<Record<DevPostureFlag, number>> | null = null;
let started = false;

/** Assemble the wire object from the flags actually read. Applies the shared
 *  devPosture scoring/rating/caption contract deterministically:
 *   • rating: debugFlag===1 → poor; else findings>0 → needs-work; else good.
 *   • score: 100, −60 if debugFlag===1, −20 per OTHER finding, clamp 0..100.
 *   • caption: good → "none of the {checks} development settings we can read
 *     were on"; otherwise the ON findings joined by ", " using the fixed
 *     setting-name phrases. */
export function assembleDevPosture(
  read: Partial<Record<DevPostureFlag, number>>,
): DevPostureReading {
  const order: DevPostureFlag[] = [
    "debugFlag",
    "verboseErrors",
    "sourceMaps",
    "profilingOpen",
  ];
  let checks = 0;
  let findings = 0;
  const onPhrases: string[] = [];
  const debugOn = read.debugFlag === 1;
  for (const key of order) {
    const v = read[key];
    if (v === undefined) continue;
    checks += 1;
    if (v === 1) {
      findings += 1;
      onPhrases.push(FLAG_PHRASE[key]);
    }
  }

  let score = 100;
  if (debugOn) score -= 60;
  for (const key of order) {
    if (key === "debugFlag") continue;
    if (read[key] === 1) score -= 20;
  }
  if (score < 0) score = 0;
  if (score > 100) score = 100;

  const rating: Rating = debugOn
    ? "poor"
    : findings > 0
      ? "needs-work"
      : "good";

  const caption =
    findings === 0
      ? `none of the ${checks} development settings we can read were on`
      : onPhrases.join(", ");

  const out: DevPostureReading = {
    score,
    rating,
    caption,
    findings,
    checks,
    measurable: 1,
  };
  for (const key of order) {
    const v = read[key];
    if (v !== undefined) out[key] = v;
  }
  return out;
}

/** Read `process.env.NODE_ENV` behind a try/catch. Bundlers inline it as a
 *  string literal, so the read succeeds in a built app. When `process` was
 *  erased (unreadable), return undefined so the flag is OMITTED. */
function readDebugFlag(): number | undefined {
  try {
    const p = (typeof process !== "undefined" ? process : undefined) as
      | { env?: Record<string, string | undefined> }
      | undefined;
    if (!p || !p.env) return undefined;
    const env = p.env.NODE_ENV;
    // Present but not "production" → development build → 1; "production" → 0.
    return env !== "production" ? 1 : 0;
  } catch {
    return undefined;
  }
}

/** First SAME-ORIGIN <script src> URL, or null when none exists / not in a
 *  browser. Used only to build the ".map" probe target — never stored/emitted. */
function firstSameOriginScriptSrc(): string | null {
  try {
    if (typeof document === "undefined" || typeof location === "undefined") {
      return null;
    }
    const scripts = document.scripts;
    if (!scripts || scripts.length === 0) return null;
    const origin = location.origin;
    for (let i = 0; i < scripts.length; i++) {
      const src = scripts[i]?.src;
      if (!src) continue;
      // Same-origin only — a cross-origin CDN map is not honestly ours to read.
      try {
        if (new URL(src, origin).origin === origin) return src;
      } catch {
        // malformed src — skip
      }
    }
    return null;
  } catch {
    return null;
  }
}

/** One-shot, bounded, catch-all source-map HEAD probe. Resolves the frozen
 *  `sourceMaps` flag: response.ok → 1, definite non-ok → 0, any
 *  error/timeout/no same-origin script → leaves the flag OMITTED. Never throws,
 *  never retries. */
async function probeSourceMaps(): Promise<void> {
  try {
    if (typeof fetch !== "function") return;
    const src = firstSameOriginScriptSrc();
    if (src === null) return; // no same-origin script → OMIT
    const mapUrl = `${src}.map`;

    let timer: ReturnType<typeof setTimeout> | null = null;
    const timeout = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), SOURCEMAP_PROBE_TIMEOUT_MS);
    });
    const head = (unwatchedFetch() ?? fetch)(mapUrl, { method: "HEAD" })
      .then((r) => r as Response | null)
      .catch(() => null);

    const res = await Promise.race([head, timeout]);
    if (timer !== null) {
      try {
        clearTimeout(timer);
      } catch {
        // ignore
      }
    }
    // Timeout / network error → res is null → OMIT (we could not look).
    if (res === null || flags === null) return;
    flags.sourceMaps = res.ok ? 1 : 0;
  } catch {
    // catch-all — the flag stays OMITTED
  }
}

/** Evaluate the synchronous checks ONCE, freeze them, and kick off the async
 *  source-map probe (which updates the frozen result when it resolves).
 *  Idempotent — a second call is a no-op so the freeze holds. */
export function startDevPosture(): void {
  if (started) return;
  started = true;
  flags = {};
  const debug = readDebugFlag();
  if (debug !== undefined) flags.debugFlag = debug;
  // verboseErrors + profilingOpen are not readable on web → never added.
  // Fire the one-shot source-map probe (fire-and-forget; it mutates `flags`).
  void probeSourceMaps();
}

/**
 * The Dev Posture axis. Freeze-at-init: the first call to startDevPosture()
 * evaluates the checks; every read returns the frozen picture (with the async
 * source-map flag folded in once the probe resolves). Auto-starts on first
 * read so a caller that never explicitly starts still gets a reading.
 *
 * Web always has at least one readable check in a built app (debugFlag), so in
 * practice it always emits. If NO check could be read at all (checks === 0 —
 * e.g. `process` erased AND no same-origin script), the axis is OMITTED
 * (returns null) — honest absence, never a fabricated clean tile.
 */
export function readDevPosture(): DevPostureReading | null {
  if (!started) startDevPosture();
  const read = flags ?? {};
  if (Object.keys(read).length === 0) return null; // zero readable checks → omit
  return assembleDevPosture(read);
}

/** @internal test seam — reset the frozen state so the next read re-evaluates,
 *  or override the frozen flags directly to test bands / omission without the
 *  browser globals. Follows the kit's leakWatch/cookieExposure test-seam
 *  conventions. */
export function _resetDevPostureForTests(
  override?: Partial<Record<DevPostureFlag, number>>,
): void {
  if (override) {
    started = true;
    flags = { ...override };
  } else {
    started = false;
    flags = null;
  }
}

/** @internal test seam — force a fresh evaluation of the SYNCHRONOUS checks
 *  (debugFlag) and return the reading. The async source-map probe is started
 *  but not awaited here (matching the live startup path). */
export function readDevPostureForTests(): DevPostureReading | null {
  started = false;
  flags = null;
  return readDevPosture();
}

/** @internal test seam — run the source-map HEAD probe to completion against
 *  the CURRENT frozen state (must have been started) and resolve when the flag
 *  has been folded in. Lets a test drive the async path deterministically. */
export async function _runSourceMapProbeForTests(): Promise<void> {
  if (!started || flags === null) startDevPosture();
  await probeSourceMaps();
}
