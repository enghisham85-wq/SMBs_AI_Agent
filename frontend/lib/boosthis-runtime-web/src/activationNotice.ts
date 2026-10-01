/**
 * What the kit says while it waits for its FIRST confirmation from Boosthis —
 * the one state in which a perfectly installed kit draws nothing at all.
 *
 * WHY (observed on a live customer project, Aug 2026): a first-ever visitor
 * holds no credential, so the anti-copy ACTIVATION LOCK keeps the badge hidden
 * until the server confirms this install. That confirmation took 22 minutes,
 * and for the whole of the developer's first sitting the page said nothing:
 * the startup line ends on "Registering next." and is then followed by
 * silence. An empty corner with no explanation reads as a broken install — the
 * developer reported no badge, and the AI helping them invented a cause
 * (blamed the preview environment) and edited their startup path. Waiting is a
 * legitimate state. Being silent about it is not.
 *
 * The budget is TWO lines per page load, and only for a wait long enough to be
 * worth a word: one when the wait becomes real, one for how it ended. Both are
 * ungated `console.info` — a privacy setting never silences them, because they
 * reveal nothing but the state of the install itself. They are skipped
 * entirely when the badge was switched off by a setting, since the startup
 * line has already explained that empty corner and a second, contrary
 * explanation is worse than none (the same "never contradict the badge" rule
 * the startup line obeys).
 */
import {
  hasInstallCredential,
  isActivated,
  isRuntimeInert,
  subscribeEntitlement,
} from "./killSwitch";
import { isBoosthisDisabled } from "./runtimeFlags";
import type { BadgeState } from "./startAnnounce";

/**
 * Kit-owned literals, duplicated verbatim in every runtime. A cross-kit guard
 * greps them as CONTIGUOUS text, so never split one across a concatenation or
 * a line break.
 */
export const AWAITING_ACTIVATION_LINE =
  "[boosthis] Registered. Waiting for Boosthis to confirm this install: until it does, nothing is measured and no badge is drawn.";
export const ACTIVATION_CONFIRMED_LINE =
  "[boosthis] Boosthis confirmed this install and the badge is up. That only means measuring is allowed: a reading is taken when a page view or screen finishes.";
export const ACTIVATION_STILL_WAITING_LINE =
  "[boosthis] Boosthis has still not confirmed this install. It keeps asking in the background, and the badge appears the moment it does.";
export const ACTIVATION_LOCKED_LINE =
  "[boosthis] Boosthis answered for this install, but this project is locked, so nothing is measured. Open your Boosthis dashboard to see why.";

/**
 * How long the badge may be missing before its absence is worth explaining.
 * The ordinary case — confirmed within a second or two of start — stays
 * completely silent: the badge appears, which is its own answer, and narrating
 * a wait nobody noticed is noise.
 */
const ANNOUNCE_AFTER_MS = 2_500;

/**
 * When to say how the wait ended if no confirmation ever arrives. Longer than
 * the confirmation chase in `killSwitch`, so the verdict lands after the chase
 * has genuinely tried rather than while it is still working: that ladder's
 * delays are cumulative (1.5 + 3 + 6 + 12 + 24 = 46.5s from start), so a
 * verdict at 45s would have announced a give-up while the last knock was still
 * in flight — the one knock most likely to be the one that succeeds.
 */
const STILL_WAITING_AFTER_MS = 50_000;

let watching = false;
let spoke = false;
let settled = false;
let waitStartedAt = 0;
let unsubscribe: (() => void) | null = null;
let graceTimer: ReturnType<typeof setTimeout> | null = null;
let verdictTimer: ReturnType<typeof setTimeout> | null = null;

function say(line: string): void {
  try {
    // console.info, not warn: waiting is a state, not a fault.
    console.info(line);
  } catch {
    // A host that replaced console must never crash because of our line.
  }
}

function later(
  ms: number,
  fn: () => void,
): ReturnType<typeof setTimeout> | null {
  try {
    const t = setTimeout(fn, ms);
    // Never keep a Node-side entrypoint alive just to say a line.
    (t as unknown as { unref?: () => void }).unref?.();
    return t;
  } catch {
    return null;
  }
}

function clear(t: ReturnType<typeof setTimeout> | null): null {
  if (t !== null) {
    try {
      clearTimeout(t);
    } catch {
      /* nothing to do */
    }
  }
  return null;
}

function stopWatching(): void {
  if (unsubscribe) {
    try {
      unsubscribe();
    } catch {
      /* a listener registry that throws must not break the kit */
    }
    unsubscribe = null;
  }
  graceTimer = clear(graceTimer);
  verdictTimer = clear(verdictTimer);
}

/**
 * How the wait stands right now, read from the gate itself.
 *
 * "locked" matters: the FIRST answer this install ever gets may be a refusal
 * (revoked, unpaid, paused, tampered). That answer ends the wait — the server
 * has spoken — but the kit stays inert, so calling it "confirmed" would claim
 * measuring had started when nothing is being measured at all.
 */
type WaitOutcome = "confirmed" | "locked" | "waiting" | "off";

function outcomeNow(): WaitOutcome {
  try {
    if (isBoosthisDisabled()) return "off";
    if (!isActivated()) return "waiting";
    return isRuntimeInert() ? "locked" : "confirmed";
  } catch {
    return "waiting"; // cannot read the gate → never claim an answer arrived
  }
}

/** Say how the wait ended — but only if the wait was ever announced. */
function settle(outcome: WaitOutcome): void {
  if (settled) return;
  settled = true;
  stopWatching();
  if (!spoke) return;
  // A total off-switch stays silent by contract, even mid-wait.
  if (outcome === "off") return;
  say(
    outcome === "confirmed"
      ? ACTIVATION_CONFIRMED_LINE
      : outcome === "locked"
        ? ACTIVATION_LOCKED_LINE
        : ACTIVATION_STILL_WAITING_LINE,
  );
}

/** The grace has expired: is the badge still missing? */
function announceWaitIfStillMissing(): void {
  graceTimer = null;
  if (settled) return;
  const outcome = outcomeNow();
  if (outcome !== "waiting") {
    // The wait ended before it was worth mentioning — and because nothing was
    // announced, settling here says nothing at all.
    settle(outcome);
    return;
  }
  // The line about to be said begins "Registered." — so it may only be said by
  // an install that really did register. A kit still holding no credential is
  // in a DIFFERENT state, and that state already has its own single line; two
  // contradictory explanations of one empty corner are worse than one. So keep
  // looking on the same cadence, and if the credential never arrives, leave
  // the whole story to the line that owns it.
  if (!hasInstallCredential()) {
    const elapsed = Date.now() - waitStartedAt;
    if (elapsed >= STILL_WAITING_AFTER_MS - ANNOUNCE_AFTER_MS) {
      stopWatching();
      return;
    }
    graceTimer = later(ANNOUNCE_AFTER_MS, announceWaitIfStillMissing);
    if (graceTimer === null) stopWatching();
    return;
  }
  spoke = true;
  say(AWAITING_ACTIVATION_LINE);
  try {
    // Subscribing can answer the question inside this very call: the first
    // subscriber triggers a lazy read of the cached verdict, which may settle
    // the wait before the unsubscribe handle exists. Adopt the handle only if
    // we are still waiting; otherwise release it here and leave nothing behind.
    const off = subscribeEntitlement(() => {
      const now = outcomeNow();
      // Anything but "still waiting" is an answer: say how it ended.
      if (now !== "waiting") settle(now);
    });
    if (settled) {
      try {
        off();
      } catch {
        /* a listener registry that throws must not break the kit */
      }
    } else {
      unsubscribe = off;
    }
  } catch {
    unsubscribe = null; // no subscription → the timer below still answers
  }
  // Never arm a give-up timer for a wait that is already over.
  if (settled) return;
  // Whatever is left of the horizon: the wait may have been running quietly
  // for a while before the credential arrived, and the verdict belongs at a
  // fixed distance from the START of the wait, not from the moment we spoke.
  const remaining = Math.max(0, STILL_WAITING_AFTER_MS - (Date.now() - waitStartedAt));
  verdictTimer = later(remaining, () => {
    verdictTimer = null;
    settle(outcomeNow());
  });
}

/**
 * Start watching for a wait worth explaining. Called from `startWebVitals`
 * beside the bubble mount, with the badge state the startup line just
 * announced.
 *
 * Says nothing at all when: the badge was switched off by a setting (the
 * startup line covers it), Boosthis is disabled outright (a silent, total off
 * switch by contract), this install is already confirmed (the overwhelmingly
 * common case — a returning visitor whose badge appears immediately), or the
 * confirmation lands inside the grace window.
 */
export function watchFirstActivation(badge: BadgeState): void {
  if (badge !== "visible") return;
  if (watching) return;
  try {
    if (isBoosthisDisabled()) return;
    if (isActivated()) return;
  } catch {
    return; // cannot read the gate → say nothing rather than something wrong
  }
  watching = true;
  waitStartedAt = Date.now();
  graceTimer = later(ANNOUNCE_AFTER_MS, announceWaitIfStillMissing);
  if (graceTimer === null) watching = false; // no timers → stay silent, retryable
}

/** @internal test hook */
export function _resetActivationNoticeForTests(): void {
  stopWatching();
  watching = false;
  spoke = false;
  settled = false;
}
