/**
 * The one sentence a browser kit is allowed to say when it did not register —
 * and the single place its wording comes from.
 *
 * WHY THIS IS ITS OWN MODULE
 * --------------------------
 * This wording, and the one-line-per-page latch around it, used to live inside
 * the telemetry client — which meant only code INSIDE a registration round trip
 * could reach it. That is exactly how the browser kit came to announce
 * "Registering next." and then say nothing at all: the failures that produce
 * that silence (an attempt never scheduled, an attempt thrown away by an error,
 * an attempt refused before anything was sent) all happen OUTSIDE the round
 * trip, where the sentence was unreachable.
 *
 * So the sentence lives here, on its own, importable by anything: the client,
 * the tag's error handler, and the watch that reconciles the promise the
 * startup line makes. There is still exactly ONE line per page load, whatever
 * combination of paths reaches it.
 *
 * It never carries the project key, an endpoint, an install id, or any server
 * text — only a coarse, code-defined reason.
 */

/** Coarse failure kinds for the one-shot inactive line. Derived from the HTTP
 *  status + the frozen wire markers only — never from server prose. */
export type RegistrationFailure =
  /** No HTTP answer at all: status 0 / a thrown transport (DNS, TLS, egress,
   *  a broken certificate store, a blocking proxy). Also covers an attempt
   *  that was sent and never came back at all. */
  | "network"
  /** The orphan dead end after rotation could not recover it: this install id
   *  already belongs to another copy and this copy holds none of its
   *  credentials. */
  | "orphan"
  /** The project key was deliberately cut off (an `invite_key_revoked`
   *  marker). Permanent — nothing but a new key recovers it. */
  | "revoked"
  /** The account behind the key has a billing problem, so the key is paused (a
   *  `plan_required` or `account_closure_pending` marker). Nothing was revoked
   *  and no rebuild is needed: settling the account resumes the SAME key. Kept
   *  apart from "revoked" because the two need opposite actions. */
  | "paused"
  /** The key matched nothing on file (an `invite_key_unknown` marker) — almost
   *  always a typo, a half-paste or a key from another project. Kept apart from
   *  "revoked" because the key the developer meant is still perfectly good;
   *  telling them it was revoked sends them to mint a replacement for nothing.
   *
   *  There is deliberately no "replaced" kind and never will be: the server
   *  follows a rotation chain, so a replaced key still works. */
  | "unknown"
  /** The project key was not accepted (401 / 403) with no sharper marker. */
  | "key"
  /** No project key is wired into this page at all, and nobody asked for that. */
  | "no-key-missing"
  /** No project key, deliberately — the page was told to run keyless. */
  | "no-key-chosen"
  /** The registration was never SENT: the activation lock or the kill-switch
   *  silenced it, so the request never left the browser. Distinct from every
   *  kind above because none of them happened — there is no server answer here
   *  of any sort, and until this kind existed the locally-invented 204 that
   *  stands in for the missing send was read as a successful registration. */
  | "not-sent"
  /** The page announced a registration and then never got as far as asking:
   *  the deferred consent never fired, or an error after the announcement took
   *  the whole registration with it. The state the startup line's promise was
   *  left in when nobody was watching. */
  | "never-attempted"
  /** Anything else the server refused. */
  | "other";

/** [why, what-to-do] for the one-shot line, mirroring the Ruby kit's
 *  `registration_failure_hint` in substance. `status` is only interpolated for
 *  the "other" case (a coarse, code-defined HTTP number — never server text). */
export function registrationFailureHint(
  kind: RegistrationFailure,
  status: number,
): [string, string] {
  switch (kind) {
    case "network":
      // The whole point of this line: a broken certificate store / blocked
      // egress shows up here, and without it the developer is left guessing.
      // The web runtime keeps no last-error record (no Safe.recent_errors
      // equivalent), so the parenthetical detail the Ruby kit adds is omitted.
      return [
        "it could not reach the Boosthis API from this process",
        "Check outbound HTTPS and certificate trust from this app, then reload.",
      ];
    case "orphan":
      // The one case that reads like success on the wire (HTTP 200), and the
      // install still cannot upload. The web runtime takes no install id from
      // the environment — it is passed to enableTelemetry() — so the fix names
      // that argument rather than an env var.
      return [
        "this install id already belongs to another copy of the app, and this copy holds none of its credentials",
        "Give this copy its own fresh UUID for enableTelemetry({ installId }), then reload.",
      ];
    case "revoked":
      return [
        "its project key has been revoked",
        "A revoked key never works again. Mint a new project key in your Boosthis dashboard, put it in this app, then reload.",
      ];
    case "paused":
      return [
        "its project key is paused by a billing problem on the account",
        "Settle the account in your Boosthis dashboard and the same key starts working again. Nothing has been revoked.",
      ];
    case "unknown":
      return [
        "its project key was not recognised",
        "Check it is the current key, pasted whole, with no stray spaces, and that it belongs to this project.",
      ];
    case "no-key-missing":
      return [
        "no project key is wired into this app",
        "Without one this app measures itself and never appears on your Boosthis dashboard. Copy a project key from your Boosthis Setup page into this app, then reload.",
      ];
    case "no-key-chosen":
      return [
        "this app is set to run with no project key",
        "That is a deliberate setting: this app measures itself and never appears on your Boosthis dashboard. Nothing is being sent.",
      ];
    case "key":
      return [
        "its project key wasn't accepted",
        "Copy a current project key from your Boosthis dashboard into this app, then reload.",
      ];
    case "not-sent":
      // Nothing was sent, so nothing can be inferred about the key, the
      // network or the server — and the fix is the lock, not the install.
      return [
        "this page is locked, so the registration was never sent",
        "This app keeps measuring itself, and nothing measured on this page has been sent to Boosthis. Open your Boosthis dashboard to check whether this project is paused or its key was revoked, then reload.",
      ];
    case "never-attempted":
      // The reported bug, said out loud: the startup line promised a
      // registration and no attempt has been made yet.
      //
      // WORDED AS "NOT YET", DELIBERATELY. The deadline behind this line is
      // ~36 seconds; the retry ladder that actually resolves it runs for
      // minutes, and a real install recovered on its own about three minutes
      // in — with this line already copied into a test report as a failure.
      // Everything final was therefore taken out: the kit says what IT did,
      // gives the horizon it is still working to, and sends the reader to the
      // one place that can settle it instead of to their own startup code.
      return [
        "it said it was registering and has not got as far as asking yet",
        "This app keeps measuring itself, and nothing measured on this page has been sent to Boosthis. This is not a final answer: the kit keeps trying on its own, and a page that only registers three or four minutes in is normal, so give it about ten minutes. Only Boosthis can settle it — check this project's connection on your Boosthis dashboard rather than changing the install id, the project key or the wiring. If this line is still the last word after that, look for an error thrown by this page during startup.",
      ];
    default:
      return [
        `the server refused the registration (HTTP ${status})`,
        "Retry later; if it keeps happening, check the project key in your Boosthis dashboard.",
      ];
  }
}

/**
 * How long the kit keeps chasing a registration on its own before a developer
 * should treat the failure as real.
 *
 * Not a guess: it is the span of the consent chase ladder in the client
 * (15s + 45s + 2m + 5m ≈ 8 minutes to the last rung), rounded up to a round
 * number a person can hold. The observed real-world recovery — a live install
 * that registered on its own with nobody touching the page — landed about
 * three minutes in, comfortably inside it. A horizon shorter than the
 * mechanism that settles the question is how a healthy install gets written
 * up as a broken one, so this number and the ladder are held together by a
 * test rather than by a comment.
 */
export const REGISTRATION_RETRY_HORIZON_MS = 10 * 60 * 1000;

/**
 * Does the kit keep trying on its own after this kind, or is this as far as it
 * gets without somebody changing something?
 *
 * The distinction the reported bug turned on. "Not registered yet, still
 * trying" and "not registered, and here is what stopped it" were the same
 * state on every surface, so a verdict taken 36 seconds in — while the retry
 * ladder still had minutes to run — read as final and went into a report as a
 * failure. A kind listed here is one the kit's own consent ladder can still
 * turn into a success with nobody touching the page.
 *
 * Kept in this module because the wording and the finality are one decision:
 * a cause worded "not yet" must be a cause that is still being chased.
 */
export function registrationKeepsTrying(
  kind: RegistrationFailure | null,
): boolean {
  return kind === "never-attempted" || kind === "network" || kind === "other";
}

/**
 * The server's own answer, mirrored here so no surface can contradict it.
 *
 * Two verdicts were observed around ONE page: the success sequence, and then —
 * on a later startup attempt in the same session — the inactive line again,
 * with nothing telling a reader which was current. Once Boosthis has this
 * install on file, an inactive line is not a second opinion, it is a stale
 * one: it is not printed, and the held sentence stops being handed to the
 * panel and the readout.
 *
 * Pushed in by registration.ts (rather than read from it) so this module keeps
 * importing nothing at all — every path that reaches the sentence, including
 * the tag's error handler, already reaches it from inside a cycle-free leaf.
 */
let registrationOnFile = false;

/** @internal mirrors the four-state machine's public verdict: true only while
 *  Boosthis has said yes. A later definite negative flips it back, so a key
 *  revoked mid-session can still be explained. */
export function markRegistrationOnFile(onFile: boolean): void {
  registrationOnFile = onFile;
}

/** One-shot guard so a registration that did not go through prints exactly ONE
 *  line per page — never on every consent retry, and never more than one line
 *  even if the cause changes between attempts. The browser/Web runtime has no
 *  locked-screen UI (unlike RN), so this console line is the parity hint that
 *  tells the developer WHY Boosthis stayed inactive.
 *
 *  It is NOT gated on any debug flag: a debug flag only helps the developer
 *  who already suspects the kit, and this exists for the one who does not. A
 *  kit that could not register looks identical to one that is working — same
 *  quiet page, same absent numbers — so the developer hunts in the wrong place
 *  for hours (a broken certificate store is exactly the case that cost a real
 *  install ~19 minutes). One line, once, is the whole budget. */
let warnedRegistrationRejected = false;

/** The last reason a registration did not go through, in this kit's own fixed
 *  vocabulary — never server text. Held here (rather than passed around) so the
 *  panel and the status readout can show the SAME sentence the console line
 *  said, instead of the generic "not registered" shrug that hid the difference
 *  between a revoked key, a paused account and a typo. Null while nothing has
 *  gone wrong. */
let registrationRefusalKind: RegistrationFailure | null = null;

/** Print the one-shot "Boosthis stayed inactive" line for a registration that
 *  did not go through. At most one line per page, whatever the cause. Never
 *  throws into the host (a host that replaced `console` must not crash on a
 *  warning). */
export function warnStayedInactiveOnce(
  kind: RegistrationFailure,
  status = 0,
): void {
  // Boosthis has this install on file, and this is one of the causes the kit
  // was still chasing — a detail of one round trip, not a verdict on the
  // install. Printing it would leave two contradictory lines in one console
  // with nothing saying which is current.
  //
  // Only the still-chased causes are dropped. A DEFINITE refusal arriving
  // after a success (a key revoked mid-session, an id that turns out to
  // belong to another copy) is the server's own newer answer, and silencing
  // that would be the same fault in the other direction.
  if (registrationOnFile && registrationKeepsTrying(kind)) return;
  if (warnedRegistrationRejected) return;
  warnedRegistrationRejected = true;
  const [why, fix] = registrationFailureHint(kind, status);
  try {
    console.warn(`[boosthis] Boosthis stayed inactive because ${why}. ${fix}`);
  } catch {
    // A host that replaced console must never crash because of a warning.
  }
}

/** Has this page already spent its one line? The watch that reconciles the
 *  startup line's promise asks before speaking, so a failure the round trip
 *  already explained is never explained twice in different words. */
export function hasSaidInactiveLine(): boolean {
  return warnedRegistrationRejected;
}

export function getRegistrationRefusalKind(): RegistrationFailure | null {
  return registrationRefusalKind;
}

/** The full `{why}. {fix}` sentence for the current refusal, or null when
 *  nothing has gone wrong. This is the ONE place the wording comes from, so the
 *  console line, the panel notice and the status readout can never drift apart.
 *  Never throws — a display surface must not be able to break the host. */
export function getRegistrationRefusalSentence(): string | null {
  try {
    if (registrationRefusalKind === null) return null;
    // Superseded: Boosthis has this install on file, and this sentence only
    // ever said the kit had not got there YET. A surface that kept showing it
    // would be the second of two contradictory verdicts standing around one
    // page. A definite refusal still speaks — see warnStayedInactiveOnce.
    if (registrationOnFile && registrationKeepsTrying(registrationRefusalKind)) {
      return null;
    }
    const [why, fix] = registrationFailureHint(registrationRefusalKind, 0);
    return `Boosthis stayed inactive because ${why}. ${fix}`;
  } catch {
    return null;
  }
}

/** @internal set by the registration paths as they classify a refusal. */
export function setRegistrationRefusalKind(kind: RegistrationFailure): void {
  registrationRefusalKind = kind;
}

/** Test-only: forget the one-shot latch and the held refusal. */
export function _resetInactiveNoticeForTests(): void {
  warnedRegistrationRejected = false;
  registrationRefusalKind = null;
  registrationOnFile = false;
}
