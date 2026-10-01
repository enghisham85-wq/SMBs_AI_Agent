/**
 * controlMap — the browser kit's on-device "press this, it opens that" record.
 *
 * WHY THIS EXISTS
 * ---------------
 * Every click in the host app already passes through ONE listener this kit
 * installs on the document (`onDocumentClick` in vitals.ts). That listener
 * walks up to eight ancestors deciding whether what was clicked is a genuine
 * control — a button, a link, something with a control role, an inline click
 * handler, a tabindex, a pointer cursor — waits the dead-click settle window,
 * and then increments one of two counters. It threw away EVERYTHING about
 * which control it was. So the honest position was never "we cannot see
 * buttons": we see every one of them and chose to keep nothing.
 *
 * This module keeps the one thing that is safe to keep, and joins it to what
 * happened next. A control followed by a page change becomes an arrow —
 * *this control opened that page*. A control followed by nothing at all
 * becomes a dead end, the browser counterpart of the phone kit's
 * dead-end-tap finding.
 *
 * WHAT IT CAN AND CANNOT SEE
 * --------------------------
 * An arrow is an IN-PAGE navigation: the address changed while the same
 * document stayed loaded, which is what every routed web app does. A press
 * that loads a WHOLE NEW DOCUMENT is outside what this map can hold — the
 * page, and this map with it, is gone before the window closes. Nothing is
 * guessed in its place, and the surfaces say so in words.
 *
 * Two presses can also be impossible to tell apart. If a second control is
 * pressed before the first one's window has closed, either of them could have
 * caused the page change that follows, so only the LATEST press may claim it;
 * the earlier one is counted and reported as unjoined rather than credited
 * with a page it may not have opened. The same applies to a window that did
 * not run on time at all (a frozen tab, a back/forward-cache restore): what
 * happened in that gap is unknowable, so nothing is claimed.
 *
 * WHAT A CONTROL MAY BE NAMED BY
 * ------------------------------
 * ONLY a code-defined test identifier: one of the `data-testid`-family
 * attributes in `CONTROL_ID_ATTRIBUTES` below, which a developer writes into
 * their own markup for their own tests. It is written by a person, it is part
 * of the code, and it is not where customer data is put.
 *
 * WHAT IS NEVER READ — not here and not on the click path
 * -------------------------------------------------------
 *   - the control's visible text (`textContent`, `innerText`)
 *   - its accessibility name (`aria-label`, `aria-labelledby`, `title`, `alt`)
 *   - its value, placeholder, `name`, `href`, or any other attribute that can
 *     carry content
 *   - its `id` or its classes — both routinely carry record identifiers
 *     ("invoice-4471", "row-user-882")
 *   - where it is on the screen
 *
 * "Delete invoice 4471 for Acme Corp" is a customer's data sitting in a label,
 * and reading labels is the line this product does not cross. The reasoning is
 * recorded in `docs/decisions/control-identity-test-ids-only.md`.
 *
 * WHERE IT GOES
 * -------------
 * Nowhere. THIS map is on-device: it feeds the kit's own bubble panel and its
 * status readout, and the test identifier it keeps is never added to any
 * uploaded payload. A person typed that identifier, so it may not travel, and
 * the current answer is still no. The RN kit's `circuitMap.ts` keeps the same
 * posture for the same reason.
 *
 * What DOES travel is a different thing with a different identity: the
 * control census in `controlCensus.ts`, which carries a count and a hash of
 * each control's POSITION in the tree, and nothing readable at all. This
 * module hands it the press — one join point, no second listener — and never
 * hands it a name. See `docs/decisions/control-handle-structural.md`.
 */

import { recordControlExercised } from "./controlCensus";

/**
 * The closed list of attributes a control may be named by, in the order they
 * are tried. Every one of them exists for one purpose — letting the
 * developer's own tests find the control — which is exactly why it is safe:
 * a person typed it, and nothing generates it from a record.
 *
 * Deliberately NOT in this list: `id`, `name`, `class`, `href`, `value`,
 * `title`, `alt`, `placeholder`, `aria-*`. Each of those is either a content
 * attribute or is routinely built out of a record's identity.
 */
export const CONTROL_ID_ATTRIBUTES: readonly string[] = [
  "data-testid",
  "data-test-id",
  "data-test",
  "data-cy",
  "data-qa",
];

/**
 * Longest value accepted as a name. A test identifier a person typed is
 * short; a long one is far more likely to be generated, and a generated
 * string is the kind that carries content. Over the cap the control is not
 * named — it is still counted, and still draws its arrow, as an unnamed one.
 */
export const MAX_CONTROL_ID_LENGTH = 64;

/**
 * Whitespace is the same signal the shared route-label guard uses: a
 * code-defined identifier does not contain spaces, so a value that does is
 * treated as content and the control counts as unnamed.
 */
const CONTROL_ID_HAS_SPACE_RE = /\s/;

/**
 * Caps on the map itself, so a long-lived session cannot grow it without
 * limit. Matched to the bound already used for this kit's on-device route
 * bookkeeping (a few hundred distinct entries is far past any real app's
 * navigation surface). When a cap bites, the report SAYS how many distinct
 * sites are not shown rather than quietly dropping them.
 */
export const MAX_CONTROL_EDGES = 200;
export const MAX_CONTROL_DEAD_ENDS = 200;
/** Bound on the distinct-named-control tally (counts only, never a list). */
const MAX_NAMED_CONTROLS = 400;

/**
 * A dead end is only CLAIMED once the same control on the same page has done
 * nothing twice — byte-parity with the phone kit's `DEAD_END_MIN_REPEAT`, so
 * "leads nowhere" means the same thing on both runtimes. A single press that
 * produced nothing is counted and reported separately, never as a claim and
 * never silently dropped.
 */
export const DEAD_END_MIN_REPEAT = 2;

/**
 * How late a settle window may fire and still be believed, measured from the
 * press. Three times the 700 ms dead-click window: comfortably past any
 * ordinary timer slip, far short of a tab that was frozen or restored from
 * the back/forward cache. A window that fires later than this observed a gap
 * it cannot account for, so its press is counted and left unjoined instead of
 * being recorded as a control that leads nowhere.
 */
export const CONTROL_JOIN_MAX_LATE_MS = 2100;

/** Separator for the internal composite keys (never rendered). */
const SEP = "\u0000";

/** One observed "this control opened that page" pair. */
export interface ControlEdge {
  /** Normalized route label of the page the control was pressed on. */
  from: string;
  /** Code-defined test identifier, or null when the control has none. */
  control: string | null;
  /** Normalized route label of the page that followed. */
  to: string;
  /** How many times this exact pair was observed. */
  count: number;
}

/** One control that was pressed and produced nothing observable. */
export interface ControlDeadEnd {
  from: string;
  control: string | null;
  count: number;
}

/** The on-device map, as the panel and the status readout read it. */
export interface ControlMapReport {
  /**
   * Whether the click watch is installed at all. False means the join could
   * not run on this browser (no MutationObserver, or the kit was never
   * started) — a "cannot tell", never a zero.
   */
  watching: boolean;
  /** Actionable presses joined to an outcome this session. */
  presses: number;
  /** Of those, presses on a control carrying a code-defined identifier. */
  namedPresses: number;
  /** Of those, presses on a control with no such identifier. */
  unnamedPresses: number;
  /** Distinct code-defined identifiers seen this session. */
  namedControls: number;
  /** Control → page pairs, heaviest first. */
  edges: ControlEdge[];
  /** Distinct pairs the cap refused to hold. */
  edgesNotShown: number;
  /** Controls that led nowhere, at or above the repeat gate, heaviest first. */
  deadEnds: ControlDeadEnd[];
  /** Distinct dead-end sites the cap refused to hold. */
  deadEndsNotShown: number;
  /** Sites that produced nothing ONCE — seen, counted, not yet a claim. */
  deadEndsBelowRepeatGate: number;
  /**
   * Presses that could not be joined because another actionable control was
   * pressed before this one's window closed. Either press could have caused
   * what followed, so neither an arrow nor a dead end is claimed for the
   * earlier one — it is counted here instead of being guessed at.
   */
  supersededPresses: number;
  /**
   * Presses whose window fired far later than it was set for — a frozen tab,
   * a back/forward-cache restore, a main thread stalled for seconds. Nothing
   * observed across that gap can be trusted, so the press is counted and
   * left unjoined.
   */
  suspendedPresses: number;
}

/* ─── Module state (on-device, never uploaded) ───────────────────────── */

let watching = false;
let presses = 0;
let namedPresses = 0;
let unnamedPresses = 0;
const edgeCounts = new Map<string, number>();
const deadEndCounts = new Map<string, number>();
const namedControls = new Set<string>();
let edgesNotShown = 0;
let deadEndsNotShown = 0;
let supersededPresses = 0;
let suspendedPresses = 0;

/**
 * Read a control's code-defined test identifier, or null when it has none.
 *
 * Reads ONLY the attributes in `CONTROL_ID_ATTRIBUTES`, and only on the ONE
 * element the actionable-target walk already matched — it does not walk, does
 * not read text, does not touch styles and forces no layout. Fully guarded: a
 * host that throws from `getAttribute` yields an unnamed control, never an
 * error into the page.
 */
export function readControlId(el: unknown): string | null {
  try {
    const node = el as { getAttribute?: (name: string) => string | null } | null;
    if (!node || typeof node.getAttribute !== "function") return null;
    for (const attr of CONTROL_ID_ATTRIBUTES) {
      const raw = node.getAttribute(attr);
      if (typeof raw !== "string") continue;
      const cleaned = cleanControlId(raw);
      if (cleaned !== null) return cleaned;
    }
    return null;
  } catch {
    // reading an identity must never break the host page
    return null;
  }
}

/**
 * The shape screen a value must pass to be treated as a name. Nothing here
 * tries to decide whether a string "is PII" — it refuses the shapes a
 * hand-written test identifier never has, so anything that slipped through
 * into one of those attributes is dropped rather than kept.
 */
export function cleanControlId(raw: string): string | null {
  try {
    const value = raw.trim();
    if (value.length === 0) return null;
    if (value.length > MAX_CONTROL_ID_LENGTH) return null;
    if (CONTROL_ID_HAS_SPACE_RE.test(value)) return null;
    return value;
  } catch {
    return null;
  }
}

/** Record whether the click watch is installed (called by vitals on start
 *  and on teardown). Without it the report says "cannot tell". */
export function setControlWatchInstalled(installed: boolean): void {
  watching = installed;
}

/**
 * Join one observed press to what happened next. Called from the settle timer
 * the dead-click detector already runs — never from the click path itself.
 *
 * - a window that fired too late to be believed, or a press overtaken by a
 *   later one, is COUNTED and joined to nothing — see the two flags below;
 * - a page change (`to` differs from `from`) records the arrow;
 * - nothing at all (`hadEffect` false) records a dead end;
 * - anything else (the control acted but stayed on the page) is counted as a
 *   press and draws no arrow, because none was observed.
 *
 * `suspended` is judged before `superseded`: a window that did not run on
 * time saw nothing it can report, whichever presses surrounded it.
 */
export function recordControlPress(press: {
  from: string;
  control: string | null;
  /**
   * The control's structural handle (`controlHandle.ts`), or null. Kept
   * SEPARATE from `control`: that is a name a person typed and stays on the
   * device, this is a position and may travel. Forwarded to the census below
   * so an exercised control can be told from an untouched one.
   */
  handle?: string | null;
  to: string | null;
  hadEffect: boolean;
  /** A later actionable press overtook this one before its window closed. */
  superseded?: boolean;
  /** This press's window fired later than `CONTROL_JOIN_MAX_LATE_MS`. */
  suspended?: boolean;
}): void {
  try {
    const from = typeof press.from === "string" ? press.from : "";
    if (from === "") return;
    const control = typeof press.control === "string" ? press.control : null;
    presses++;

    // The census is told about EVERY press that carries a handle, including
    // one whose join cannot be believed. The control was exercised either
    // way; what is unknown is where it went, and `joinable: false` is how the
    // census says exactly that rather than inventing a destination.
    const joinable = press.suspended !== true && press.superseded !== true;
    const toLabel =
      typeof press.to === "string" && press.to !== "" && press.to !== from
        ? press.to
        : null;
    // `hadEffect` is the dead-click detector's own verdict, reused exactly as
    // it stands and filed against the handle. It is passed only when the join
    // can be believed: an overtaken or late window saw a change it cannot
    // attribute to THIS press, so the census counts the press and records no
    // verdict rather than crediting or accusing the wrong control.
    recordControlExercised({
      // The page the press happened on, not the page it may have led to and
      // not the page showing when a snapshot is next taken.
      from,
      handle: typeof press.handle === "string" ? press.handle : null,
      to: toLabel,
      joinable,
      hadEffect: joinable ? press.hadEffect === true : null,
    });

    if (control !== null) {
      namedPresses++;
      if (namedControls.size < MAX_NAMED_CONTROLS) namedControls.add(control);
    } else {
      unnamedPresses++;
    }

    // Unjoinable presses stop here: counted, identity counted, nothing drawn.
    if (press.suspended === true) {
      suspendedPresses++;
      return;
    }
    if (press.superseded === true) {
      supersededPresses++;
      return;
    }

    const to = typeof press.to === "string" && press.to !== "" ? press.to : null;
    if (to !== null && to !== from) {
      bump(edgeCounts, `${from}${SEP}${control ?? ""}${SEP}${to}`, MAX_CONTROL_EDGES, () => {
        edgesNotShown++;
      });
      return;
    }
    if (!press.hadEffect) {
      bump(
        deadEndCounts,
        `${from}${SEP}${control ?? ""}`,
        MAX_CONTROL_DEAD_ENDS,
        () => {
          deadEndsNotShown++;
        },
      );
    }
  } catch {
    // the map is display-only — it must never break click counting
  }
}

/** Increment a bounded counter map; count the refusal when the cap bites. */
function bump(
  map: Map<string, number>,
  key: string,
  cap: number,
  onRefused: () => void,
): void {
  const existing = map.get(key);
  if (existing !== undefined) {
    map.set(key, existing + 1);
    return;
  }
  if (map.size >= cap) {
    onRefused();
    return;
  }
  map.set(key, 1);
}

/** The current on-device map. Pure read; safe to call from a render. */
export function getControlMap(): ControlMapReport {
  const edges: ControlEdge[] = [];
  for (const [key, count] of edgeCounts) {
    const [from, control, to] = key.split(SEP);
    edges.push({ from, control: control === "" ? null : control, to, count });
  }
  edges.sort(
    (a, b) =>
      b.count - a.count ||
      a.from.localeCompare(b.from) ||
      (a.control ?? "").localeCompare(b.control ?? "") ||
      a.to.localeCompare(b.to),
  );

  const deadEnds: ControlDeadEnd[] = [];
  let deadEndsBelowRepeatGate = 0;
  for (const [key, count] of deadEndCounts) {
    if (count < DEAD_END_MIN_REPEAT) {
      deadEndsBelowRepeatGate++;
      continue;
    }
    const [from, control] = key.split(SEP);
    deadEnds.push({ from, control: control === "" ? null : control, count });
  }
  deadEnds.sort(
    (a, b) =>
      b.count - a.count ||
      a.from.localeCompare(b.from) ||
      (a.control ?? "").localeCompare(b.control ?? ""),
  );

  return {
    watching,
    presses,
    namedPresses,
    unnamedPresses,
    namedControls: namedControls.size,
    edges,
    edgesNotShown,
    deadEnds,
    deadEndsNotShown,
    deadEndsBelowRepeatGate,
    supersededPresses,
    suspendedPresses,
  };
}

/**
 * How a control with no code-defined test id is drawn. It is on the map and
 * says so: an unnamed control is never dropped and never disguised as one
 * that has a name.
 */
export const UNNAMED_CONTROL_LABEL = "(unnamed)";

/** One drawn line of the map: a control, and where pressing it went. */
export interface ControlMapRow {
  kind: "arrow" | "dead-end";
  /** The code-defined test id, or `UNNAMED_CONTROL_LABEL`. */
  control: string;
  /** "/orders → /orders/new", or "/billing → nowhere". */
  detail: string;
  /** How many times it was observed. */
  count: number;
  /** The whole line as one string, for a surface that renders sentences. */
  text: string;
}

/**
 * Default number of lines a surface draws before it starts saying "and N
 * more". Small on purpose: the panel is 300px wide and the point of the map
 * is the heaviest paths, not an inventory.
 */
export const CONTROL_MAP_ROW_LIMIT = 6;

/**
 * The drawn map: arrows first (heaviest first), then dead ends. Derived once,
 * here, so the bubble panel and the status readout cannot draw two different
 * pictures of the same presses.
 *
 * `more` counts everything the caller is not being handed: lines past the
 * limit PLUS the distinct pairs the on-device cap already refused, so a
 * surface can never imply it is showing all of them.
 */
export function controlMapRows(
  r: ControlMapReport,
  limit: number = CONTROL_MAP_ROW_LIMIT,
): { rows: ControlMapRow[]; more: number } {
  const rows: ControlMapRow[] = [];
  try {
    const arrow = "\u2192";
    for (const e of r.edges) {
      rows.push(makeRow("arrow", e.control, `${e.from} ${arrow} ${e.to}`, e.count));
    }
    for (const d of r.deadEnds) {
      rows.push(
        makeRow("dead-end", d.control, `${d.from} ${arrow} nowhere`, d.count),
      );
    }
  } catch {
    return { rows: [], more: 0 };
  }
  const cap = typeof limit === "number" && limit > 0 ? limit : CONTROL_MAP_ROW_LIMIT;
  const shown = rows.slice(0, cap);
  const more = rows.length - shown.length + r.edgesNotShown + r.deadEndsNotShown;
  return { rows: shown, more: more > 0 ? more : 0 };
}

function makeRow(
  kind: "arrow" | "dead-end",
  control: string | null,
  detail: string,
  count: number,
): ControlMapRow {
  const name = control === null ? UNNAMED_CONTROL_LABEL : control;
  const times = count > 1 ? ` \u00d7${count}` : "";
  return { kind, control: name, detail, count, text: `${name}: ${detail}${times}` };
}

/* ─── One derived verdict, worded per surface ────────────────────────── */

/**
 * The compact caption for the bubble panel's row. Four answers that cannot be
 * confused: the watch is not installed, nothing has been pressed yet, and
 * otherwise what was actually found — with unnamed presses and anything the
 * cap refused said out loud rather than dropped.
 */
export function controlMapPanelText(r: ControlMapReport): string {
  if (!r.watching) return "not observable here";
  if (r.presses === 0) return "—";
  const parts = [
    r.edges.length === 1 ? "1 arrow" : `${r.edges.length} arrows`,
    r.deadEnds.length === 1 ? "1 dead end" : `${r.deadEnds.length} dead ends`,
  ];
  if (r.unnamedPresses > 0) parts.push(`${r.unnamedPresses} unnamed`);
  const unjoined = r.supersededPresses + r.suspendedPresses;
  if (unjoined > 0) parts.push(`${unjoined} unjoined`);
  const notShown = r.edgesNotShown + r.deadEndsNotShown;
  if (notShown > 0) parts.push(`${notShown} not shown`);
  return parts.join(" \u00b7 ");
}

/**
 * The scope the drawn rows are true of, for the caption beneath them. Short
 * enough for a 300px panel, and it states the boundary rather than leaving a
 * developer to infer that a full page load is covered.
 */
export const CONTROL_MAP_SCOPE_CAPTION =
  "in-page navigations \u00b7 this page view \u00b7 names stay on this device";

/**
 * The same verdict as one sentence for the status readout — the browser kit's
 * page-equivalent surface. Says what the panel row says, plus the part a
 * one-line row has no room for: how many presses came from controls with no
 * code-defined identifier, and that an unnamed control is still on the map.
 */
export function controlMapStatusLine(r: ControlMapReport): string {
  if (!r.watching) {
    return (
      "Controls \u2192 pages: not observable on this browser \u2014 the click " +
      "watch is not installed, so no press could be joined to what followed."
    );
  }
  if (r.presses === 0) {
    return (
      "Controls \u2192 pages: no control has been pressed yet on this page " +
      "view, so there is nothing to draw."
    );
  }
  const bits = [
    `${r.presses} ${r.presses === 1 ? "press" : "presses"}`,
    `${r.edges.length} ${r.edges.length === 1 ? "arrow" : "arrows"}`,
    `${r.deadEnds.length} ${r.deadEnds.length === 1 ? "dead end" : "dead ends"}`,
  ];
  let line = `Controls \u2192 pages: ${bits.join(", ")}`;
  if (r.unnamedPresses > 0) {
    line +=
      `. ${r.unnamedPresses} of those presses came from a control with no ` +
      "code-defined test id; they are counted and still draw their arrow, " +
      "just without a name";
  }
  if (r.deadEndsBelowRepeatGate > 0) {
    line +=
      `. ${r.deadEndsBelowRepeatGate} ` +
      (r.deadEndsBelowRepeatGate === 1 ? "control" : "controls") +
      ` produced nothing once \u2014 below the ${DEAD_END_MIN_REPEAT}-press ` +
      "repeat gate, so not claimed as a dead end yet";
  }
  if (r.supersededPresses > 0) {
    line +=
      `. ${r.supersededPresses} ` +
      (r.supersededPresses === 1 ? "press was" : "presses were") +
      " overtaken by another control before the window closed, so " +
      (r.supersededPresses === 1 ? "it is" : "they are") +
      " counted and joined to nothing rather than credited with a page " +
      (r.supersededPresses === 1 ? "it" : "they") +
      " may not have opened";
  }
  if (r.suspendedPresses > 0) {
    line +=
      `. ${r.suspendedPresses} ` +
      (r.suspendedPresses === 1 ? "press" : "presses") +
      " had a window that fired far too late to be believed (a frozen tab or " +
      "a restored page); nothing is claimed for " +
      (r.suspendedPresses === 1 ? "it" : "them");
  }
  const notShown = r.edgesNotShown + r.deadEndsNotShown;
  if (notShown > 0) {
    line += `. ${notShown} more did not fit the on-device cap and are not shown`;
  }
  return (
    `${line}. Arrows are in-page navigations: a press that loads a whole new ` +
    "document takes this page view, and this map, with it. The control names " +
    "on this map stay on this device \u2014 they are never uploaded. The " +
    "separate control census sends a count and each control's position, " +
    "never a name."
  );
}

/** @internal test hook — clear the on-device map between hermetic runs. */
export function _resetControlMapForTests(): void {
  watching = false;
  presses = 0;
  namedPresses = 0;
  unnamedPresses = 0;
  edgeCounts.clear();
  deadEndCounts.clear();
  namedControls.clear();
  edgesNotShown = 0;
  deadEndsNotShown = 0;
  supersededPresses = 0;
  suspendedPresses = 0;
}
