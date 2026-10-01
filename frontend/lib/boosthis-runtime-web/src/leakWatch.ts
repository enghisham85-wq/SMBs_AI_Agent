/** Leak Watch tracker — the input for the additive `leakWatch` axis.
 *
 * DETECTS the CUSTOMER's app leaking stack traces, secrets/keys, or personal
 * details to its OWN users (error-level log/console output, and — web-only —
 * stack traces rendered onto the page). Runtime evidence only. ADDITIVE +
 * display-only: NEVER feeds the Speed score, exactly like swallowedErrors.
 *
 * OBSERVATION POINTS (browser):
 *   1. Log/console output — we PIGGYBACK the SAME `console.error` chain that
 *      swallowedErrors installs (see `swallowedErrors.ts`). The host's original
 *      is always called first; we only scan a bounded prefix of the stringified
 *      args on the side and record a category + timestamp. Categories from log
 *      output: secret, pii (stack-shaped console output on web is not counted
 *      here — the DOM scan below owns user-visible stacks).
 *   2. DOM — on a passive window `error` event we schedule ONE deferred
 *      (~1s) scan of `document.body.innerText` (first 20 000 chars) for
 *      stack-frame patterns, throttled to at most one scan / 30 s. Category:
 *      stack.
 *
 * CLASSIFICATION: each observation is put into EXACTLY ONE category, priority
 * secret > pii > stack, so stackCount + secretCount + piiCount === count.
 *
 * GUEST SAFETY (this runs inside a customer's page):
 *   - all scanning is monitor-only, chain-and-delegate, and wrapped in
 *     try/catch that silently drops — never alters host behavior, never throws
 *     into the host,
 *   - bounded work per event (first 4096 chars of a log message, first 20 000
 *     chars of innerText, one deferred DOM scan / 30 s),
 *   - the passive `error` listener never calls preventDefault and is removed on
 *     uninstall,
 *   - installation is idempotent.
 *
 * ABSOLUTE PRIVACY RULE (a mistake here is itself a breach): the matched text,
 * the matched value, the log line, the response body, and the route/path NAME
 * are NEVER stored beyond the local classification scope, never logged, never
 * placed in any field, caption, or error. COUNTERS + TIMESTAMPS + CATEGORY
 * ONLY. The ring holds at most LEAK_RING_CAP {ts, cat} records.
 */

import {
  computeLeakWatch,
  LEAK_MIN_WINDOW_MS,
  type LeakWatchResult,
} from "./meterAxes";
import { onConsoleErrorArgs } from "./swallowedErrors";
import { EMAIL_RE } from "./no-pii";

/* ─── Gates + window (mirror swallowedErrors) ────────────────────────────── */

/** Trailing window capped at 60 minutes. */
export const LEAK_WINDOW_MS = 60 * 60_000;
/** Bounded ring of {timestamp, category} records. */
export const LEAK_RING_CAP = 200;
/** Bounded scan prefix for a stringified log message/args. */
const LOG_SCAN_MAX = 4096;
/** Bounded scan prefix for the rendered page text. */
const DOM_SCAN_MAX = 20_000;
/** At most one deferred DOM scan per this interval. */
const DOM_SCAN_THROTTLE_MS = 30_000;
/** Deferred DOM scan delay after a window `error` event. */
const DOM_SCAN_DELAY_MS = 1_000;

/** The closed set of leak categories that ever cross the wire (as counts). */
export type LeakCategory = "stack" | "secret" | "pii";

/* ─── Patterns (conservative; include ONLY these) ─────────────────────────
 * PRIVACY: these are used ONLY to CLASSIFY within the local scan scope — the
 * matched substrings are never captured, stored, or surfaced.
 */
const STACK_JS_AT_RE = /\bat .{1,200}\(.{1,200}:\d+:\d+\)/;
const STACK_ERROR_AT_RE = /Error:[\s\S]{0,200}\n\s{4}at /;

const JWT_RE = /eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/;
const BEARER_RE = /bearer\s+\S{8,}/i;
const AKIA_RE = /AKIA[0-9A-Z]{16}/;
const PRIVATE_KEY_RE = /-----BEGIN [\s\S]{0,80}PRIVATE KEY/;
const SECRET_KV_RE =
  /(api[_-]?key|secret|token)["']?\s*[:=]\s*["']?[A-Za-z0-9_\-]{16,}/i;

// PII from log output uses the kit's EXISTING no-pii email pattern (reused, not
// duplicated) — the pii category of the leak scan.

function looksLikeSecret(text: string): boolean {
  return (
    JWT_RE.test(text) ||
    BEARER_RE.test(text) ||
    AKIA_RE.test(text) ||
    PRIVATE_KEY_RE.test(text) ||
    SECRET_KV_RE.test(text)
  );
}

function looksLikePII(text: string): boolean {
  return EMAIL_RE.test(text);
}

function looksLikeStack(text: string): boolean {
  return STACK_JS_AT_RE.test(text) || STACK_ERROR_AT_RE.test(text);
}

/** Classify a bounded text into EXACTLY ONE category (priority secret > pii >
 *  stack) or null when nothing matches. Never returns/stores the matched text. */
function classify(
  text: string,
  allowStack: boolean,
): LeakCategory | null {
  if (looksLikeSecret(text)) return "secret";
  if (looksLikePII(text)) return "pii";
  if (allowStack && looksLikeStack(text)) return "stack";
  return null;
}

/* ─── State (COUNTERS + TIMESTAMPS + CATEGORY ONLY) ───────────────────────── */

interface LeakEvent {
  ts: number;
  cat: LeakCategory;
}

let installed = false;
let armed = false;
let startedAt = 0;
let ring: LeakEvent[] = [];

let logUnsubscribe: (() => void) | null = null;
let domErrorListener: ((this: unknown) => void) | null = null;
let lastDomScanAt = 0;

function nowMs(): number {
  return Date.now();
}

/** Record one classified observation — a {timestamp, category} only. Never the
 *  matched text/value, the log line, the response body, or the route name. */
function note(cat: LeakCategory): void {
  try {
    if (!armed) return;
    ring.push({ ts: nowMs(), cat });
    if (ring.length > LEAK_RING_CAP) {
      ring.splice(0, ring.length - LEAK_RING_CAP);
    }
  } catch {
    // never let bookkeeping break the host
  }
}

/** Scan a bounded stringification of console.error args. Log output yields
 *  secret/pii only (user-visible stacks are the DOM scan's job). */
function scanLogArgs(args: unknown[]): void {
  try {
    if (!armed) return;
    let text = "";
    for (const a of args) {
      if (text.length >= LOG_SCAN_MAX) break;
      try {
        text +=
          typeof a === "string"
            ? a
            : a instanceof Error
              ? `${a.name}: ${a.message}`
              : String(a);
      } catch {
        // a hostile toString must never break us
      }
      text += " ";
    }
    if (text.length > LOG_SCAN_MAX) text = text.slice(0, LOG_SCAN_MAX);
    const cat = classify(text, /* allowStack */ false);
    // Discard the buffer immediately after classification.
    text = "";
    if (cat) note(cat);
  } catch {
    // scans never throw into the host
  }
}

/** Deferred, throttled scan of the rendered page text for stack frames. */
function scheduleDomScan(): void {
  try {
    if (!armed) return;
    if (typeof setTimeout !== "function") return;
    const t = nowMs();
    if (t - lastDomScanAt < DOM_SCAN_THROTTLE_MS) return;
    lastDomScanAt = t;
    setTimeout(() => {
      try {
        if (!armed) return;
        const body =
          typeof document !== "undefined" ? document.body : undefined;
        let text =
          body && typeof body.innerText === "string"
            ? body.innerText.slice(0, DOM_SCAN_MAX)
            : "";
        const isStack = looksLikeStack(text);
        // Discard the buffer immediately after scanning.
        text = "";
        if (isStack) note("stack");
      } catch {
        // never throw out of a deferred callback
      }
    }, DOM_SCAN_DELAY_MS);
  } catch {
    // ignore
  }
}

/** Install both observation points once. Guest-safe + idempotent. Returns true
 *  when at least the log piggyback attached (or was already installed). The
 *  axis stays absent (pending) until the 5-minute window is met regardless. */
export function installLeakWatch(): boolean {
  if (installed) return true;
  try {
    // 1. Piggyback the SAME console.error chain swallowedErrors owns — never a
    //    second wrapper/hook. Returns a no-op unsubscribe when the chain is not
    //    (yet) installed.
    logUnsubscribe = onConsoleErrorArgs(scanLogArgs);

    // 2. Passive, non-capturing window `error` listener — observe only, never
    //    preventDefault; schedules the deferred DOM scan.
    if (
      typeof window !== "undefined" &&
      typeof window.addEventListener === "function"
    ) {
      domErrorListener = (): void => scheduleDomScan();
      window.addEventListener("error", domErrorListener);
    }

    installed = true;
    armed = true;
    startedAt = nowMs();
    ring = [];
    lastDomScanAt = 0;
    return true;
  } catch {
    uninstallLeakWatch();
    return false;
  }
}

/** Detach the log piggyback + passive listener and clear state. Safe to call
 *  repeatedly. */
export function uninstallLeakWatch(): void {
  try {
    if (logUnsubscribe) logUnsubscribe();
  } catch {
    // ignore
  }
  try {
    if (
      typeof window !== "undefined" &&
      typeof window.removeEventListener === "function" &&
      domErrorListener
    ) {
      window.removeEventListener("error", domErrorListener);
    }
  } catch {
    // ignore
  }
  logUnsubscribe = null;
  domErrorListener = null;
  ring = [];
  startedAt = 0;
  lastDomScanAt = 0;
  installed = false;
  armed = false;
}

/** Current Leak Watch reading. Pending (null score) until armed AND the
 *  trailing window reaches the 5-minute minimum — never a fabricated reading.
 *  When installation was impossible it stays pending, so the snapshot omits the
 *  axis. Output is COUNTS + rating + caption only — never any matched content. */
export function readLeakWatch(): LeakWatchResult {
  if (!armed) {
    return {
      score: null,
      rating: "pending",
      count: 0,
      perHour: null,
      windowMin: null,
      stackCount: 0,
      secretCount: 0,
      piiCount: 0,
      caption: null,
    };
  }
  const now = nowMs();
  const windowMs = Math.min(now - startedAt, LEAK_WINDOW_MS);
  const cutoff = now - windowMs;
  let stackCount = 0;
  let secretCount = 0;
  let piiCount = 0;
  for (const e of ring) {
    if (e.ts < cutoff) continue;
    if (e.cat === "stack") stackCount++;
    else if (e.cat === "secret") secretCount++;
    else if (e.cat === "pii") piiCount++;
  }
  return computeLeakWatch({
    count: stackCount + secretCount + piiCount,
    stackCount,
    secretCount,
    piiCount,
    windowMs,
  });
}

/** True once installed (guards double-install at the caller). */
export function isLeakWatchInstalled(): boolean {
  return installed;
}

/** @internal test hook — seed the ring + window deterministically without live
 *  console/DOM events or wall-clock. Marks the axis "armed" so readLeakWatch
 *  scores. `events` are {ago (ms-before-now), cat}. */
export function _setLeakWatchForTests(opts: {
  windowElapsedMs: number;
  events: Array<{ agoMs: number; cat: LeakCategory }>;
}): void {
  armed = true;
  installed = true;
  const now = nowMs();
  startedAt = now - opts.windowElapsedMs;
  ring = opts.events.map((e) => ({ ts: now - e.agoMs, cat: e.cat }));
}

/** @internal test hook — number of records currently held in the ring. */
export function _leakRingSizeForTests(): number {
  return ring.length;
}

/** @internal test hook — reset all state (no restore of host hooks). */
export function _resetLeakWatchForTests(): void {
  uninstallLeakWatch();
}

/** @internal test hook — feed a synthetic console.error arg list through the
 *  SAME scan path a host call would trigger (no real console needed). */
export function _scanLogArgsForTests(args: unknown[]): void {
  scanLogArgs(args);
}

/** @internal test hook — force a DOM scan synchronously (bypasses the timer +
 *  throttle) and return the category noted, if any. */
export function _scanDomTextForTests(text: string): void {
  try {
    if (!armed) return;
    let t = (text ?? "").slice(0, DOM_SCAN_MAX);
    const isStack = looksLikeStack(t);
    t = "";
    if (isStack) note("stack");
  } catch {
    // ignore
  }
}
