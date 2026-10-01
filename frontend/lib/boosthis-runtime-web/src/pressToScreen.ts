/** Press → screen usable, in the browser.
 *
 * The kit already times two halves of a navigation and reports neither as the
 * thing a person actually waits for:
 *
 *   - the press itself is stamped by the click watcher, and used only to
 *     decide whether a control did anything at all;
 *   - the destination view gets its OWN reading (its soft-navigation LCP, or
 *     the document's for the first view), which starts when the view opens.
 *
 * A single-page app can move the address instantly, paint a skeleton, and
 * spend three seconds fetching — and every number we publish about it stays
 * calm, because no reading spans the gap the reader is sitting through. This
 * module spans it: one press, joined to the view it opened, closed by that
 * view's own reading.
 *
 * WHAT IS MEASURED. Per navigation, two contiguous spans:
 *
 *   dead   = the view opening   − the press          (nothing on screen yet)
 *   settle = the view's own measured duration        (its LCP, its own clock)
 *   whole  = dead + settle
 *
 * `whole` is composed per navigation, from two spans that meet exactly at the
 * view boundary and are both measured for THAT navigation. That is not the
 * thing the contract forbids: what must never happen is a published p75 built
 * by adding the p75 of one leg to the p75 of another, which is a number no
 * navigation ever took. Each leg's percentile here is reported beside the
 * whole over the SAME joined navigations, so a reader can see both without
 * being invited to add them.
 *
 * Clocks: `dead` is two `Date.now()` reads taken in this document, and
 * `settle` is a DURATION the browser measured. A duration has no origin to
 * disagree with, so there is only one clock in the arithmetic.
 *
 * WHAT IS NOT MEASURED IS COUNTED. Every press that does not become a reading
 * is counted in one of three named ways, never silently dropped:
 *
 *   unjoinedPastBound — a view did open, but later than the join bound allows;
 *   unjoinedNoScreen  — the press was replaced by another before any view
 *                       opened, or the view opened before the press;
 *   unjoinedNoTiming  — the view opened and then ended with no reading of its
 *                       own (no LCP, and the first view's number belongs to
 *                       the first view alone). Browser-only: it is the count
 *                       behind "a view with no measurement of its own is
 *                       counted as untimed" on this side of the join.
 *
 * Numbers only. No URL, route label, control name, or per-press identifier
 * ever reaches this module or its wire shape
 * (docs/press-to-screen-contract.md).
 */

import { linearScore, ratingFor, type Rating } from "./thresholds";
import { REASON_PLATFORM_DOES_NOT_EXPOSE } from "./buildIdentity";

/** Bands for the WHOLE interval, shared with every other kit. */
export const PRESS_TO_SCREEN_THRESHOLDS = { good: 1000, poor: 3000 } as const;

/** How long a press stays eligible to be joined to a view it opened. */
export const NAV_JOIN_BOUND_MS = 15_000;

/** Joined navigations needed before the reading stops being pending. */
export const MIN_JOINED_NAVS = 5;

/** Ring cap — a long-lived tab must not grow this list without bound. */
const RING_CAP = 300;

/** A rating that can also be "pending" while the axis is still warming up,
 *  or "not-available" where the browser itself has no way to produce it. */
export type PressRating = Rating | "pending" | "not-available";

/** WHAT THIS BROWSER ANSWERED when the kit asked it for per-view timing:
 *  `false` is a look that found nothing, `true` is the mechanism being
 *  there, and `null` is nobody having asked yet — which is never evidence
 *  about the browser.
 *
 *  Deliberately NOT cleared by `resetPressToScreen()`. Everything else in
 *  this module is a measurement, and `forget()` exists to drop measurements;
 *  this is a fact about the engine the page is running in, and forgetting it
 *  would put the reading back to "still warming up" in a browser that has
 *  already answered that it can never produce one. */
let viewTimingSupported: boolean | null = null;

/** Tell the join what the browser said about per-view timing (vitals.ts owns
 *  the probe; this module owns what the answer means for the reading). */
export function notePressViewTimingSupport(supported: boolean | null): void {
  viewTimingSupported = supported;
}

export interface PressToScreenResult {
  /** 0–100, or null while pending (fewer than 5 joined navigations). */
  score: number | null;
  rating: PressRating;
  /** 0 ONLY where this reading can never be taken here. Absent otherwise:
   *  the shared availability slot every kit uses, so the server needs no
   *  per-axis knowledge to word the tile. */
  measurable?: number;
  /** Why, as a code from the shared closed list — never prose. */
  reasonCode?: number;
  /** p75 press→usable interval (ms), or null while pending. */
  p75Ms: number | null;
  /** Navigations joined press→usable (shown even while pending). */
  navCount: number;
  /** p75 of the dead leg over the SAME joined navigations. */
  deadP75Ms: number | null;
  /** p75 of the settle leg over the same navigations. */
  settleP75Ms: number | null;
  /** Presses whose view opened past the bound. Never silently dropped. */
  unjoinedPastBound: number;
  /** Presses no view could be attributed to at all. */
  unjoinedNoScreen: number;
  /** Presses whose view ended with no reading of its own. */
  unjoinedNoTiming: number;
  /** The bound in force, so the server words it without retyping it. */
  boundMs: number;
}

/** The most recent press, still eligible to join a view opening.
 *
 *  A press still waiting here when the document ends is deliberately NOT
 *  counted. In a browser the commonest reason a document ends a moment after
 *  a press is that the press was a link to somewhere else: the person got
 *  exactly what they asked for, on a full page load this reading does not
 *  measure and the browser's own navigation timing already covers. Counting
 *  it as "a press that went nowhere" would file a working link under the
 *  count that exists for presses which genuinely reached nothing, and every
 *  visit would end by inflating it. The press that opened a view and is
 *  waiting on that view's reading is a different matter, and IS counted when
 *  the document ends (`noteViewUntimedForPress`). */
let lastPressAt: number | null = null;
/** The press the currently open view belongs to, awaiting that view's own
 *  reading. Null when this view had no press to join. */
let openPressAt: number | null = null;
/** When the view holding that press opened. */
let openViewAt: number | null = null;
/** Joined navigations: the whole interval and the leg it contains. */
const joined: { dead: number; whole: number }[] = [];

let unjoinedPastBound = 0;
let unjoinedNoScreen = 0;
let unjoinedNoTiming = 0;

/**
 * A press happened. The next view to open within the bound joins it.
 * Best-effort, NEVER throws — this runs inside the host's click path.
 */
export function notePress(now: number): void {
  try {
    // A press still waiting when the next one arrives never reached a view.
    // That is the count a plain "keep the latest press" would throw away.
    if (lastPressAt != null) unjoinedNoScreen++;
    lastPressAt = now;
  } catch {
    /* best-effort */
  }
}

/**
 * A new view has opened. Banks the dead leg and holds the press for this
 * view's own reading. Best-effort, NEVER throws.
 */
export function noteViewOpened(now: number): void {
  try {
    // A new view supersedes whatever the last one was holding: that view
    // never produced a reading, so its press is not chargeable to this one.
    if (openPressAt != null) unjoinedNoTiming++;
    openPressAt = null;
    openViewAt = null;
    if (lastPressAt == null) return;
    const gap = now - lastPressAt;
    // Consume the press either way — a view that opens ends this press's
    // candidacy, so a later unrelated view cannot reuse it.
    const pressAt = lastPressAt;
    lastPressAt = null;
    if (gap < 0) {
      unjoinedNoScreen++;
      return;
    }
    if (gap > NAV_JOIN_BOUND_MS) {
      unjoinedPastBound++;
      return;
    }
    openPressAt = pressAt;
    openViewAt = now;
  } catch {
    /* best-effort */
  }
}

/**
 * The open view produced its own reading, `durationMs` long. Closes the join.
 * Best-effort, NEVER throws.
 */
export function noteViewMeasured(durationMs: number): void {
  try {
    const pressAt = openPressAt;
    const viewAt = openViewAt;
    openPressAt = null;
    openViewAt = null;
    if (pressAt == null || viewAt == null) return;
    if (!Number.isFinite(durationMs) || durationMs < 0) {
      // The view ended with a number we cannot use. That is the same silence
      // as no reading at all, and it is counted as such rather than dropped.
      unjoinedNoTiming++;
      return;
    }
    const dead = viewAt - pressAt;
    if (dead < 0) {
      unjoinedNoScreen++;
      return;
    }
    joined.push({ dead, whole: dead + durationMs });
    if (joined.length > RING_CAP) joined.splice(0, joined.length - RING_CAP);
  } catch {
    /* best-effort */
  }
}

/**
 * The open view produced a reading that spans FROM THE PRESS, not from the
 * view opening. Closes the join with that as the whole interval.
 *
 * This is how a browser reports a view it opened itself: the soft-navigation
 * entry starts at the interaction and its duration runs to the paint, so the
 * browser has already measured press→usable end to end. Passing it through
 * `noteViewMeasured` would add the dead leg a second time and publish an
 * interval no reader ever sat through.
 *
 * The dead leg stays OUR measurement (the press and the view opening, both
 * read here), and the settle leg is what is left of the browser's interval
 * after it. The two spans start at the same press a few milliseconds apart,
 * so a browser interval that comes back SHORTER than our own dead leg is
 * clock skew, not a negative settle: the whole is held at the dead leg and
 * the settle reads zero rather than inventing a number.
 *
 * Best-effort, NEVER throws.
 */
export function noteViewMeasuredFromPress(wholeMs: number): void {
  try {
    const pressAt = openPressAt;
    const viewAt = openViewAt;
    openPressAt = null;
    openViewAt = null;
    if (pressAt == null || viewAt == null) return;
    if (!Number.isFinite(wholeMs) || wholeMs < 0) {
      unjoinedNoTiming++;
      return;
    }
    const dead = viewAt - pressAt;
    if (dead < 0) {
      unjoinedNoScreen++;
      return;
    }
    joined.push({ dead, whole: Math.max(wholeMs, dead) });
    if (joined.length > RING_CAP) joined.splice(0, joined.length - RING_CAP);
  } catch {
    /* best-effort */
  }
}

/**
 * The open view ended with no reading of its own. The press it was holding is
 * counted as such rather than charged to whatever view completes next.
 */
export function noteViewUntimedForPress(): void {
  try {
    if (openPressAt != null) unjoinedNoTiming++;
    openPressAt = null;
    openViewAt = null;
  } catch {
    /* best-effort */
  }
}

/** Nearest-rank p75 of an unordered list (does not mutate). 0 when empty. */
function p75(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.max(
    0,
    Math.min(sorted.length - 1, Math.ceil(0.75 * sorted.length) - 1),
  );
  return sorted[idx];
}

/**
 * The whole press→usable interval, its two legs, and everything the bound
 * left behind. Pure read — never mutates state, NEVER throws.
 *
 * The counts ride even while the reading is pending: "nothing joined yet, and
 * here is how many presses went nowhere" is an answer, and it is the one a
 * silent discard makes impossible.
 */
export function readPressToScreen(): PressToScreenResult {
  try {
    const navCount = joined.length;
    const base = {
      navCount,
      unjoinedPastBound,
      unjoinedNoScreen,
      unjoinedNoTiming,
      boundMs: NAV_JOIN_BOUND_MS,
    };
    if (navCount < MIN_JOINED_NAVS) {
      if (viewTimingSupported === false && navCount === 0 && unjoinedNoTiming > 0) {
        // THIS BROWSER WILL NEVER MEASURE IT. The kit asked it for the
        // per-view timing this reading is built on and it does not have the
        // mechanism, so a press here reaches a view nobody can time — today
        // and every day after. That is an answer, and "pending" would be a
        // promise this browser cannot keep
        // (docs/press-to-screen-contract.md).
        //
        // Two facts about the app hold beside the browser's own answer.
        // Nothing may have joined: a browser that has already produced a
        // reading plainly does expose one, whatever it says now. And at
        // least one press must have reached a view with no reading of its
        // own — without that the kit has looked at the browser but not at
        // this app, and an app nobody has pressed anything in is still
        // warming up, which is what "pending" is for. The counts ride along
        // either way; they are how a reader tells those two apart.
        return {
          score: null,
          rating: "not-available" as PressRating,
          measurable: 0,
          reasonCode: REASON_PLATFORM_DOES_NOT_EXPOSE,
          p75Ms: null,
          deadP75Ms: null,
          settleP75Ms: null,
          ...base,
        };
      }
      return {
        score: null,
        rating: "pending" as PressRating,
        p75Ms: null,
        deadP75Ms: null,
        settleP75Ms: null,
        ...base,
      };
    }
    const whole = p75(joined.map((j) => j.whole));
    const score = linearScore(
      whole,
      PRESS_TO_SCREEN_THRESHOLDS.good,
      PRESS_TO_SCREEN_THRESHOLDS.poor,
    );
    return {
      score,
      rating: ratingFor(score),
      p75Ms: Math.round(whole),
      // Both legs are percentiles over the SAME joined navigations, each
      // measured in its own right. They are not expected to add up to the
      // whole, and a reader who adds them is reading three percentiles.
      deadP75Ms: Math.round(p75(joined.map((j) => j.dead))),
      settleP75Ms: Math.round(p75(joined.map((j) => j.whole - j.dead))),
      ...base,
    };
  } catch {
    return {
      score: null,
      rating: "pending",
      p75Ms: null,
      navCount: 0,
      deadP75Ms: null,
      settleP75Ms: null,
      unjoinedPastBound: 0,
      unjoinedNoScreen: 0,
      unjoinedNoTiming: 0,
      boundMs: NAV_JOIN_BOUND_MS,
    };
  }
}

/** Reset all state (forget() hook + tests). Idempotent. */
export function resetPressToScreen(): void {
  lastPressAt = null;
  openPressAt = null;
  openViewAt = null;
  joined.length = 0;
  unjoinedPastBound = 0;
  unjoinedNoScreen = 0;
  unjoinedNoTiming = 0;
}
