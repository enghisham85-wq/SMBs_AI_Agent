/** BOOSTHIS_SPAN_WORK_V1 — what KIND of work a span measured, and whether it
 *  WORKED.
 *
 *  This kit's copy of the shared vocabulary in `lib/span-wire-vocabulary.json`.
 *  Every runtime carries the same eight kind names and the same two outcome
 *  names, spelled identically, so "a database query" found by this kit and by
 *  a Go service meet in one place on the server instead of splitting into two.
 *
 *  THE FIELDS ARE OPTIONAL AND ABSENCE IS NOT A VALUE. A span with no `kind`
 *  came from a kit that does not report one; it is not a handler, and it is not
 *  `other`. A span with no `outcome` is not "ok" — nothing was said about it.
 *  The server stores both as absent and every reading says "not reporting"
 *  rather than inventing a clean result.
 *
 *  A value not on these lists is dropped at the door rather than refusing the
 *  batch. Adding one means adding it to the shared JSON first, then to every
 *  kit's copy, then to the server's stored enum — never here alone.
 */

/** The eight kinds of work a span may name. Closed set. */
export const SPAN_WORK_KINDS = [
  "handler",
  "db",
  "http",
  "cache",
  "queue",
  "job",
  "render",
  "other",
] as const;

export type SpanWorkKind = (typeof SPAN_WORK_KINDS)[number];

/** Did the measured work succeed? Closed set, and INDEPENDENT of the rating:
 *  a fast call can fail and a slow one can succeed. */
export const SPAN_OUTCOMES = ["ok", "error"] as const;

export type SpanOutcome = (typeof SPAN_OUTCOMES)[number];

/** Return the kind unchanged if it is one of the eight, otherwise null (the
 *  span then reports no kind at all, which reads as "not reporting"). */
export function sanitizeWorkKind(value: unknown): SpanWorkKind | null {
  return typeof value === "string" &&
    (SPAN_WORK_KINDS as readonly string[]).includes(value)
    ? (value as SpanWorkKind)
    : null;
}

/** Same contract for the outcome. */
export function sanitizeOutcome(value: unknown): SpanOutcome | null {
  return typeof value === "string" &&
    (SPAN_OUTCOMES as readonly string[]).includes(value)
    ? (value as SpanOutcome)
    : null;
}

/** Decide an outcome for a call that answered with an HTTP status.
 *
 *  A refusal that is a real answer — 404, 401, a validation 422 — is `ok`: the
 *  app did what it was asked to do. Only a 5xx is the work itself failing. A
 *  call that threw before any status arrived is `error` by the caller passing
 *  `threw`. */
export function outcomeForStatus(
  status: number | null | undefined,
  threw = false,
): SpanOutcome {
  if (threw) return "error";
  return typeof status === "number" && status >= 500 ? "error" : "ok";
}
