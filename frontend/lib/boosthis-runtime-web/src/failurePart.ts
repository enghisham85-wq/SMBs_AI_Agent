/**
 * WHICH PART OF THE APP A FAILURE WAS IN.
 *
 * This kit captured crashes from the beginning and never recorded WHERE. The
 * two ids it attaches when an action is open — a trace id and a span id — are
 * random correlation tags identifying AN OPERATION; neither is screen-bearing
 * and neither ever answered "which screen?". So a developer was told the app
 * crashes and left to find where, while a slow press on the very same screen
 * arrived labelled, because timing findings ride the sample path.
 *
 * This module answers the question, from the navigation state the kit ALREADY
 * reads for every other reading it takes — the document's own address, put
 * through the ONE part-name rule in `partName.ts`. No new source of truth, and
 * nothing remembered between calls.
 *
 * THE THREE HONEST ANSWERS, and why the third exists:
 *
 *   • a part name — the screen the document is on, normalized by the same rule
 *     and to the same bound as every sample's label, so a crash and a slow
 *     press on one screen join without translating anything;
 *   • an unknown TERM — the kit looked and could not tell;
 *   • nothing at all — not produced here. An absent field on the wire means a
 *     kit that was never taught to send one, which is a different fact from
 *     either of the above and must never be worded as a failure that happened
 *     nowhere.
 *
 * NEVER GUESS. This reads `location.pathname` at the moment of capture and
 * holds no state of its own, so it cannot serve a screen the app has already
 * left. Where there is no document to ask — a web worker, a server-side
 * render — it says so rather than naming the last page anything saw.
 *
 * BOOSTHIS_FAILURE_PART_V1 — copy of the shared vocabulary in
 * `lib/failure-part-vocabulary.json`.
 * `scripts/src/__tests__/failure-part-coverage.test.ts` fails if this copy
 * drifts from it.
 */

import { normalizeRouteLabel } from "./partName";

/** What this kit says when it could not name the part. Closed set, the same
 *  three words on every runtime we ship. */
export const FAILURE_PART_UNKNOWN = {
  /** Nothing was open: no document to ask — a worker, or a render with no
   *  page. */
  noPartOpen: "no-part-open",
  /** No screen state was readable at this moment. */
  notTracked: "part-not-tracked",
  /** A screen was known and the one part-name rule refused its name. */
  refused: "part-refused",
} as const;

export type FailurePartUnknown =
  (typeof FAILURE_PART_UNKNOWN)[keyof typeof FAILURE_PART_UNKNOWN];

/** The part, or the reason there is not one. Exactly one key is ever set. */
export interface FailurePart {
  routeLabel?: string;
  partUnknown?: FailurePartUnknown;
}

/**
 * Which screen the code calling this is on, right now.
 *
 * Never throws: a reporter that could be taken down by its own placement
 * would cost the failure report the part exists to explain.
 */
export function currentFailurePart(): FailurePart {
  try {
    if (typeof location === "undefined" || typeof location.pathname !== "string") {
      return { partUnknown: FAILURE_PART_UNKNOWN.noPartOpen };
    }
    const label = normalizeRouteLabel(location.pathname);
    // The one part-name rule refused this address — too long, or it
    // normalized to nothing. The reading is kept and the name is not; the
    // server reaches the same verdict independently and stores the same word.
    if (label === null) return { partUnknown: FAILURE_PART_UNKNOWN.refused };
    return { routeLabel: label };
  } catch {
    return { partUnknown: FAILURE_PART_UNKNOWN.notTracked };
  }
}

/**
 * Where a FINDING happened, read off the name the detector already filed it
 * under.
 *
 * A finding is accumulated over time rather than caught on a stack, so there
 * is no "now" to read the address bar at — and reading it at report time would
 * name whichever screen the visitor had wandered to by then, which is the
 * guess this whole change exists to refuse. What there is is the name the
 * detector gave it, and for the screen-shaped detectors that name IS the part:
 * it is the very label the sample path filed the page view under.
 *
 * WHICH NAMES QUALIFY IS DECIDED BY THE PART-NAME RULE, NOT BY A LIST OF
 * DETECTORS. `outbound-calls` and `outbound-fan-out` are real names and
 * neither is a screen of this app. Rather than keep a list of which kinds are
 * screen-shaped — which would be wrong the first time a detector is added —
 * the name is offered to the one part-name rule and accepted only if it comes
 * back UNCHANGED.
 */
export function findingPart(name: unknown): FailurePart {
  try {
    if (typeof name !== "string" || name.length === 0) {
      return { partUnknown: FAILURE_PART_UNKNOWN.notTracked };
    }
    const label = normalizeRouteLabel(name);
    // Unchanged, or not a part. A name the rule had to REWRITE — a detector's
    // own word, which it would prefix with a slash — was never a screen.
    if (label !== name) return { partUnknown: FAILURE_PART_UNKNOWN.notTracked };
    return { routeLabel: label };
  } catch {
    return { partUnknown: FAILURE_PART_UNKNOWN.notTracked };
  }
}
