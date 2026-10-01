/** Novelty — "is this part of the app one we have never reported before?"
 *
 * WHY THIS EXISTS. Spans are batched: the emitter holds them and flushes on a
 * timer. That is right for the thousandth call to the same route — the map
 * does not change and nothing is waiting on it — and wrong for the FIRST one,
 * where a developer is sitting in front of the dashboard waiting for a box to
 * appear. The insight that makes an eager send affordable is that novelty is
 * rare and repetition is common: a page seen for the very first time is worth
 * sending immediately, and every traversal after that is worth batching.
 *
 * So this module answers one question about each span the emitter buffers, and
 * answers it in words rather than a boolean, because there are four honest
 * answers and only two of them mean "send now":
 *
 *   • `new-part`          — this (runtime, label) pair is new to this kit.
 *   • `new-call`          — this parent→child pair is new to this kit.
 *   • `seen`              — both have been reported before. Batch it.
 *   • `no-longer-judging` — the memory below is full, so the kit genuinely
 *                           CANNOT tell new from seen any more. Never rendered
 *                           or counted as "seen": not knowing is its own state.
 *
 * WHAT IT KNOWS, AND FOR HOW LONG. A part is keyed by runtime AND label, the
 * same pairing the server's map keys a box by (its own separator differs; the
 * pairing is the part that has to agree, so one label reported by two runtimes
 * is two parts on both sides). A call is keyed `parentLabel>childLabel` from
 * the two ends this kit can actually see. Nothing here is stored or sent:
 * the sets live in memory for the life of the page and die with it. That is a
 * deliberate limit, not an oversight — a first visit in a fresh tab therefore
 * looks novel again, and the cost of that is bounded by the emitter's rate
 * ceiling, not by this file. Remembering across sessions means writing the
 * kit's own map state to storage, which belongs with the kit-side map memory
 * rather than with a scheduling change.
 *
 * SELF-QUIETING BY CONSTRUCTION. Both sets are capped. Past the cap the kit
 * stops CLAIMING novelty rather than evicting and re-learning: an evicting set
 * would make a long-lived page rediscover labels it had already reported and
 * turn a quiet app into a permanent source of eager sends. An app that keeps
 * minting new labels therefore gets a fast map for its first few hundred parts
 * and the ordinary batched cadence afterwards, which is the right way round.
 *
 * PRIVACY. Only what is already on the span wire is read here: a code-defined
 * route label (ids already redacted by the emitter's `spanLabel`) and two
 * random hex span ids. Nothing is added to any payload — this module decides
 * WHEN the existing batch goes out, never WHAT is in it.
 */

/** How many distinct parts this kit will judge in one page session. */
export const MAX_REMEMBERED_PARTS = 200;
/** How many distinct calls (parent→child pairs) it will judge. */
export const MAX_REMEMBERED_CALLS = 200;
/**
 * How many recent span ids are kept so a child span can find its parent's
 * label. Matched to the emitter's trace-root table (50): a call whose parent
 * has already fallen out of this table is simply not judged as a call, which
 * is the honest answer — the kit no longer knows what the other end was.
 */
export const MAX_REMEMBERED_SPAN_LABELS = 50;

export type NoveltyVerdict =
  | "new-part"
  | "new-call"
  | "seen"
  | "no-longer-judging";

/** Exactly the fields novelty reads. Declared narrowly so this module can
 *  never grow a dependency on the rest of the span. */
export interface NoveltySpan {
  layer: string;
  routeLabel: string;
  spanId?: string | null;
  parentSpanId?: string | null;
}

const parts = new Set<string>();
const calls = new Set<string>();
/** spanId → the label of the span that carried it (bounded, drop-oldest). */
const spanLabels = new Map<string, string>();

/** Tally per answer, so a test — and the kit's own introspection — can tell
 *  "nothing was new" from "the kit stopped being able to tell". */
const verdicts: Record<NoveltyVerdict, number> = {
  "new-part": 0,
  "new-call": 0,
  seen: 0,
  "no-longer-judging": 0,
};

/** The part key: the runtime and the label together, which is what makes the
 *  kit's idea of a new part the same idea as a new box on the map. */
export function noveltyPartKey(layer: string, routeLabel: string): string {
  return `${layer}|${routeLabel}`;
}

function remember(set: Set<string>, key: string, cap: number): boolean | null {
  if (set.has(key)) return false;
  if (set.size >= cap) return null; // full: cannot tell, and will not guess
  set.add(key);
  return true;
}

/**
 * Judge one span and remember it.
 *
 * Called once per buffered span, from inside the emitter's existing
 * privacy gate — so a kit with no submitter wired (sharing not authorized)
 * never reaches here and remembers nothing at all.
 */
export function judgeNovelty(span: NoveltySpan): NoveltyVerdict {
  const label = span.routeLabel;
  const spanId = typeof span.spanId === "string" ? span.spanId : null;
  const parentId =
    typeof span.parentSpanId === "string" ? span.parentSpanId : null;

  // The parent's label has to be looked up BEFORE this span's own id is
  // recorded, so a span that is somehow its own parent cannot invent a call.
  const parentLabel = parentId ? (spanLabels.get(parentId) ?? null) : null;

  if (spanId) {
    spanLabels.set(spanId, label);
    while (spanLabels.size > MAX_REMEMBERED_SPAN_LABELS) {
      const oldest = spanLabels.keys().next().value;
      if (oldest === undefined) break;
      spanLabels.delete(oldest);
    }
  }

  const partNew = remember(
    parts,
    noveltyPartKey(span.layer, label),
    MAX_REMEMBERED_PARTS,
  );
  let callNew: boolean | null = false;
  if (parentLabel !== null) {
    callNew = remember(calls, `${parentLabel}>${label}`, MAX_REMEMBERED_CALLS);
  }

  // A new part outranks a new call: both mean "send now", and the part is the
  // stronger statement about the picture changing.
  let verdict: NoveltyVerdict;
  if (partNew === true) verdict = "new-part";
  else if (callNew === true) verdict = "new-call";
  else if (partNew === null || callNew === null) verdict = "no-longer-judging";
  else verdict = "seen";

  verdicts[verdict] += 1;
  return verdict;
}

/** True when this answer means the picture on the dashboard is about to gain
 *  something it has never had. The emitter sends eagerly on exactly these. */
export function isNovel(verdict: NoveltyVerdict): boolean {
  return verdict === "new-part" || verdict === "new-call";
}

/** Drop everything remembered. Called by the emitter's buffer clear, which is
 *  what `forget()` reaches — so forgetting a kit forgets what it had seen. */
export function resetNovelty(): void {
  parts.clear();
  calls.clear();
  spanLabels.clear();
  verdicts["new-part"] = 0;
  verdicts["new-call"] = 0;
  verdicts.seen = 0;
  verdicts["no-longer-judging"] = 0;
}

/** Test/introspection hooks. */
export const _noveltyInternals = {
  parts: (): number => parts.size,
  calls: (): number => calls.size,
  /** True once either set is full — the kit has stopped judging novelty. */
  exhausted: (): boolean =>
    parts.size >= MAX_REMEMBERED_PARTS || calls.size >= MAX_REMEMBERED_CALLS,
  verdicts: (): Record<NoveltyVerdict, number> => ({ ...verdicts }),
};
