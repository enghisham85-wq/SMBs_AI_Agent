/**
 * HOW MANY PAGE VIEWS THIS KIT HAS MEASURED — and what that number covers.
 *
 * The panel's first number is "Page views measured", repeated in the hero line
 * under the score. It used to be read straight off the in-process sample
 * buffer, which is a plain array in the page's own JS context. Three facts
 * composed into a number that could never reach 2:
 *
 *  - the buffer dies with the document, so a full navigation threw the whole
 *    history away and the next page started from empty;
 *  - exactly one reading is taken per page view, on the way OUT (pagehide /
 *    visibilitychange→hidden), so the page being looked at was not in it;
 *  - a single-page app that changes the address never closed a view at all, so
 *    it sat at 1 for the life of the tab.
 *
 * Observed live: /lab showed "Page views measured 1", then the home page of the
 * same site showed "Page views measured 1". Two pages, same 1. A developer who
 * has just browsed ten pages and reads 1 concludes the kit is broken.
 *
 * This module holds the count instead, with two rules:
 *
 *  1. IT SAYS WHAT IT COVERS. The count is kept in the kit's own namespaced
 *     browser store, so it spans page loads on this device. Where that store
 *     does not exist or refuses the write, the number covers less — and the
 *     panel says so in words rather than implying a device-wide figure. Three
 *     distinguishable answers, never two (see PageViewTallyScope).
 *  2. IT COUNTS THE PAGE BEING LOOKED AT. A view whose measurement already
 *     exists is counted while it is open, so the number is not permanently one
 *     behind. It is counted exactly once: the open view adds 1 until it closes,
 *     and closing is what increments the stored total.
 *
 * WHAT IT IS NOT. This is a display number for the kit's own panel on the
 * developer's own device. It is never uploaded and it is not on any wire: the
 * per-document reading count that travels in a snapshot (`pageActivity`,
 * `sampleCount`) is unchanged, and stays per-document on purpose, because that
 * is the question the server is asking. Nothing here is a label, an address or
 * anything a person typed — two integers and one word from a closed list.
 *
 * WHERE IT IS WRITTEN. Nowhere on the navigation path. The tally rides the same
 * two moments the accumulated page map already rides (pagehide and
 * visibilitychange→hidden), so a route change costs no storage write. A tab
 * killed without either moment loses that page's increments, exactly as the
 * page map does.
 */

import { storage } from "./storage";

/** Namespaced by storage.ts (`boosthis:` prefix), so it can never collide with
 *  or enumerate the host app's own storage. */
const STORAGE_KEY = "pageviews.v1";

/** A corrupted or hostile store must not put an absurd number on the panel. */
const MAX_COUNT = 1_000_000_000;

/**
 * What the number on the panel actually covers. An accumulating count and one
 * that only looks like it is accumulating must never read alike:
 *
 *  - `this-page`            nothing has ever reached a durable store, so the
 *                           number covers this page load and no more. True on
 *                           the very first view of a fresh device, and true for
 *                           the whole visit where storage is unavailable (SSR,
 *                           a sandboxed embed, private mode, storage disabled).
 *  - `this-device`          the durable store holds the count and is still
 *                           taking it: the number spans page loads in this
 *                           browser.
 *  - `this-device-stalled`  the count came from (or once reached) the durable
 *                           store, but the most recent save was refused —
 *                           quota, a blocked embed. What is shown is real up to
 *                           that point and may stop growing.
 */
export type PageViewTallyScope =
  | "this-page"
  | "this-device"
  | "this-device-stalled";

export interface PageViewTally {
  /** Page views the kit holds a measurement for — closed views plus the one
   *  being looked at, when something has already been measured for it. This is
   *  the ONE number both panel surfaces render. */
  measured: number;
  /** View changes that closed with no measurement of their own. Counted rather
   *  than filled in with an earlier view's number (see finalizePageSample in
   *  vitals.ts). Same scope as `measured`, so the two never mix windows. */
  untimed: number;
  /** Is the page being looked at part of `measured`? Lets the panel say so
   *  instead of leaving a reader to wonder why the number moved. */
  inProgress: boolean;
  /** What `measured` and `untimed` cover. */
  scope: PageViewTallyScope;
}

let completed = 0;
let untimed = 0;
let hydrated = false;
/** Has the durable store ever demonstrably held this tally — read back out of
 *  it on load, or a save that landed? */
let everKept = false;
/** Did the most recent save get refused? */
let lastSaveRefused = false;
/** Reads "is a measurement already in hand for the view now open?" — registered
 *  by vitals.ts, which owns that question. Unregistered means no open view is
 *  claimed, never a false claim of one. */
let openViewMeasured: (() => boolean) | null = null;

function clampCount(v: unknown): number {
  if (typeof v !== "number" || !Number.isFinite(v) || v <= 0) return 0;
  return Math.min(Math.floor(v), MAX_COUNT);
}

/** Read the kept tally once per page load. Never throws. */
function hydrate(): void {
  if (hydrated) return;
  hydrated = true;
  try {
    const raw = storage.get(STORAGE_KEY);
    if (raw == null) return;
    const parsed = JSON.parse(raw) as { m?: unknown; u?: unknown };
    completed = clampCount(parsed?.m);
    untimed = clampCount(parsed?.u);
    // Something was read back, so the number on the panel already spans more
    // than this page load — say the wider scope, and let a refused save below
    // downgrade it again.
    if (storage.isPersistent()) everKept = true;
  } catch {
    // A store that cannot be read is a store that is not keeping this: the
    // count starts from this page load, and the scope word says so.
  }
}

/** A page view closed with a reading of its own. */
export function noteViewMeasured(): void {
  try {
    hydrate();
    completed = clampCount(completed + 1);
  } catch {
    // bookkeeping must never break the host page
  }
}

/** A page view closed with no measurement belonging to it. */
export function noteViewUntimed(): void {
  try {
    hydrate();
    untimed = clampCount(untimed + 1);
  } catch {
    // bookkeeping must never break the host page
  }
}

/** Register the "the open view already has a measurement" reader (vitals.ts).
 *  Pass null to drop it. */
export function setOpenViewMeasuredSource(fn: (() => boolean) | null): void {
  openViewMeasured = fn;
}

/** Persist the tally. Called from the page-exit moments the accumulated page
 *  map already rides — never on the navigation path. Never throws. */
export function savePageViewTally(): void {
  try {
    hydrate();
    const landed = storage.set(
      STORAGE_KEY,
      JSON.stringify({ m: completed, u: untimed }),
    );
    lastSaveRefused = !landed;
    if (landed) everKept = true;
  } catch {
    lastSaveRefused = true;
  }
}

/** Drop the kept tally (called by the kit's forget path). */
export function clearPageViewTally(): void {
  try {
    storage.remove(STORAGE_KEY);
  } catch {
    // ignore
  }
  completed = 0;
  untimed = 0;
  everKept = false;
  lastSaveRefused = false;
  hydrated = true;
}

function scopeWord(): PageViewTallyScope {
  if (!everKept) return "this-page";
  return lastSaveRefused ? "this-device-stalled" : "this-device";
}

/** The current picture, including the page being looked at. Never throws. */
export function readPageViewTally(): PageViewTally {
  try {
    hydrate();
  } catch {
    // fall through with whatever is in hand
  }
  let inProgress = false;
  try {
    inProgress = openViewMeasured ? !!openViewMeasured() : false;
  } catch {
    // A source that cannot answer claims nothing.
    inProgress = false;
  }
  return {
    measured: clampCount(completed + (inProgress ? 1 : 0)),
    untimed,
    inProgress,
    scope: scopeWord(),
  };
}

// ── The words. Authored here so every surface greps to one place. ───────────

/** The sentence under the panel's count. It states the scope in full every
 *  time: the count's label says "page views measured", and a reader must not
 *  have to guess whether that means this page, this visit or this browser. */
export function pageViewScopeLine(t: PageViewTally): string {
  const current = t.inProgress
    ? ", including the page you are on"
    : ", not counting the page you are on until you leave it";
  switch (t.scope) {
    case "this-page":
      return `counted on this page only${current} \u2014 nothing has been kept between pages yet`;
    case "this-device-stalled":
      return `counted on this device up to now${current} \u2014 this browser refused to keep the record, so the count may stop growing`;
    default:
      return `counted on this device${current}`;
  }
}

/** @internal test hook — reset module state between hermetic runs. */
export function _resetPageViewTallyForTests(): void {
  completed = 0;
  untimed = 0;
  hydrated = false;
  everKept = false;
  lastSaveRefused = false;
  openViewMeasured = null;
}
