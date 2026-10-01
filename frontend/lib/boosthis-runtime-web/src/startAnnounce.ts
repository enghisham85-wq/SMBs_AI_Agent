/** The two ungated lines every Boosthis kit says about switching itself on.
 *
 * WHY THIS EXISTS
 * A kit that never started is perfectly silent: it draws nothing, contacts
 * nobody and appears nowhere — which is indistinguishable from a kit that is
 * running quietly by choice. A real customer install was misdiagnosed for two
 * days on exactly that ambiguity (sandboxes and key rotations were blamed; the
 * start call had simply never run).
 *
 * So every kit now says ONE short line the moment its start path begins:
 *
 *   [boosthis] Boosthis starting: project key ...1a2b. Registering next.
 *
 * That line is the anchor for every diagnosis. No line means the kit never
 * started, whatever anyone else believes. It is never silenced by a quiet
 * mode, a privacy setting or a switched-off badge, because it reveals nothing
 * about the app: a four-character tail of a key the developer already holds.
 *
 * The second line refuses a value that cannot be a project key AT THE POINT IT
 * IS SUPPLIED. The same install pasted `Bearer bk_...` (the way an HTTP header
 * shows a key) and the failure surfaced three layers away as a refused
 * registration with no explanation. Nothing is ever stripped or repaired
 * silently — a wrong value is reported and refused.
 *
 * The wording is duplicated verbatim in every kit ON PURPOSE (no server prose
 * ever renders inside a host app) and a cross-runtime guard greps each phrase
 * as a contiguous literal. See .agents/memory/kit-startup-announcement.md.
 */

import {
  openRegistrationPromise,
  _resetRegistrationWatchForTests,
} from "./registrationWatch";

/** The four things a supplied value can be wrong in. Closed set: a kit must
 *  never invent a fifth explanation. */
export type ProjectKeyProblem =
  | "authWord"
  | "quoted"
  | "cutOff"
  | "wrongShape";

/** Authorization words an HTTP header shows in front of a key. Matched only at
 *  the very start, followed by whitespace. */
const AUTH_WORDS = ["bearer", "token", "basic", "apikey", "api-key", "key"];

/** Quote-ish characters a paste can wrap a key in (including the guide's own
 *  `<...>` placeholder brackets). */
const QUOTE_CHARS = ['"', "'", "`", "\u2018", "\u2019", "\u201c", "\u201d"];

/** A project key is one unbroken run of these characters. */
const KEY_CHARS = /^[A-Za-z0-9_-]+$/;

/**
 * Name what is wrong with a supplied project key, or null when it could be a
 * real one. Deliberately conservative: it refuses only values that CANNOT be a
 * key, never values that merely look unfamiliar (env-configured keys predate
 * the `bk_` prefix and must keep working).
 */
export function findProjectKeyProblem(
  raw: unknown,
): ProjectKeyProblem | null {
  if (typeof raw !== "string") return null;
  const value = raw.trim();
  if (!value) return null; // "absent" is a different condition, handled elsewhere
  const lower = value.toLowerCase();
  for (const word of AUTH_WORDS) {
    if (lower.startsWith(word) && /\s/.test(value.charAt(word.length))) {
      return "authWord";
    }
  }
  const first = value.charAt(0);
  const last = value.charAt(value.length - 1);
  if (
    QUOTE_CHARS.indexOf(first) >= 0 ||
    QUOTE_CHARS.indexOf(last) >= 0 ||
    (first === "<" && last === ">")
  ) {
    return "quoted";
  }
  // Only an ELLIPSIS is honest evidence of truncation. A key may be any
  // length (env-configured keys predate the `bk_` prefix entirely), so a
  // short-but-plausible value must never be refused as "cut off" — being
  // shorter than a key can be is a shape problem, checked below.
  if (
    value.endsWith("...") ||
    value.endsWith("\u2026") ||
    value.startsWith("...") ||
    value.startsWith("\u2026")
  ) {
    return "cutOff";
  }
  if (!KEY_CHARS.test(value) || value.length < 8) return "wrongShape";
  return null;
}

/** The sentence, per problem. Kit-authored literals, identical in every kit. */
function problemSentence(problem: ProjectKeyProblem): string {
  switch (problem) {
    case "authWord":
      return "it starts with an authorization word. Paste the key on its own, with nothing in front of it.";
    case "quoted":
      return "it is wrapped in quotes. Paste the key on its own, with no quotes around it.";
    case "cutOff":
      return "it looks cut off. Paste the whole key from your Boosthis Setup page.";
    case "wrongShape":
      return "it is not the shape of a project key. A project key is one unbroken value from your Boosthis Setup page.";
  }
}

/**
 * The full refusal line. `source` names the attribute, environment variable or
 * setting the value came from, so the developer knows where to look.
 */
export function projectKeyRefusalLine(
  problem: ProjectKeyProblem,
  source: string,
): string {
  return `[boosthis] Boosthis will not use the project key from ${source}: ${problemSentence(problem)}`;
}

/** Last four characters of a key — the only part of it that is ever printed. */
export function projectKeyTail(key: unknown): string | null {
  if (typeof key !== "string") return null;
  const value = key.trim();
  if (!value) return null;
  return value.length >= 4 ? value.slice(-4) : value;
}

/** Why the badge will not be on the page, when it will not. */
export type BadgeState = "visible" | "hidden-by-setting" | "switched-off";

/** The startup line itself. Exported for tests and for the guides. */
export function kitStartupLine(
  tail: string | null,
  badge: BadgeState = "visible",
): string {
  const head = tail
    ? `[boosthis] Boosthis starting: project key ...${tail}. Registering next.`
    : "[boosthis] Boosthis starting: no project key. Nothing will register.";
  if (badge === "hidden-by-setting") {
    return `${head} Badge hidden by a setting; Boosthis is still running.`;
  }
  if (badge === "switched-off") {
    return `${head} Badge hidden: Boosthis is switched off.`;
  }
  return head;
}

// ── one line per page load ────────────────────────────────────────────────

let announced = false;
let pendingBadge: BadgeState | null = null;
let pendingTimer: ReturnType<typeof setTimeout> | null = null;
let announcementPending = false;
const refused = new Set<string>();
const heldRefusals: string[] = [];

function say(line: string): void {
  try {
    // Deliberately console.info, not warn: this line is evidence, not a
    // complaint, and a healthy install prints it too.
    console.info(line);
  } catch {
    // A host that replaced console must never crash because of our line.
  }
}

/**
 * Say the startup line — and, when it named a key, open the promise it just
 * made.
 *
 * "Registering next." is a claim about the future, and nothing ever went back
 * to check it: a page could announce a registration, fail in any of half a
 * dozen ways that live outside the registration round trip, and never say
 * another word. Announcing an intention and then going quiet reads as success,
 * which is worse than saying nothing at all.
 *
 * Opened HERE rather than at the call sites so it cannot drift: both browser
 * entry points — the one-line tag and the bundler import — reach the console
 * through this function, so both are covered by construction.
 *
 * A keyless line ("Nothing will register.") promises nothing, so it opens
 * nothing; that case has its own backstop.
 */
function sayStartupLine(tail: string | null, badge: BadgeState): void {
  say(kitStartupLine(tail, badge));
  if (!tail) return;
  try {
    openRegistrationPromise();
  } catch {
    /* the watch must never break the line it watches */
  }
}

function sayRefusal(line: string): void {
  try {
    console.warn(line);
  } catch {
    /* a host that replaced console must not crash on a warning */
  }
}

/**
 * Mark the start path as begun, BEFORE a single key is read.
 *
 * From here until the line is actually said, a refusal is held back and
 * released immediately afterwards. Without this the first thing a developer
 * sees can be a complaint about a key — which is precisely the reading order
 * that cost one install two days, because a complaint about a key implies the
 * kit is running when it may not be. The anchor line comes first, always.
 *
 * Holding is strictly bounded: a refusal is only ever queued while an
 * announcement is pending, and every path that announces drains the queue.
 */
export function beginStartAnnouncement(): void {
  if (announced) return;
  announcementPending = true;
}

/**
 * Say a developer-facing complaint, but never ahead of the startup line. Used
 * by the one-line tag, whose objections (a missing key, a bad install id, a
 * refused value) are produced by the parser before the kit is started.
 */
export function sayAfterStartupLine(line: string): void {
  if (announcementPending && !announced) {
    heldRefusals.push(line);
    return;
  }
  sayRefusal(line);
}

/** Print anything held back. Called the instant the line is said, and by the
 *  start path if it fails before getting that far. */
export function flushHeldRefusals(): void {
  announcementPending = false;
  if (heldRefusals.length === 0) return;
  const lines = heldRefusals.splice(0, heldRefusals.length);
  for (const line of lines) sayRefusal(line);
}

function flushPending(): void {
  if (announced || pendingBadge === null) return;
  const badge = pendingBadge;
  pendingBadge = null;
  pendingTimer = null;
  announced = true;
  sayStartupLine(null, badge);
  flushHeldRefusals();
}

/**
 * Say the startup line. Called at the very top of `startWebVitals` — before
 * the key is resolved, before any registration, ahead of the measurement APIs
 * — so it appears wherever the badge would.
 *
 * The browser runtime is the one kit whose key can arrive AFTER its start
 * call: the module install is documented as `startWebVitals()` then
 * `enableTelemetry({ inviteKey })` on the next line. So when no key is known
 * yet the line waits one turn of the event loop for `enableTelemetry` to
 * supply one, rather than committing to a "no project key" the install does
 * not deserve. Exactly one line is printed either way.
 */
export function announceKitStart(
  key: string | null | undefined,
  badge: BadgeState,
): void {
  if (announced || pendingBadge !== null) return;
  const tail = projectKeyTail(key);
  if (tail) {
    announced = true;
    sayStartupLine(tail, badge);
    flushHeldRefusals();
    return;
  }
  pendingBadge = badge;
  try {
    pendingTimer = setTimeout(flushPending, 0);
  } catch {
    flushPending();
  }
}

/** The key the reporting client resolved. Fills in a start line that is still
 *  waiting for one; a no-op once the line has been said. */
export function announceProjectKey(key: string | null | undefined): void {
  if (announced || pendingBadge === null) return;
  const tail = projectKeyTail(key);
  if (!tail) return;
  const badge = pendingBadge;
  pendingBadge = null;
  if (pendingTimer !== null) {
    try {
      clearTimeout(pendingTimer);
    } catch {
      /* nothing to do */
    }
    pendingTimer = null;
  }
  announced = true;
  sayStartupLine(tail, badge);
  flushHeldRefusals();
}

/**
 * Refuse a supplied value once, naming where it came from. Returns true when
 * the value was refused (the caller must then behave as if no key was given —
 * never as if a repaired one was).
 */
export function warnProjectKeyRefused(raw: unknown, source: string): boolean {
  const problem = findProjectKeyProblem(raw);
  if (!problem) return false;
  if (!refused.has(source)) {
    refused.add(source);
    const line = projectKeyRefusalLine(problem, source);
    // Never ahead of the anchor line: a refusal read first implies the kit is
    // running, which is the one thing it cannot vouch for.
    if (announcementPending && !announced) heldRefusals.push(line);
    else sayRefusal(line);
  }
  return true;
}

/** @internal test hook. Also clears the registration promise this module
 *  opens, so a deadline armed by one test can never speak during another. */
export function _resetStartAnnounceForTests(): void {
  _resetRegistrationWatchForTests();
  announced = false;
  pendingBadge = null;
  if (pendingTimer !== null) {
    try {
      clearTimeout(pendingTimer);
    } catch {
      /* nothing to do */
    }
  }
  pendingTimer = null;
  announcementPending = false;
  heldRefusals.length = 0;
  refused.clear();
}
