/**
 * controlCensus — ask the rendered page what controls it has.
 *
 * WHY THIS EXISTS
 * ---------------
 * `routeInventory.ts` asks the router for the whole route table, so the map
 * draws pages nobody has opened instead of only the ones someone happened to
 * walk. The same trick works one level down. A browser kit is not watching
 * the app from outside — it is INSIDE the rendered document, and the document
 * already knows how many interactive elements it has and where they sit. So
 * the kit asks it.
 *
 * That is not crawling, not clicking, and not reading source. It is the
 * running interface describing its own shape, exactly as a framework
 * describes its own routes.
 *
 * WHAT TRAVELS, AND WHAT NEVER DOES
 * ---------------------------------
 * **Structure travels, text never does.** A control is identified by
 * `controlHandle.ts` — a hash of its position in the tree — and by a kind
 * word from a closed list. No label, no value, no placeholder, no attribute
 * value, no text, nothing a person typed. The reasoning is recorded in
 * `docs/decisions/control-handle-structural.md`.
 *
 * The developer's own `data-testid` name that `controlMap.ts` keeps is a
 * SEPARATE identity and stays on the device; a person typed it, so it may
 * not travel. Nothing in this module reads it.
 *
 * WHAT IT CAN AND CANNOT SEE
 * --------------------------
 * The census enumerates the controls a CSS selector can match: the control
 * tags, the ARIA control roles, an inline `onclick`, a `tabindex`. The click
 * path also treats a `cursor: pointer` element as a control, and those are
 * NOT censused — deciding it would mean asking for a computed style on every
 * node of the page, which is the exact cost this product exists to report.
 * So a press can land on a handle the census never enumerated; that is
 * counted as {@link ControlCensusReport.usedOffCensus} and said out loud,
 * never dropped and never silently folded in.
 *
 * It is a census of the document AS IT IS NOW. A control behind a closed
 * menu, an unopened dialog or a route nobody has visited is not rendered and
 * so is not in it. The census states its total rather than implying the page
 * has no others.
 */

import { controlHandle, isControlHandle } from "./controlHandle";
import { readFlagValue } from "./runtimeFlags";
import { safeRoutePattern } from "./routeInventory";

/**
 * The status vocabulary. The route list's four words, plus one this census
 * needs and a route list does not: `no-identity`, for a runtime that can
 * see a press happen but has no way to say WHICH control it was. That is the
 * phone kit's real position today, and it is a different fact from "switched
 * off" and from "nothing rendered".
 *
 * The block always travels — including when it says `off` — so "a developer
 * switched it off" can be told from "a kit too old to know the question".
 */
export const CONTROL_CENSUS_STATUS_WORDS = [
  "read",
  "unsupported",
  "unreadable",
  "off",
  "no-identity",
] as const;
export type ControlCensusStatus = (typeof CONTROL_CENSUS_STATUS_WORDS)[number];

/**
 * What kind of control it is, from a closed list. Derived from the element's
 * TAG and from whether it matches one of our own literal role selectors —
 * never from an attribute's value, so nothing a developer wrote decides it.
 */
export const CONTROL_KIND_WORDS = [
  "press",
  "link",
  "field",
  "choice",
  "other",
] as const;
export type ControlKind = (typeof CONTROL_KIND_WORDS)[number];

/**
 * Where pressing it went, from a closed list.
 *
 * `route`      — a page change followed within the join window.
 * `same-page`  — it was pressed and the page stayed. A FACT, not a failure:
 *                most controls in an app are supposed to stay.
 * `unknown`    — never pressed, or pressed and the join could not be
 *                believed (overtaken by another press, frozen tab).
 */
export const CONTROL_LEADS_WORDS = ["route", "same-page", "unknown"] as const;
export type ControlLeads = (typeof CONTROL_LEADS_WORDS)[number];

/** Why the census stopped early, from a closed list. `none` means it did not. */
export const CONTROL_CENSUS_BOUND_WORDS = [
  "none",
  "entries",
  "nodes",
  "time",
  "shared-handle",
] as const;
export type ControlCensusBound = (typeof CONTROL_CENSUS_BOUND_WORDS)[number];

export interface ControlCensusEntry {
  /** Structural handle — see `controlHandle.ts`. Never a name. */
  handle: string;
  kind: ControlKind;
  /** Whether a press on this handle has been observed this page view. */
  used: boolean;
  leads: ControlLeads;
  /** The route it led to. Present ONLY when `leads === "route"`. */
  to?: string;
  /**
   * WAS IT PRESSED AND DID NOTHING — per control, not per page.
   *
   * The dead-click detector in `vitals.ts` already judges every actionable
   * press: it watches a settling window for a navigation, a DOM change or a
   * scroll, and increments the page's dead or actionable tally. That judgement
   * is reused here UNCHANGED and simply filed against the handle the press
   * landed on, so the map can point at one control instead of a page.
   *
   * `responded` — the press produced an observable change. It says the
   *   control RESPONDED. It does NOT say the control did the right thing:
   *   nothing here can see what the change was supposed to be.
   * `dead`      — the press produced nothing observable in the window.
   * `unjudged`  — the press happened and its window could not be believed
   *   (overtaken by a later press, or fired far too late after a frozen tab
   *   or a back/forward-cache restore). A discarded comparison, counted as
   *   its own thing rather than folded into either verdict.
   *
   * Each is present only when non-zero. A `used: true` entry always carries
   * at least one of them, so an absent field on a pressed control is a real
   * zero, and an entry with `used: false` was never pressed at all — three
   * distinguishable readings and no fourth.
   */
  responded?: number;
  dead?: number;
  unjudged?: number;
}

export interface ControlCensusReport {
  status: ControlCensusStatus;
  /**
   * The route label of the page this census is OF, already normalized by the
   * same function every other label this kit sends goes through.
   *
   * A census is a statement about ONE rendered page, and without this the
   * server has a count with nothing to attach it to. Present only on a
   * `read`, and absent when the caller could not say which page it was —
   * an unattached census is still reported, it is simply not drawn on a box.
   */
  route?: string;
  /**
   * The controls found. ALWAYS an array so a missing key reads as an
   * unrecognised shape rather than an empty page, and always EMPTY unless
   * `status` is `read` — a census that did not run enumerated nothing, and
   * that is not the same as a page with no controls.
   */
  entries: ControlCensusEntry[];
  /**
   * How many controls the scan FOUND, before the entry cap.
   *
   * PRESENT ONLY WHEN `status` IS `read`. A census that did not run has no
   * count, and sending `0` for it would render an unmeasured page as a page
   * with no controls — the exact confusion the status words exist to stop.
   *
   * When `boundedBy` is `nodes` or `time` this is a FLOOR: the scan stopped
   * before it had seen the whole page, and it says so rather than letting
   * the number read as a total.
   */
  total?: number;
  /** True when the census is not the whole page. Present only on a `read`. */
  bounded?: boolean;
  /** Present only on a `read`. */
  boundedBy?: ControlCensusBound;
  /**
   * Presses joined to a handle that the census did not enumerate — a
   * `cursor: pointer` control, or one rendered after the scan. Counted and
   * reported; never folded into the census, which would claim the scan
   * found something it did not. Present only on a `read`.
   */
  usedOffCensus?: number;
}

/**
 * How many entries travel. A page with more still reports its `total`, so a
 * reader says "at least this many" rather than believing the cap. Matched to
 * the same order of magnitude as the route list's cap: past a few hundred
 * controls on one screen, individual controls are not what anyone is reading.
 */
export const MAX_CENSUS_ENTRIES = 300;

/**
 * How many matched nodes the scan will walk. A query that matched more than
 * this stops, and the report says `nodes` — the page is bigger than the
 * census, and the census says so instead of pretending its total is the page.
 */
export const MAX_CENSUS_SCAN = 3000;

/**
 * The scan's own time budget, in milliseconds. Taking a handle is a bounded
 * walk, but a pathological tree can still add up, and this must cost the page
 * nothing a person could feel. Past the budget the scan stops and says `time`.
 */
export const CENSUS_BUDGET_MS = 8;

/** Bound on the exercised record, so a long session cannot grow it freely. */
export const MAX_EXERCISED_CONTROLS = 600;

/**
 * Ceiling on any one control's press tally. A counter that can only be
 * incremented by a human pressing something cannot realistically reach this,
 * so hitting it means something is pressing in a loop — and a number that
 * stops climbing is better than one that runs away into a reader's face.
 * The server refuses anything above it outright.
 */
export const MAX_CONTROL_PRESS_COUNT = 100000;

/**
 * The selector the census asks the document for. Every term is one of ours:
 * a tag, or an attribute PRESENCE, or an attribute matched against one of our
 * own literal words. No value is read out of the page by any of it.
 *
 * `[tabindex]` deliberately matches presence rather than a value, so
 * `tabindex="-1"` (a focus target, not a control) is included. Over-counting
 * in a way the reader can see beats a value read.
 */
export const CONTROL_CENSUS_SELECTOR =
  "button,a,input,select,textarea,summary," +
  '[role="button"],[role="link"],[role="menuitem"],' +
  '[role="menuitemcheckbox"],[role="tab"],' +
  "[onclick],[tabindex]";

/** Our own literal role selectors, reused for the kind word. */
const PRESS_ROLE_SELECTOR =
  '[role="button"],[role="menuitem"],[role="menuitemcheckbox"],[role="tab"]';
const LINK_ROLE_SELECTOR = '[role="link"]';

/* ─── Module state ───────────────────────────────────────────────────── */

interface ExercisedRecord {
  leads: ControlLeads;
  to: string | null;
  /** Presses on this handle that produced an observable change. */
  responded: number;
  /** Presses on this handle that produced nothing observable. */
  dead: number;
  /** Presses whose settling window could not be believed. */
  unjudged: number;
}

/** Add one to a tally without letting it run past its stated ceiling. */
function bumpPressCount(n: number): number {
  return n >= MAX_CONTROL_PRESS_COUNT ? MAX_CONTROL_PRESS_COUNT : n + 1;
}

/**
 * WHAT A PRESS IS FILED UNDER: THE PAGE **AND** THE CONTROL.
 *
 * A structural handle describes a position in the rendered tree, and the
 * same position exists on every page an app draws — the third button in the
 * header is `c1a2b3c4` on `/basket` and on `/checkout` alike. Keyed by the
 * handle alone, a press that died on one page would be read back against a
 * different control on another, and the map would point at a pin that was
 * never touched. So the key is the route label the press happened on and
 * the handle together, and a census joins ONLY against its own route.
 */
const EXERCISED_KEY_SEP = "\u0000";

function exercisedKey(route: string, handle: string): string {
  return route + EXERCISED_KEY_SEP + handle;
}

const exercised = new Map<string, ExercisedRecord>();
/** The one name this switch answers to. An OPT-OUT: the census runs unless
 *  a page or build says otherwise, so an absent flag means on. */
export const CONTROL_CENSUS_FLAG = "BOOSTHIS_CONTROL_CENSUS";

let enabledOverride: boolean | null = null;


/**
 * Switch the census off. Switched off, the block STILL travels saying `off`,
 * so the server can tell a developer who turned it off from a kit too old to
 * have it.
 */
export function setControlCensusEnabled(on: boolean): void {
  enabledOverride = on === false ? false : true;
}

export function controlCensusEnabled(): boolean {
  if (enabledOverride !== null) return enabledOverride;
  try {
    // Read through the kit’s ONE flag reader, so the switch answers to
    // every spelling the install guide teaches — including the two a
    // shipped page can actually set. A flag read straight off `globalThis`
    // by name works perfectly under the test runner and is unreachable in
    // half the ways a site owner is told to set it.
    const v = readFlagValue(CONTROL_CENSUS_FLAG);
    if (v === false) return false;
    if (typeof v === "string") {
      const s = v.trim().toLowerCase();
      if (s === "0" || s === "false" || s === "off" || s === "no") return false;
    }
  } catch {
    /* a page may seal globalThis */
  }
  return true;
}

/**
 * Record that a control was pressed, joined to what followed.
 *
 * Called from the ONE place a press is already observed — the settle timer
 * `controlMap.ts` runs — so no new listener is added anywhere. A press is
 * recorded whenever a handle could be taken, including when the join itself
 * could not be believed: the control was exercised either way, and the
 * *destination* is what is unknown, which is what `unknown` says.
 *
 * `route` outranks `same-page` outranks `unknown`: a control that has once
 * been seen to open a page keeps that, because a later press that stayed put
 * does not unmake the one that navigated.
 */
export function recordControlExercised(press: {
  /**
   * The route label the press happened ON, already normalized by the caller
   * the same way the census's own route is. Without it there is nothing to
   * scope the verdict to, so a press that cannot name its page is dropped
   * rather than filed against whichever page is showing later.
   */
  from: string;
  handle: string | null;
  /** The route that followed, already normalized, or null. */
  to: string | null;
  /** False when the join could not be believed (superseded / suspended). */
  joinable: boolean;
  /**
   * The dead-click detector's OWN verdict on this press — true when it saw a
   * change, false when it saw none — or null when the window could not be
   * believed and the comparison is therefore discarded rather than counted.
   *
   * Optional so a caller that has no verdict (there is none today, and there
   * must be no way for one to arrive as a silent false) says so by omission
   * and lands in `unjudged`.
   */
  hadEffect?: boolean | null;
}): void {
  try {
    // Switched off means nothing is collected, not merely nothing sent.
    if (!controlCensusEnabled()) return;
    const handle = press.handle;
    if (!isControlHandle(handle)) return;
    // A press with no page of its own cannot be attributed to a control on
    // one: the same handle exists on every page the app draws.
    const fromLabel =
      typeof press.from === "string" && press.from !== ""
        ? safeRoutePattern(press.from)
        : null;
    if (fromLabel === null) return;
    const to =
      press.joinable && typeof press.to === "string" && press.to !== ""
        ? safeRoutePattern(press.to)
        : null;
    const leads: ControlLeads = !press.joinable
      ? "unknown"
      : to !== null
        ? "route"
        : "same-page";

    const verdict = deadPressPerControlVerdict(
      press.joinable,
      press.hadEffect,
    );

    const key = exercisedKey(fromLabel, handle);
    const existing = exercised.get(key);
    if (existing === undefined) {
      if (exercised.size >= MAX_EXERCISED_CONTROLS) return;
      exercised.set(key, {
        leads,
        to,
        responded: verdict === "responded" ? 1 : 0,
        dead: verdict === "dead" ? 1 : 0,
        unjudged: verdict === "unjudged" ? 1 : 0,
      });
    } else {
      // The destination keeps its ranking rule — a control once seen to open
      // a page keeps that — while the press tallies simply accumulate: they
      // are counts of events, and an event does not stop having happened.
      if (leadsRank(leads) > leadsRank(existing.leads)) {
        existing.leads = leads;
        existing.to = to;
      }
      existing[verdict] = bumpPressCount(existing[verdict]);
    }

    // A press on something the census never enumerated is said out loud in
    // the report — but it is worked out THERE, against the list that census
    // actually produced for this page, not against whichever page was last
    // scanned. It is never added to the census: the census is a statement
    // about what the scan found, and this was not found.
  } catch {
    // the census is display-only — it must never break click counting
  }
}

/**
 * Presses this census cannot account for, on THIS page.
 *
 * Worked out at report time rather than counted as presses arrive, because
 * the question is "did the scan that just ran list this control?" and only
 * the scan that just ran can answer it. A press recorded before the first
 * census of a page is therefore judged by that census rather than counted
 * off it by default.
 *
 * The answer is a FLOOR twice over: each record's tallies stop at their own
 * ceiling, and the record map itself is capped.
 */
function offCensusPresses(
  keyPrefix: string | null,
  listed: Set<string>,
): number {
  if (keyPrefix === null) return 0;
  let n = 0;
  for (const [key, rec] of exercised) {
    if (!key.startsWith(keyPrefix)) continue;
    const handle = key.slice(keyPrefix.length);
    if (listed.has(handle)) continue;
    n += rec.responded + rec.dead + rec.unjudged;
  }
  return n;
}

function leadsRank(leads: ControlLeads): number {
  return leads === "route" ? 2 : leads === "same-page" ? 1 : 0;
}

/**
 * WHAT PRESSING THIS CONTROL DID — the one derivation, in one place.
 *
 * This is the whole of what "the kit can attribute a dead press to a
 * control" means on this runtime, which is why it is a named function rather
 * than a ternary inside the recorder: the coverage contract's per-runtime
 * answer is derived from this kit's own source, and a fact that is only true
 * because of an expression buried in a caller cannot be pointed at.
 *
 * A press whose window could not be believed has no verdict to record:
 * whatever the settling window saw may belong to the press that overtook it,
 * or to the gap a frozen tab left behind. Counting it as `dead` would accuse
 * a control of a fault nobody observed, and counting it as `responded` would
 * clear one. It gets its own tally instead.
 *
 * `responded` is never "correct": all that was seen is that something
 * changed, never whether it was the right something.
 */
export function deadPressPerControlVerdict(
  joinable: boolean,
  hadEffect?: boolean | null,
): "responded" | "dead" | "unjudged" {
  if (!joinable) return "unjudged";
  if (hadEffect === true) return "responded";
  if (hadEffect === false) return "dead";
  return "unjudged";
}

/* ─── The census itself ──────────────────────────────────────────────── */

function kindOf(el: Element): ControlKind {
  let tag = "";
  try {
    tag = typeof el.tagName === "string" ? el.tagName.toLowerCase() : "";
  } catch {
    return "other";
  }
  if (tag === "a") return "link";
  if (tag === "button" || tag === "summary") return "press";
  if (tag === "input" || tag === "textarea") return "field";
  if (tag === "select") return "choice";
  try {
    if (typeof el.matches === "function") {
      if (el.matches(LINK_ROLE_SELECTOR)) return "link";
      if (el.matches(PRESS_ROLE_SELECTOR)) return "press";
    }
  } catch {
    /* a host that cannot answer `matches` gets the catch-all word */
  }
  return "other";
}

function now(): number {
  try {
    const p = (globalThis as { performance?: { now?: () => number } }).performance;
    if (p && typeof p.now === "function") return p.now();
  } catch {
    /* fall through */
  }
  try {
    return Date.now();
  } catch {
    return 0;
  }
}

/**
 * Walk the rendered document and report its controls.
 *
 * Pure with respect to the page: one `querySelectorAll`, then `tagName`,
 * `parentNode` and `previousSibling` per match. No computed style, no
 * geometry, no attribute values, no text — so nothing here can force a
 * layout, and nothing here can read content.
 */
export function controlCensusReport(routeLabel?: string | null): ControlCensusReport {
  if (!controlCensusEnabled()) return { status: "off", entries: [] };

  // The census's OWN page, resolved before anything is joined to it. A
  // census with no route of its own joins nothing: it cannot tell whether a
  // recorded press belongs to the page in front of it, and reading somebody
  // else's press onto this page is the one mistake a control-level map must
  // not make. Every entry then reads `used: false`, which is what the
  // absence of a believable join means here — and the caller that has a
  // route (every shipped one) always passes it.
  const route =
    typeof routeLabel === "string" && routeLabel !== ""
      ? safeRoutePattern(routeLabel)
      : null;
  const keyPrefix = route === null ? null : route + EXERCISED_KEY_SEP;

  let matches: ArrayLike<Element>;
  try {
    const doc = (globalThis as { document?: Document }).document;
    if (!doc || typeof doc.querySelectorAll !== "function") {
      return { status: "unsupported", entries: [] };
    }
    matches = doc.querySelectorAll(CONTROL_CENSUS_SELECTOR);
  } catch {
    return { status: "unreadable", entries: [] };
  }

  try {
    const started = now();
    const seen = new Set<string>();
    const entries: ControlCensusEntry[] = [];
    let boundedBy: ControlCensusBound = "none";
    // Two controls too far along the same run of siblings share one
    // handle (the ordinal gives up past its cap). They are one entry,
    // so the count is short by however many collapsed — and a count
    // that is short must SAY so rather than read as the whole page.
    let collapsed = 0;
    let total = 0;
    const length = typeof matches.length === "number" ? matches.length : 0;

    for (let i = 0; i < length; i++) {
      if (i >= MAX_CENSUS_SCAN) {
        boundedBy = "nodes";
        break;
      }
      if ((i & 63) === 63 && now() - started > CENSUS_BUDGET_MS) {
        boundedBy = "time";
        break;
      }
      const el = matches[i];
      if (!el) continue;
      const handle = controlHandle(el);
      if (handle === null) continue;
      // Two controls can share a handle: the ordinal gives up past its cap,
      // and an unrecognised tag collapses to one token. They are ONE entry
      // and the `total` counts them once, so a count and a list never
      // disagree.
      if (seen.has(handle)) {
        collapsed++;
        continue;
      }
      seen.add(handle);
      total++;
      if (entries.length >= MAX_CENSUS_ENTRIES) {
        if (boundedBy === "none") boundedBy = "entries";
        continue;
      }
      const record =
        keyPrefix === null ? undefined : exercised.get(keyPrefix + handle);
      const entry: ControlCensusEntry = {
        handle,
        kind: kindOf(el),
        used: record !== undefined,
        leads: record === undefined ? "unknown" : record.leads,
      };
      if (record !== undefined && record.leads === "route" && record.to !== null) {
        entry.to = record.to;
      }
      if (record !== undefined) {
        // Only non-zero tallies travel. A control that was pressed always
        // carries at least one of them, so an absent field beside
        // `used: true` is a measured zero — and a control with `used: false`
        // was never pressed, which is a different statement again.
        if (record.responded > 0) entry.responded = record.responded;
        if (record.dead > 0) entry.dead = record.dead;
        if (record.unjudged > 0) entry.unjudged = record.unjudged;
      }
      entries.push(entry);
    }

    // A collapse is the quietest way this census could be wrong, so it
    // is the last thing checked and it never overwrites a louder reason:
    // a scan that also ran out of nodes or time is short for that reason
    // FIRST, and both answers make the total a floor either way.
    if (boundedBy === "none" && collapsed > 0) boundedBy = "shared-handle";
    return {
      status: "read",
      ...(route !== null ? { route } : {}),
      total,
      entries,
      bounded: boundedBy !== "none",
      boundedBy,
      usedOffCensus: offCensusPresses(keyPrefix, seen),
    };
  } catch {
    return { status: "unreadable", entries: [] };
  }
}

/**
 * The block a snapshot carries.
 *
 * Present whenever this kit knows the question — including when the answer is
 * `off` or `unsupported`. Absent only when the census could not produce a
 * block at all, which is the one case the server reads as "this kit cannot
 * answer".
 */
export function controlCensusForSnapshot(
  routeLabel?: string | null,
): ControlCensusReport | undefined {
  try {
    return controlCensusReport(routeLabel);
  } catch {
    return undefined;
  }
}

/* ─── The on-device wording ──────────────────────────────────────────── */

/**
 * One sentence for the kit's own status readout. Every status word gets its
 * own answer, and none of them is a zero: a census that did not run says why
 * it did not run, rather than reporting a page with no controls.
 */
export function controlCensusStatusLine(r: ControlCensusReport): string {
  if (r.status === "off") {
    return (
      "Controls on this page: switched off in this build, so no control " +
      "count and no structural handles are collected or sent."
    );
  }
  if (r.status === "unsupported") {
    return (
      "Controls on this page: not readable here \u2014 this build has no " +
      "document to ask, so nothing is counted. Not a page with no controls."
    );
  }
  if (r.status === "unreadable") {
    return (
      "Controls on this page: the page refused the question, so nothing is " +
      "counted. Not a page with no controls."
    );
  }
  if (r.status === "no-identity") {
    return CONTROL_CENSUS_NO_IDENTITY_SENTENCE;
  }
  const total = typeof r.total === "number" ? r.total : r.entries.length;
  const used = r.entries.filter((e) => e.used).length;
  const leadsSomewhere = r.entries.filter((e) => e.leads === "route").length;
  const deadControls = r.entries.filter((e) => (e.dead ?? 0) > 0).length;
  const deadPresses = r.entries.reduce((a, e) => a + (e.dead ?? 0), 0);
  let line =
    `Controls on this page: ${total} found` +
    `, ${used} of the ${r.entries.length} listed pressed so far` +
    `, ${leadsSomewhere} seen to open another page`;
  if (deadControls > 0) {
    line +=
      `. ${deadControls} ${deadControls === 1 ? "control was" : "controls were"} ` +
      `pressed and nothing happened (${deadPresses} ` +
      `${deadPresses === 1 ? "press" : "presses"} in all) \u2014 no ` +
      "navigation, no change on the page, no scroll within the settling " +
      "window";
  }
  if (r.bounded === true) {
    line +=
      ". This census is bounded and is not the whole page: " +
      censusBoundSentence(r);
  }
  const offCensus = typeof r.usedOffCensus === "number" ? r.usedOffCensus : 0;
  if (offCensus > 0) {
    line +=
      `. ${offCensus} ` +
      (offCensus === 1 ? "press was" : "presses were") +
      " on a control this scan never listed (a pointer-cursor control, or " +
      "one drawn after the scan); counted here, not added to the count above";
  }
  const respondedControls = r.entries.filter(
    (e) => (e.responded ?? 0) > 0,
  ).length;
  if (respondedControls > 0) {
    line +=
      `. ${respondedControls} ${respondedControls === 1 ? "control" : "controls"} ` +
      "responded to a press \u2014 something changed. That is not a " +
      "statement that the right thing changed; this kit sees that the page " +
      "moved, never what it was supposed to do";
  }
  const unjudged = r.entries.reduce((a, e) => a + (e.unjudged ?? 0), 0);
  if (unjudged > 0) {
    line +=
      `. ${unjudged} ${unjudged === 1 ? "press" : "presses"} could not be ` +
      "judged either way \u2014 overtaken by a later press, or the window " +
      "fired far too late to be believed \u2014 so " +
      (unjudged === 1 ? "it is" : "they are") +
      " counted apart from both";
  }
  return (
    line +
    ". A control is identified by its position in the page, never by its " +
    "text, label or value. The count and those positions are sent; nothing " +
    "readable is."
  );
}

/**
 * What a runtime that observes presses but cannot say WHICH control says.
 * One sentence, shared by the kit that is in that position and by the server
 * that words it, so the two surfaces cannot drift.
 */
export const CONTROL_CENSUS_NO_IDENTITY_SENTENCE =
  "Controls on this screen: this kit sees that a control was pressed but " +
  "not which one \u2014 its touch observation carries a timestamp and " +
  "nothing else \u2014 so there is no census of this screen. That is a gap " +
  "in what we can see, not a screen with no controls. No press on this " +
  "screen is reported as having done nothing either: without an identity " +
  "there is no control to attribute a dead press to, and this kit reports " +
  "none rather than a number nobody could act on.";

/** Why the census is not the whole page, in words, per closed reason. */
export function censusBoundSentence(r: ControlCensusReport): string {
  const total = typeof r.total === "number" ? r.total : r.entries.length;
  if (r.boundedBy === "entries") {
    return (
      `the page has ${total} controls and the first ${r.entries.length} ` +
      "are listed; the rest are counted but not listed"
    );
  }
  if (r.boundedBy === "nodes") {
    return (
      `the scan stopped after ${MAX_CENSUS_SCAN} elements, so ${total} is a ` +
      "floor \u2014 the page has at least that many"
    );
  }
  if (r.boundedBy === "shared-handle") {
    return (
      `${total} distinct positions were counted and some controls share ` +
      "one \u2014 past a long run of identical rows a position stops " +
      `being individual \u2014 so ${total} is a floor: the page has at ` +
      "least that many"
    );
  }
  if (r.boundedBy === "time") {
    return (
      `the scan stopped at its ${CENSUS_BUDGET_MS}ms budget rather than slow ` +
      `the page, so ${total} is a floor \u2014 the page has at least that many`
    );
  }
  // Not bounded is still an answer, and a reader who is handed only this
  // sentence needs the SCOPE — a census is what was rendered at that
  // moment, which is smaller than "every control the app has".
  return (
    "it is not bounded: this is every control the page had rendered at " +
    "that moment, and a control behind a closed menu or an unopened " +
    "dialog is not rendered, so it is not in the count"
  );
}

/** The compact caption for the bubble panel's row. */
export function controlCensusPanelText(r: ControlCensusReport): string {
  if (r.status === "off") return "off";
  if (r.status === "unsupported") return "not readable here";
  if (r.status === "unreadable") return "page refused";
  if (r.status === "no-identity") return "no control identity here";
  const total = typeof r.total === "number" ? r.total : r.entries.length;
  const used = r.entries.filter((e) => e.used).length;
  const deadControls = r.entries.filter((e) => (e.dead ?? 0) > 0).length;
  const parts = [
    `${total} ${total === 1 ? "control" : "controls"}`,
    `${used} pressed`,
  ];
  if (deadControls > 0) parts.push(`${deadControls} did nothing`);
  if (r.bounded === true) parts.push("bounded");
  const offCensus = typeof r.usedOffCensus === "number" ? r.usedOffCensus : 0;
  if (offCensus > 0) parts.push(`${offCensus} off-census`);
  return parts.join(" \u00b7 ");
}

/** @internal test hook — clear the census between hermetic runs. */
export function _resetControlCensusForTests(): void {
  exercised.clear();
  enabledOverride = null;
}
