/* ─────────────────────────────────────────────────────────────────────────
 * WHAT A PART OF THE APP IS CALLED — this kit's copy of the one rule.
 *
 * BOOSTHIS_PART_NAME_V1 — shared with lib/part-name-vocabulary.json.
 * Full reasoning: docs/decisions/part-name-one-rule.md.
 *
 * A part is the piece of the app a reading belongs to: a page here, a route
 * on a server kit. At most 100 characters, refused rather than shortened, and
 * never a minted stand-in for a page this kit could not name.
 *
 * WHY ITS OWN MODULE. This kit was already the working model — its page map,
 * inventory and timings all called one normaliser — but that normaliser lived
 * in `vitals.ts`, the module that also ASSEMBLES the kit. Anything needing
 * the rule had to import the assembler, which is how the page map's wire
 * module was pulled into this kit's large module cycle. A rule every half
 * takes belongs in a leaf: this module imports nothing at all, so it has
 * nothing it COULD import that would close a loop.
 * ───────────────────────────────────────────────────────────────────────── */

/** Segments that look volatile or identifying collapse to ":id". */
const NUMERIC_SEG = /^\d+$/;
const UUID_SEG =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
/**
 * A long hex RUN anywhere in the segment, not only a segment that is nothing
 * but hex.
 *
 * The whole-segment form missed every identifier that carries a prefix. Read
 * against real addresses, `/…/snapshot/mcp-2066268df023bf5e9dd7aeb0` came
 * through untouched — a live identifier, in a label that travels — because
 * "mcp-" is not a hex character. A run is the right unit: an identifier
 * stops being opaque when it is glued to a word, not when it is 24 hex
 * characters long.
 *
 * Deliberately not narrowed to a known prefix. Nothing about "mcp" is
 * special; the next identifier shape will carry a different one.
 */
const LONG_HEX_SEG = /[0-9a-f]{16,}/i;
// BOOSTHIS_PART_NAME_V1 — shared with lib/part-name-vocabulary.json.
export const MAX_PART_NAME = 100;

/** Normalize a pathname into a stable, non-identifying route label. */
export function normalizeRouteLabel(pathname: string): string | null {
  if (typeof pathname !== "string") return null;
  let path = pathname;
  if (path.length === 0) return null;
  if (path.length > MAX_PART_NAME) return null;
  // Never read query string or hash even if handed a full URL-ish string.
  const q = path.search(/[?#]/);
  if (q >= 0) path = path.slice(0, q);
  const segs = path.split("/").map((seg) => {
    if (!seg) return seg;
    if (
      NUMERIC_SEG.test(seg) ||
      UUID_SEG.test(seg) ||
      LONG_HEX_SEG.test(seg) ||
      seg.includes("@")
    ) {
      return ":id";
    }
    return seg;
  });
  let label = segs.join("/");
  if (!label.startsWith("/")) label = "/" + label;
  if (label.length > MAX_PART_NAME) return null;
  return label || null;
}
