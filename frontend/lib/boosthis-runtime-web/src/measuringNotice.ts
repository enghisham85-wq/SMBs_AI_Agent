/**
 * Whether anything has actually been MEASURED — the half of the story the kit
 * used to announce from the wrong evidence.
 *
 * WHY (observed on a live customer storefront, Sep 2026): the moment the server
 * confirmed the install, the kit printed "Measuring has started". Seventy-eight
 * minutes later our own server still said that install had never measured
 * anything, and it was right: a confirmation is permission, not an outcome. The
 * kit's line is the one a developer reads first and trusts most, so it was the
 * kit that was lying and the server that was honest — the wrong way round.
 *
 * The confirmation line no longer claims an outcome (see `activationNotice`).
 * This module owns what happens NEXT, and it only ever speaks about facts it
 * witnessed:
 *
 *   • nothing measured a minute and a half after a first-ever start, on an
 *     install that IS confirmed and DOES upload per-page readings → say so, and
 *     say what would produce one;
 *   • a reading actually taken after that → retract, so the developer is never
 *     left holding a claim the dashboard contradicts.
 *
 * The budget is at most TWO lines, and only on a page that started unconfirmed
 * — the first-ever load for this browser, which is overwhelmingly the
 * developer's own. A returning visitor, already confirmed when the page starts,
 * never hears any of it: an explanation nobody was waiting for is noise.
 *
 * On an idle page, nothing being measured is the DESIGNED behaviour, not a
 * fault: the browser kit takes one reading per page view and takes it when the
 * view ends (`finalizePageSample` on pagehide / visibilitychange→hidden), and
 * the snapshot mirror refuses to ship a content-free payload. So the line below
 * explains rather than alarms — but it is still said, because "nothing has been
 * measured" is exactly what the server would say about the same install at the
 * same moment, and the kit must never disagree with it.
 */
import { isActivated, isRuntimeInert } from "./killSwitch";
import { isBoosthisDisabled } from "./runtimeFlags";
import {
  pageActivityFacts,
  NOTHING_MEASURED_IN_PLACE_LINE,
} from "./pageActivity";
import { hasSampleObserver, setSampleWatcher } from "./samples";
import type { BadgeState } from "./startAnnounce";

/**
 * Kit-owned literals. Kept as CONTIGUOUS single-quoted runs for the same
 * reason the activation notice's are: a cross-kit guard greps the shipped
 * source text, so never split one across a concatenation or a line break.
 */
export const NOTHING_MEASURED_YET_LINE =
  "[boosthis] Boosthis confirmed this install, but nothing has been measured yet. A reading is taken when a page view ends: open another page or leave this one, and the first reading goes up.";
export const FIRST_MEASUREMENT_LINE =
  "[boosthis] Boosthis has now taken its first reading on this page, so the earlier line about nothing being measured is out of date.";

/**
 * How long an install may be confirmed and empty before that silence is worth
 * explaining. Longer than the activation notice's own 50s horizon, so the two
 * never talk over each other, and longer than the 30s upload cadence, so a
 * reading that was merely in flight is never reported as a reading that never
 * happened.
 */
const NOTHING_MEASURED_AFTER_MS = 90_000;

let watching = false;
let spoke = false;
let done = false;
let sawReading = false;
let horizonTimer: ReturnType<typeof setTimeout> | null = null;

function say(line: string): void {
  try {
    // console.info, not warn: an install with nothing to report yet is a
    // state, not a fault.
    console.info(line);
  } catch {
    // A host that replaced console must never crash because of our line.
  }
}

function stopWatching(): void {
  if (horizonTimer !== null) {
    try {
      clearTimeout(horizonTimer);
    } catch {
      /* nothing to do */
    }
    horizonTimer = null;
  }
  try {
    setSampleWatcher(null);
  } catch {
    /* the ring buffer never breaks the kit */
  }
}

/** Nothing left to say: release the watcher and the timer. */
function finish(): void {
  done = true;
  stopWatching();
}

/** A reading was taken. Only worth a word if we already said there were none. */
function onReading(): void {
  if (sawReading) return;
  sawReading = true;
  if (done) return;
  if (spoke) say(FIRST_MEASUREMENT_LINE);
  finish();
}

/**
 * The horizon has arrived: is this install confirmed, uploading, and still
 * empty? Every other combination belongs to a line that already owns it.
 */
function reportIfNothingMeasured(): void {
  horizonTimer = null;
  if (done) return;
  if (sawReading) {
    // Something was measured. Nothing was ever claimed, so nothing is said:
    // narrating a success nobody doubted is noise.
    finish();
    return;
  }
  let speakable = false;
  try {
    speakable =
      !isBoosthisDisabled() &&
      isActivated() &&
      !isRuntimeInert() &&
      // An install that does not upload per-page readings (private mode, or a
      // host that never enabled telemetry) is a different story with its own
      // startup line. Two explanations of one silence are worse than one.
      hasSampleObserver();
  } catch {
    speakable = false; // cannot read the gate → say nothing rather than wrong
  }
  if (!speakable) {
    // Not confirmed, locked, off, or local-only: the activation notice and the
    // startup line own those, and they already say nothing is being measured.
    finish();
    return;
  }
  spoke = true;
  // WHICH silence is this? An app being used in place has nothing measured
  // for a reason the ordinary line does not name, and sending that developer
  // to "open another page" reads as a fault when it is the shape of their
  // app. Same voice, same moment, one extra fact — never a second notice
  // talking over this one.
  let inPlace = false;
  try {
    inPlace = pageActivityFacts().activity === "in-place";
  } catch {
    inPlace = false; // cannot read the counts → say the plainer thing
  }
  say(inPlace ? NOTHING_MEASURED_IN_PLACE_LINE : NOTHING_MEASURED_YET_LINE);
  // Stay armed: the promise this line makes ("a reading is taken when a page
  // view ends") has to be closed by the reading itself when it arrives.
}

/**
 * Start watching whether this install ever measures anything. Called from
 * `startWebVitals` beside `watchFirstActivation`, with the badge state the
 * startup line just announced.
 *
 * Says nothing at all when: Boosthis is switched off outright, the badge was
 * switched off with it, or this install is ALREADY confirmed when the page
 * starts (a returning visitor — the confirmation story, and this postscript to
 * it, belong to the first-ever load).
 */
export function watchFirstMeasurement(badge: BadgeState): void {
  if (badge === "switched-off") return;
  if (watching) return;
  try {
    if (isBoosthisDisabled()) return;
    if (isActivated()) return;
  } catch {
    return; // cannot read the gate → say nothing rather than something wrong
  }
  watching = true;
  try {
    setSampleWatcher(() => {
      onReading();
    });
  } catch {
    watching = false;
    return; // no way to witness a reading → never claim there were none
  }
  try {
    const t = setTimeout(reportIfNothingMeasured, NOTHING_MEASURED_AFTER_MS);
    // Never keep a Node-side entrypoint alive just to say a line.
    (t as unknown as { unref?: () => void }).unref?.();
    horizonTimer = t;
  } catch {
    // no timers → stay silent and leave nothing behind
    watching = false;
    stopWatching();
  }
}

/** @internal test hook */
export function _resetMeasuringNoticeForTests(): void {
  stopWatching();
  watching = false;
  spoke = false;
  done = false;
  sawReading = false;
}
