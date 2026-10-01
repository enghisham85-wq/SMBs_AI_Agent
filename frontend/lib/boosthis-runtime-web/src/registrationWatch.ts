/**
 * "I said I was registering — did that ever happen?"
 *
 * WHY THIS EXISTS
 * ---------------
 * A live install printed the browser kit's startup line —
 *
 *   [boosthis] Boosthis starting: project key …1ed2. Registering next.
 *
 * — and then never registered, never appeared in `connection_status`, and never
 * said why. The developer only found out by asking Boosthis from the outside.
 *
 * The registration POST itself was never the problem: it speaks on every path
 * it owns (a thrown transport, a 400, a 401/403, an orphan marker, any other
 * status). The problem is that EVERY line the kit owns was emitted from inside
 * that round trip, and the observed failure was a round trip that never
 * happened:
 *
 *   - an error after the startup line took the whole registration with it;
 *   - the deferred consent was never scheduled (a stale persisted token) or
 *     never fired (the page went away first);
 *   - the runtime was locked, so `consent()` returned zero without a word;
 *   - the kill-switch handed back a locally-invented 204, which is `ok`, so a
 *     request that never left the browser was recorded as a REGISTRATION.
 *
 * Announcing an intention and then going quiet is worse than saying nothing,
 * because it reads as success. So the announcement is now a promise with a
 * deadline: the moment the startup line names a key, this module starts
 * watching, and within a bounded time either a registration has happened or the
 * kit says — on its own, unasked — that it did not.
 *
 * Never throws into the host page, and never keeps a process alive.
 */

import {
  hasSaidInactiveLine,
  setRegistrationRefusalKind,
  warnStayedInactiveOnce,
  type RegistrationFailure,
} from "./inactiveNotice";
import {
  getRegistrationState,
  markRegistrationNotSent,
} from "./registration";

/** Said once, and only after a registration that had already gone wrong later
 *  went right. Without it a developer who has read the failure line has no way
 *  to learn it cleared, and a tab that started offline looks broken forever. */
export const REGISTRATION_RECOVERED_LINE =
  "[boosthis] Boosthis registered after all. This app is on file with Boosthis now, and measurements from this page reach your dashboard from here on.";

/**
 * How long after the announcement the kit waits before saying it never got
 * there.
 *
 * Long enough to cover the tag's own 2s deferral of the first consent plus a
 * slow round trip on a bad connection, short enough that the answer arrives
 * while the developer is still looking at the console they just opened. Every
 * cause this deadline exists for (never scheduled, thrown away, refused before
 * sending) is decided within about three seconds; the rest of the budget is
 * slack for a genuinely slow network.
 */
const VERDICT_AFTER_MS = 12_000;
let verdictAfterMs = VERDICT_AFTER_MS;

/**
 * A request still in flight at the deadline is not silence, so the deadline
 * moves rather than accusing. Bounded, because a fetch that hangs forever must
 * still produce a sentence rather than an indefinitely deferred one.
 */
const MAX_EXTENSIONS = 2;

type PromiseState =
  /** The startup line never promised a registration on this page. */
  | "not-announced"
  /** Announced, and the answer is still legitimately outstanding. */
  | "waiting"
  /** Announced, and it never registered. The state this task exists to name. */
  | "unkept"
  /** Announced, and the server has this install on file. */
  | "kept";

let promiseState: PromiseState = "not-announced";
let timer: ReturnType<typeof setTimeout> | null = null;
let extensionsUsed = 0;
/** An attempt was started and has not come back yet. */
let attemptsInFlight = 0;
/** Any attempt at all was started on this page. Distinguishes "we asked and got
 *  nothing" from "we never asked", which is the whole point of the exercise. */
let anyAttemptStarted = false;
/** Something went wrong at least once — including the silent kinds. Read by the
 *  recovery line, which must never fire on a first-time clean registration. */
let sawTrouble = false;
/** The recovery line is said at most once per page. */
let saidRecovered = false;
/** The cause to name at the deadline when nothing sharper is known. */
let pendingCause: RegistrationFailure | null = null;
/** The deadline's last resort, installed by the reporting client. */
let probe: RegistrationProbe | null = null;
/** One probe per page: a second deadline never re-asks. */
let probed = false;

/**
 * "Nothing was sent — is this page registered anyway?"
 *
 * A repeat visit is the honest counter-example to this whole module: the kit
 * says "Registering next.", holds a credential from a previous session, and
 * deliberately does not register again. Accusing it would put a fault line on
 * every healthy returning page, which is worse than the silence it replaced.
 *
 * Only the server can tell that page apart from one holding a stale or foreign
 * token, so at the deadline the watch asks. Resolves TRUE when this page holds
 * a credential of its own (in which case an unanswered check means "cannot
 * tell", not a fault) and FALSE when it holds nothing.
 */
export type RegistrationProbe = () => Promise<boolean>;

/** Installed by the reporting client; the watch owns no endpoint of its own. */
export function setRegistrationProbe(fn: RegistrationProbe | null): void {
  probe = fn;
}

function later(fn: () => void, ms: number): ReturnType<typeof setTimeout> {
  const t = setTimeout(fn, ms);
  try {
    (t as unknown as { unref?: () => void }).unref?.();
  } catch {
    // Browsers have no unref; nothing to do.
  }
  return t;
}

function clear(): void {
  if (timer === null) return;
  try {
    clearTimeout(timer);
  } catch {
    // never throw into the host
  }
  timer = null;
}

/**
 * The startup line has just promised a registration ("Registering next.").
 *
 * Called from the announcement itself rather than from a caller, so both
 * browser entry points — the static tag and the bundler import — are covered by
 * construction: they say the same line through the same function.
 *
 * A page with no project key does NOT get here: its startup line promises no
 * registration, and the keyless backstop already explains that case.
 */
export function openRegistrationPromise(): void {
  try {
    if (promiseState !== "not-announced") return;
    promiseState = "waiting";
    clear();
    timer = later(() => {
      settle();
    }, verdictAfterMs);
  } catch {
    // A watch must never break the page it is watching.
  }
}

/** A registration attempt is about to be sent. */
export function noteRegistrationAttemptStarted(): void {
  try {
    anyAttemptStarted = true;
    attemptsInFlight += 1;
  } catch {
    /* never throw into the host */
  }
}

/** An attempt came back, whatever it said. */
export function noteRegistrationAttemptEnded(): void {
  try {
    if (attemptsInFlight > 0) attemptsInFlight -= 1;
  } catch {
    /* never throw into the host */
  }
}

/**
 * Nothing was sent: the activation lock or the kill-switch silenced the
 * registration before it left the page.
 *
 * `silent` is for the ENV kill-switch alone. That switch is a deliberate total
 * off-switch and the startup line already said so in its own badge clause —
 * repeating it as a fault would contradict the line the developer just read.
 * The state is still recorded either way, so the status readout and the panel
 * stay honest even when the console stays quiet.
 */
export function noteRegistrationNotSent(opts?: { silent?: boolean }): void {
  try {
    sawTrouble = true;
    markRegistrationNotSent();
    if (opts?.silent) {
      // Announced, explained by the startup line, and definitively not
      // registered. Nothing further to reconcile.
      promiseState = promiseState === "kept" ? "kept" : "unkept";
      clear();
      return;
    }
    pendingCause = "not-sent";
    say("not-sent");
  } catch {
    /* never throw into the host */
  }
}

/**
 * An error after the announcement threw the registration away, or the attempt
 * was never scheduled at all. Said immediately: there is nothing left running
 * that could turn this into a success on its own.
 */
export function noteRegistrationNeverAttempted(): void {
  try {
    sawTrouble = true;
    markRegistrationNotSent();
    pendingCause = "never-attempted";
    say("never-attempted");
  } catch {
    /* never throw into the host */
  }
}

/**
 * A round trip failed and said so in its own words.
 *
 * Recorded so a later success can announce the recovery — and, once the
 * developer has actually been told, the promise is closed here rather than at
 * the deadline: the announcement has been answered, and every surface should
 * say so immediately instead of showing "waiting" for another ten seconds.
 *
 * A path that fails WITHOUT speaking deliberately does not close anything. That
 * is the silence this module exists to catch, and the deadline must still fire
 * on it.
 */
export function noteRegistrationTrouble(): void {
  try {
    sawTrouble = true;
    if (!hasSaidInactiveLine()) return;
    promiseState = promiseState === "kept" ? "kept" : "unkept";
    clear();
  } catch {
    /* never throw into the host */
  }
}

/**
 * A verdict was reached AND the developer was told, in a sentence this watch
 * did not author (the invalid-install-id line is its own fixed wording).
 *
 * Closes the promise without adding a second, vaguer line. Kept deliberately
 * explicit rather than inferred from the registration state: a path that
 * reaches a definite negative and says nothing must still trip the deadline,
 * which is exactly the failure this whole module exists to catch.
 */
export function noteRegistrationExplained(): void {
  try {
    sawTrouble = true;
    promiseState = promiseState === "kept" ? "kept" : "unkept";
    clear();
  } catch {
    /* never throw into the host */
  }
}

/**
 * The server has this install on file. Closes the promise, and — only if
 * something had already gone wrong — says so once, so a tab that started
 * offline does not leave its failure line as the last word.
 */
export function noteRegistrationConfirmed(): void {
  try {
    promiseState = "kept";
    clear();
    if (!sawTrouble || saidRecovered) return;
    saidRecovered = true;
    try {
      console.info(REGISTRATION_RECOVERED_LINE);
    } catch {
      // A host that replaced console must never crash on a recovery line.
    }
  } catch {
    /* never throw into the host */
  }
}

/** Say the one line, and record the cause every other surface reads. */
function say(kind: RegistrationFailure, status = 0): void {
  // A kept promise is not re-opened by a later attempt that went nowhere.
  // Both verdicts were seen around one page — the success sequence, then the
  // inactive line again on a later startup attempt — with nothing telling a
  // reader which was current. The earlier answer stands, and the stale one is
  // simply not said.
  if (promiseState === "kept" || getRegistrationState() === "registered") {
    promiseState = "kept";
    clear();
    return;
  }
  // Everything that could still be kept returned above.
  promiseState = "unkept";
  clear();
  try {
    // Only claim the shared refusal slot when nothing sharper already holds it:
    // a 401 that named a revoked key must not be relabelled by this watch.
    if (!hasSaidInactiveLine()) setRegistrationRefusalKind(kind);
    warnStayedInactiveOnce(kind, status);
  } catch {
    /* never throw into the host */
  }
}

/** The deadline. Decide, from what the page actually observed, whether the
 *  announcement was kept — and if not, why not. */
function settle(): void {
  try {
    timer = null;
    if (promiseState !== "waiting") return;
    if (getRegistrationState() === "registered") {
      promiseState = "kept";
      return;
    }
    if (attemptsInFlight > 0 && extensionsUsed < MAX_EXTENSIONS) {
      // Still asking. Waiting longer is honest; accusing is not.
      extensionsUsed += 1;
      timer = later(() => {
        settle();
      }, verdictAfterMs);
      return;
    }
    if (hasSaidInactiveLine()) {
      // A round trip already explained this page's failure in its own words.
      // The promise is closed; adding a second line would only dilute it.
      promiseState = "unkept";
      sawTrouble = true;
      return;
    }
    if (!anyAttemptStarted && probe !== null && !probed) {
      probed = true;
      askThenDecide(probe);
      return;
    }
    decide(false);
  } catch {
    /* never throw into the host */
  }
}

/** Run the probe, then decide on its answer — or on the deadline, whichever
 *  comes first. A probe that hangs must never swallow the verdict. */
function askThenDecide(fn: RegistrationProbe): void {
  let held = false;
  let done = false;
  const finish = (): void => {
    if (done) return;
    done = true;
    decide(held);
  };
  try {
    void Promise.resolve(fn()).then(
      (answer) => {
        held = answer === true;
        finish();
      },
      () => {
        finish();
      },
    );
  } catch {
    finish();
    return;
  }
  timer = later(finish, verdictAfterMs);
}

/**
 * The verdict itself.
 *
 * `heldCredential` says this page already holds a credential from an earlier
 * registration. It only matters in the third case: with no answer from the
 * server AND local evidence of a past success, the honest reading is "cannot
 * tell", so the watch says nothing and the promise stays openly outstanding —
 * which is what every surface then shows. Inventing a fault there would put a
 * red line on a healthy returning visitor.
 */
function decide(heldCredential: boolean): void {
  try {
    clear();
    if (promiseState !== "waiting") return;
    const state = getRegistrationState();
    if (state === "registered") {
      promiseState = "kept";
      return;
    }
    if (state !== "unregistered" && heldCredential) return;
    // Nothing has been said, and there is no registration. Name what the page
    // actually saw: an attempt that went out and never came back reads as
    // unreachable; no attempt at all is the announced-then-nothing case.
    sawTrouble = true;
    const cause: RegistrationFailure =
      pendingCause ?? (anyAttemptStarted ? "network" : "never-attempted");
    say(cause);
  } catch {
    /* never throw into the host */
  }
}

/**
 * What became of the announcement — the fact the status readout needs to tell
 * "this page announced a registration and never got one" apart from "Boosthis
 * never started here" and "registered and measuring". Never throws.
 */
export function getRegistrationPromiseState(): PromiseState {
  try {
    if (promiseState === "waiting" && getRegistrationState() === "registered") {
      return "kept";
    }
    return promiseState;
  } catch {
    return "not-announced";
  }
}

/** @internal test hook — shorten the deadline so behaviour can be proven. */
export function _setRegistrationVerdictMsForTests(ms: number): void {
  verdictAfterMs = ms;
}

/** @internal test hook — forget the promise, its deadline and every latch. */
export function _resetRegistrationWatchForTests(): void {
  clear();
  promiseState = "not-announced";
  extensionsUsed = 0;
  attemptsInFlight = 0;
  anyAttemptStarted = false;
  sawTrouble = false;
  saidRecovered = false;
  pendingCause = null;
  probe = null;
  probed = false;
  verdictAfterMs = VERDICT_AFTER_MS;
}
