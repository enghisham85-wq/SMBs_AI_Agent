/** Accumulated page map — the web runtime's self-drawing navigation graph.
 *
 * The kit already observes every page change on ANY router with zero setup:
 * the initial location, `popstate`, `hashchange`, and a guest-safe wrap of
 * `pushState`/`replaceState` (see `vitals.ts`). Until now it threw the path
 * away after checking for a bounce and forgot everything on reload, so the map
 * was only ever as complete as one visit.
 *
 * This module keeps what that observation already produced: a bounded, counted
 * set of page NODES and the EDGES between them, accumulated across reloads in
 * the kit's own namespaced local-storage wrapper. No new listener, no
 * router-specific hook, no element inspection — the only input is the
 * normalized route label `recordVisit()` already computed.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * PRIVACY. Labels are the same normalized, non-identifying route labels the
 * reachability registry uses (`normalizeRouteLabel` collapses ids/uuids/hex
 * and never reads the query string or hash). Storage is the host page's OWN
 * same-origin localStorage under the kit's `boosthis:` prefix.
 *
 * This module itself uploads nothing. It hands its contents out in three
 * places and no others: `getPageMapStats()` (all-numeric, for the dev
 * bubble), `getPageMapGraph()` (the on-device panel), and — only when the
 * host opted in with `sendPageMap` — `pageMapWire.ts`, which turns the graph
 * into the bounded, label-only shape that may ride the snapshot upload.
 *
 * What may never travel, from here or anywhere: the control that was pressed,
 * the gesture, a coordinate, element text, an ARIA label. The wire has no
 * field for one. See docs/page-map-wire-contract.md.
 *
 * COST: recording is in-memory only — there is deliberately NO storage write
 * on the navigation hot path. Saving is coalesced onto the lifecycle moments
 * the kit already flushes on (`pagehide` and `visibilitychange`→hidden), and
 * the serialized map is capped so that write stays small and bounded.
 *
 * LIMITATION (stated, not worked around): a hash-only router puts the whole
 * route in `location.hash`, which the privacy rule never reads — every page of
 * such an app collapses to one label, so the map correctly shows a single node
 * rather than inventing a graph it cannot see.
 * ───────────────────────────────────────────────────────────────────────── */

import { storage } from "./storage";

/** Local-storage key (namespaced by the storage wrapper). */
const STORAGE_KEY = "pagemap.v1";

/** Cap on distinct pages kept. Beyond it the least-visited page is evicted. */
export const MAX_MAP_NODES = 200;
/** Cap on distinct links kept. Beyond it the least-traversed link is evicted. */
export const MAX_MAP_EDGES = 400;
/** Cap on the serialized map written to storage, in UTF-8 BYTES (not JS
 *  string length: a non-ASCII page label costs up to four bytes per
 *  character, so counting characters would quietly break the promise). */
export const MAX_MAP_STORED_BYTES = 16 * 1024;

/** Separator between the two halves of an edge key. Matches the RN kit. */
const EDGE_SEP = "\u2192"; // →

interface Counted {
  /** Times this page was opened / this link was traversed. */
  count: number;
  /** Monotonic recency stamp — the tie-break when counts are equal. */
  seq: number;
}

let nodes = new Map<string, Counted>();
let edges = new Map<string, Counted>();
let seq = 0;
/** True once anything was evicted — the map is showing a SUBSET. */
let truncated = false;
/** Last label seen this page-load; the source of the next edge. */
let prevLabel: string | null = null;
/** Something changed since the last save. */
let dirty = false;
let hydrated = false;

/**
 * How the accumulated map is being kept. Four distinguishable answers — an
 * unsaved map and a map that cannot be saved must never look alike:
 *
 *  - `not-saved-yet`  nothing has been written OR read back yet, so whether
 *                     this map is being kept is not yet known.
 *  - `stored`         the map is in the browser's durable store — either the
 *                     last save reached it, or this page-load read an earlier
 *                     visit's map back out of it.
 *  - `memory-only`    there is no durable store here (SSR, storage disabled),
 *                     so the map lives for this page-load only.
 *  - `unavailable`    a durable store exists but rejected the write (quota,
 *                     blocked embed) — the map did not survive.
 */
export type PageMapPersistence =
  | "not-saved-yet"
  | "stored"
  | "memory-only"
  | "unavailable";

let persistence: PageMapPersistence = "not-saved-yet";

export interface PageMapStats {
  /** Distinct pages the map holds (accumulated across reloads). */
  pageCount: number;
  /** Distinct links between those pages. */
  linkCount: number;
  /** Total page opens counted (sum of every page's count). */
  visitCount: number;
  /** True when a cap evicted something — the map is showing a subset. */
  truncated: boolean;
  /** Whether the accumulated map is actually being kept. */
  persistence: PageMapPersistence;
}

export interface PageMapNode {
  label: string;
  count: number;
}

export interface PageMapEdge {
  from: string;
  to: string;
  count: number;
}

/* ─── Recording (hot path — in memory only) ──────────────────────────── */

/** Bump a counted entry, creating it when absent. */
function bump(map: Map<string, Counted>, key: string): void {
  const rec = map.get(key);
  seq++;
  if (rec) {
    rec.count++;
    rec.seq = seq;
  } else {
    map.set(key, { count: 1, seq });
  }
}

/** Key of the entry that should go first when a cap is reached: the least
 *  traversed, and among equals the least recently seen. */
function weakest(map: Map<string, Counted>): string | null {
  let worstKey: string | null = null;
  let worst: Counted | null = null;
  for (const [key, rec] of map) {
    if (
      !worst ||
      rec.count < worst.count ||
      (rec.count === worst.count && rec.seq < worst.seq)
    ) {
      worst = rec;
      worstKey = key;
    }
  }
  return worstKey;
}

/** Drop the edges that touch a page we just evicted, so the map never holds a
 *  link to a node it no longer knows about. */
function dropEdgesTouching(label: string): void {
  for (const key of Array.from(edges.keys())) {
    const sep = key.indexOf(EDGE_SEP);
    if (sep < 0) continue;
    if (key.slice(0, sep) === label || key.slice(sep + 1) === label) {
      edges.delete(key);
    }
  }
}

/** Forget a page the caps dropped, everywhere it could still be named.
 *
 *  Dropping the node is not enough. `prevLabel` is the page the NEXT
 *  navigation draws its edge FROM, and a page can be evicted the moment it is
 *  first opened (a one-off visit among 200 well-travelled ones is the weakest
 *  thing on the map). Left alone, the next navigation would then draw a link
 *  out of a page the map no longer holds, and no later eviction would ever
 *  remove it — the map would report a link to a node it cannot show. */
function forgetDroppedPages(): void {
  if (prevLabel !== null && !nodes.has(prevLabel)) prevLabel = null;
  for (const key of Array.from(edges.keys())) {
    const sep = key.indexOf(EDGE_SEP);
    if (
      sep <= 0 ||
      sep >= key.length - 1 ||
      !nodes.has(key.slice(0, sep)) ||
      !nodes.has(key.slice(sep + 1))
    ) {
      edges.delete(key);
    }
  }
}

/** Hold both caps. Only ever does work when a cap is actually reached. */
function enforceCaps(): void {
  let droppedNode = false;
  while (nodes.size > MAX_MAP_NODES) {
    const key = weakest(nodes);
    if (key === null) break;
    nodes.delete(key);
    dropEdgesTouching(key);
    truncated = true;
    droppedNode = true;
  }
  while (edges.size > MAX_MAP_EDGES) {
    const key = weakest(edges);
    if (key === null) break;
    edges.delete(key);
    truncated = true;
  }
  // Only ever walks the edges when a page actually went — the hot path pays
  // nothing until a cap bites.
  if (droppedNode) forgetDroppedPages();
}

/**
 * Record one page change into the map. Called from the navigation the kit
 * already observes — never by a listener of its own.
 *
 * Consecutive duplicates are ignored (a `replaceState` refresh is not a
 * navigation), so a page never links to itself. In-memory only: no storage
 * write happens here.
 */
export function noteMapVisit(label: string): void {
  try {
    if (typeof label !== "string" || label.length === 0) return;
    if (label === prevLabel) return;
    const from = prevLabel;
    prevLabel = label;
    bump(nodes, label);
    if (from !== null) bump(edges, from + EDGE_SEP + label);
    enforceCaps();
    dirty = true;
  } catch {
    // the map must never break the host page
  }
}

/* ─── Reading ────────────────────────────────────────────────────────── */

/** Current map size + honesty flags. CLOSED, all-numeric-plus-enum shape —
 *  never a route label — so the dev bubble can show how complete the map is
 *  without any label leaving this module. */
export function getPageMapStats(): PageMapStats {
  let visits = 0;
  for (const rec of nodes.values()) visits += rec.count;
  return {
    pageCount: nodes.size,
    linkCount: edges.size,
    visitCount: visits,
    truncated,
    persistence,
  };
}

/**
 * Whether the accumulated map already holds this page.
 *
 * The label must be the same normalized route label recording uses — the
 * reachability registry produces exactly that one, so the two sides can never
 * disagree about what "the same page" means. Answers yes/no and never hands a
 * label back, so a count-only caller stays label-free.
 */
export function pageMapHas(label: string): boolean {
  return typeof label === "string" && label.length > 0 && nodes.has(label);
}

/** The accumulated graph, busiest first. ON-DEVICE ONLY — consumed by the
 *  kit's own panel, never by any upload path. */
export function getPageMapGraph(): {
  nodes: PageMapNode[];
  edges: PageMapEdge[];
} {
  const nodeList: PageMapNode[] = Array.from(nodes.entries())
    .map(([label, rec]) => ({ label, count: rec.count }))
    .sort((a, b) => b.count - a.count || (a.label < b.label ? -1 : 1));
  const edgeList: PageMapEdge[] = [];
  for (const [key, rec] of edges) {
    const sep = key.indexOf(EDGE_SEP);
    if (sep <= 0 || sep >= key.length - 1) continue;
    // A link is only shown between two pages the map still holds: handing out
    // an endpoint the node list dropped would put a page back on the drawing
    // that the cap says is gone.
    if (!nodes.has(key.slice(0, sep)) || !nodes.has(key.slice(sep + 1))) {
      continue;
    }
    edgeList.push({
      from: key.slice(0, sep),
      to: key.slice(sep + 1),
      count: rec.count,
    });
  }
  edgeList.sort((a, b) => b.count - a.count || (a.from < b.from ? -1 : 1));
  return { nodes: nodeList, edges: edgeList };
}

/* ─── Persistence (lifecycle moments only) ───────────────────────────── */

interface StoredMap {
  /** Schema version. */
  v: 1;
  /** Was anything evicted before this was written? */
  t: boolean;
  /** [label, count] */
  n: [string, number][];
  /** [from, to, count] */
  e: [string, string, number][];
}

/**
 * UTF-8 byte length of a string, counted by hand.
 *
 * The cap is a promise about BYTES, and `String.length` counts UTF-16 units:
 * a page labelled in Arabic, Japanese or emoji would be measured at a third
 * to a half of what it actually writes, so the stored map could sail well
 * past the cap while the code believed it was inside it. `TextEncoder` is
 * not assumed — this kit runs in old browsers and in SSR shells — and this
 * only ever runs on the flush path, never on the navigation hot path.
 */
function utf8Bytes(s: string): number {
  let n = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c < 0x80) n += 1;
    else if (c < 0x800) n += 2;
    else if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) {
      const next = s.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        n += 4; // surrogate PAIR — one 4-byte code point
        i++;
      } else {
        n += 3; // lone surrogate — replacement character
      }
    } else n += 3;
  }
  return n;
}

/** Serialize the map, dropping the weakest entries until it fits the byte cap.
 *  Returns the JSON plus whether the cap itself forced anything out. */
function serialize(): { json: string; shed: boolean } {
  const graph = getPageMapGraph();
  let n = graph.nodes.map((x): [string, number] => [x.label, x.count]);
  let e = graph.edges.map((x): [string, string, number] => [
    x.from,
    x.to,
    x.count,
  ]);
  let shed = false;
  const render = (): string =>
    JSON.stringify({ v: 1, t: truncated || shed, n, e } satisfies StoredMap);
  let json = render();
  // Shed the least-traversed links first (they are the cheapest to lose), then
  // the least-visited pages, until the written map is inside the cap. Both
  // lists are busiest-first, so dropping from the tail sheds the weakest.
  while (utf8Bytes(json) > MAX_MAP_STORED_BYTES && e.length > 0) {
    e = e.slice(0, e.length - Math.max(1, Math.ceil(e.length / 8)));
    shed = true;
    json = render();
  }
  while (utf8Bytes(json) > MAX_MAP_STORED_BYTES && n.length > 1) {
    n = n.slice(0, Math.max(1, n.length - Math.max(1, Math.ceil(n.length / 8))));
    shed = true;
    json = render();
  }
  return { json, shed };
}

/**
 * Write the accumulated map. Call ONLY from a lifecycle moment the kit
 * already flushes on — never per navigation. Cheap and idempotent: a map that
 * has not changed since the last save writes nothing.
 */
export function savePageMap(): void {
  try {
    if (!dirty) return;
    if (nodes.size === 0) return;
    const { json, shed } = serialize();
    if (shed) truncated = true;
    const reached = storage.set(STORAGE_KEY, json);
    persistence = reached
      ? "stored"
      : storage.isPersistent()
        ? "unavailable"
        : "memory-only";
    dirty = false;
  } catch {
    // a save must never break the unload path
  }
}

/**
 * Read the map saved by earlier visits and MERGE it into whatever this
 * page-load has already seen (counts add), so the map grows rather than being
 * replaced. Idempotent — a second call is a no-op.
 */
export function loadPageMap(): void {
  if (hydrated) return;
  hydrated = true;
  try {
    const raw = storage.get(STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw) as Partial<StoredMap> | null;
    if (!parsed || parsed.v !== 1) return;
    if (Array.isArray(parsed.n)) {
      for (const entry of parsed.n) {
        if (!Array.isArray(entry)) continue;
        const [label, count] = entry;
        if (typeof label !== "string" || !label) continue;
        if (typeof count !== "number" || !Number.isFinite(count) || count <= 0) {
          continue;
        }
        seq++;
        const rec = nodes.get(label);
        if (rec) rec.count += Math.floor(count);
        else nodes.set(label, { count: Math.floor(count), seq });
      }
    }
    if (Array.isArray(parsed.e)) {
      for (const entry of parsed.e) {
        if (!Array.isArray(entry)) continue;
        const [from, to, count] = entry;
        if (typeof from !== "string" || !from) continue;
        if (typeof to !== "string" || !to) continue;
        if (typeof count !== "number" || !Number.isFinite(count) || count <= 0) {
          continue;
        }
        // Never resurrect a link whose pages the caps already dropped.
        if (!nodes.has(from) || !nodes.has(to)) continue;
        seq++;
        const key = from + EDGE_SEP + to;
        const rec = edges.get(key);
        if (rec) rec.count += Math.floor(count);
        else edges.set(key, { count: Math.floor(count), seq });
      }
    }
    if (parsed.t === true) truncated = true;
    // A map that came BACK is a map that was kept: saying "not saved yet"
    // here would describe a demonstrably stored map as an unknown. Where no
    // durable store is reachable the read can only have come from the
    // in-memory fallback, which dies with the page — so it says that instead.
    persistence = storage.isPersistent() ? "stored" : "memory-only";
    enforceCaps();
  } catch {
    // Corrupt or unreadable — start from what this page-load can see.
  }
}

/** @internal test hook — drop the accumulated map (memory AND storage) so no
 *  test inherits another's map. */
export function _resetPageMapForTests(): void {
  nodes = new Map<string, Counted>();
  edges = new Map<string, Counted>();
  seq = 0;
  truncated = false;
  prevLabel = null;
  dirty = false;
  hydrated = false;
  persistence = "not-saved-yet";
  try {
    storage.remove(STORAGE_KEY);
  } catch {
    // ignore
  }
}
