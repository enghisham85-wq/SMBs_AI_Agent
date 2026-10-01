/** Per-call failure counts — "which call is FAILING", not just which is slow.
 *
 * WHAT THIS IS, AND WHAT IT IS NOT. The call watcher already decides, for
 * every watched call, whether it completed or failed — `outcomeForResponse`
 * classifies a 400-or-worse reply as failed, and `outcomeForError` classifies
 * a rejection. The span emitter already knows how to build a normalised route
 * label out of the same call's method and address. Both facts are in hand at
 * the same instant and are then thrown apart: the outcome goes to an aggregate
 * bucket keyed by a coarse destination GROUP, and the label goes onto a span
 * only when this call happens to be traced. This module is the join. It
 * observes NOTHING new — it wraps no method the call watcher has not already
 * wrapped, and it is handed values the settle path had already computed.
 *
 * WHAT TRAVELS. Counts, and only counts. The labels are the KEY of a local
 * map and never leave the page: what this module puts on the wire is four
 * numbers — how many answers were observed, how many failed, how many
 * distinct parts are behind those two, and how many observations the cap
 * turned away. So the browser's reading is strictly narrower than the server
 * kits': they carry a label the server already stores, this one carries none.
 * There is no field here that could hold a URL, a message, a stack, a body or
 * a header, and no parameter that could carry one.
 *
 * WHY THE LABEL STAYS HOME. A page calls its own backend, and it also calls
 * Stripe, an AI provider, a map tile server. A THIRD PARTY'S path is not this
 * project's route, and shipping one as though it were would put somebody
 * else's address into this project's picture. The per-part detail the map
 * draws for the browser already rides the span path, which is same-decision
 * and already reviewed; this module exists to report the APP-WIDE reading
 * honestly, over every watched call rather than only the sampled ones.
 *
 * WHAT COUNTS AS FAILED — AND HOW IT DIFFERS FROM THE SERVER KITS. Whatever
 * `outcomeForResponse` / `outcomeForError` already call a failure: a reply of
 * 400 or worse, or a call that rejected. That is deliberately WIDER than the
 * server kits, which count the 5xx class alone. The asymmetry is real and is
 * stated rather than smoothed over: a server knows a 404 it issued is its own
 * answer to a caller's bad request, and a browser making the call does not —
 * from here a 404 is simply an address that did not work. An abort and a
 * caller-side timeout are NOT failures here, because the page caused them.
 *
 * ABSENCE IS NOT ZERO. A label this module never saw has no entry, and an
 * app that made no watched calls reports no reading at all rather than a
 * zero. When the label cap fills, further NEW labels are not invented as
 * empty rows: they are untracked, and the number of observations that went
 * untracked is reported so the gap is visible rather than silent.
 */

import { spanLabel } from "./spanEmitter";
import type { CallOutcome } from "./networkSampler";

/** How many distinct call labels this module will track. Matches the Node
 *  kit's cap and the server's per-day part cap, so no runtime can build a
 *  picture the project's own history cannot hold. */
export const MAX_TRACKED_ROUTE_OUTCOMES = 200;

/** One part's answers, for the life of this page. */
export interface RouteOutcomeCounts {
  /** Watched calls whose end this module could judge. */
  observed: number;
  /** Of those, how many the call watcher had already called a failure. */
  failed: number;
}

let counts = new Map<string, RouteOutcomeCounts>();
/** Observations that arrived for a label the cap would not let us start
 *  tracking. Reported, never hidden: it is the difference between "this part
 *  had no failures" and "we stopped looking". */
let untracked = 0;

/** Pull an address out of whatever `fetch` was handed. Mirrors the extraction
 *  the call watcher already does for its group key; never throws. */
function urlOf(input: unknown): string {
  try {
    if (typeof input === "string") return input;
    const href = (input as { href?: unknown } | null)?.href;
    if (typeof href === "string") return href;
    const url = (input as { url?: unknown } | null)?.url;
    if (typeof url === "string") return url;
  } catch {
    /* fall through */
  }
  return "";
}

/** Does this outcome mean the work did not get done?
 *
 *  `aborted` and `timed-out` are the PAGE's own doing — a navigation away, a
 *  cancelled search-as-you-type, a deadline the caller chose — so they are
 *  neither observed nor failed here. Counting them would make a well-behaved
 *  app that cancels its own stale requests look broken. */
function judged(outcome: CallOutcome): { counts: boolean; failed: boolean } {
  if (outcome === "completed") return { counts: true, failed: false };
  if (outcome === "failed") return { counts: true, failed: true };
  return { counts: false, failed: false };
}

/** Record how one watched call ENDED, against the label built from the same
 *  call's method and address. Never throws — instrumentation must not break
 *  the page. */
export function noteRouteOutcome(
  input: unknown,
  method: unknown,
  outcome: CallOutcome,
): void {
  try {
    const address = urlOf(input);
    if (address === "") return;
    noteRouteOutcomeByLabel(
      spanLabel(typeof method === "string" && method ? method : "GET", address),
      outcome,
    );
  } catch {
    /* swallow */
  }
}

/** The same reading, for a caller that built the label earlier — XHR settles
 *  on an event long after `open()`, so the label is derived once, where the
 *  method and address are both in hand, and handed back here. */
export function noteRouteOutcomeByLabel(
  label: unknown,
  outcome: CallOutcome,
): void {
  try {
    const verdict = judged(outcome);
    if (!verdict.counts) return;
    if (typeof label !== "string" || label.length === 0) return;
    let row = counts.get(label);
    if (!row) {
      if (counts.size >= MAX_TRACKED_ROUTE_OUTCOMES) {
        untracked++;
        return;
      }
      row = { observed: 0, failed: 0 };
      counts.set(label, row);
    }
    row.observed++;
    if (verdict.failed) row.failed++;
  } catch {
    /* swallow */
  }
}

/** Correct one already-counted answer for a label: the transport reported a
 *  normal completion and the payload then turned out to carry an error.
 *
 *  This is the opt-in body-error check's other half. The aggregate call
 *  reading reclassifies the same call at the same moment, and the two must
 *  move together — a page whose reading says "one call failed" while its part
 *  says "none of 1 answers failed" is the exact contradiction this reading
 *  exists to remove.
 *
 *  It never creates a row (a label with no observation has seen nothing, and
 *  a correction is not an observation) and never pushes `failed` past
 *  `observed`. Never throws. */
export function noteRouteBodyFailureByLabel(label: unknown): void {
  try {
    if (typeof label !== "string" || label.length === 0) return;
    const row = counts.get(label);
    // No row: the cap turned this label away, or the settle path never ran.
    // Nothing to correct, and inventing a row here would manufacture an
    // observation out of a correction.
    if (!row) return;
    if (row.failed >= row.observed) return;
    row.failed++;
  } catch {
    /* swallow */
  }
}

/** The same correction, for the fetch path, which holds the call's own input
 *  and method rather than the label. The label is rebuilt exactly as the
 *  settle path built it, so the correction lands on the row it corrects. */
export function noteRouteBodyFailure(input: unknown, method: unknown): void {
  try {
    const address = urlOf(input);
    if (address === "") return;
    noteRouteBodyFailureByLabel(
      spanLabel(typeof method === "string" && method ? method : "GET", address),
    );
  } catch {
    /* swallow */
  }
}

/** App-wide totals for the axis: how many parts carry a reading, how many
 *  answers were observed across all of them, how many failed, and how many
 *  observations the label cap turned away. Null when nothing has been
 *  observed at all — "nothing seen" is not a row of zeroes. */
export interface RouteOutcomeTotals {
  parts: number;
  observed: number;
  failed: number;
  untracked: number;
}

export function getRouteOutcomeTotals(): RouteOutcomeTotals | null {
  let observed = 0;
  let failed = 0;
  for (const row of counts.values()) {
    observed += row.observed;
    failed += row.failed;
  }
  if (counts.size === 0 && untracked === 0) return null;
  return { parts: counts.size, observed, failed, untracked };
}

/** Clear everything. Called by `forget()` and by the disable path, exactly
 *  like every other in-page meter. */
export function resetRouteOutcomes(): void {
  counts = new Map();
  untracked = 0;
}
