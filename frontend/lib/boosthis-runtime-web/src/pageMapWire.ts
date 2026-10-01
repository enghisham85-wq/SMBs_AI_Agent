/** The ONE place the accumulated page map may become something that travels.
 *
 * `pageMap.ts` records; this module decides what — if anything — leaves the
 * device. Keeping the boundary in its own file is deliberate: there is a
 * single function to read when you want to know what a Boosthis website sends
 * about its own pages, and a single import for a test to point at.
 *
 * WHAT TRAVELS: page labels, and pairs of page labels with a count. The
 * labels are the SAME normalized route labels that already ride every perf
 * sample and every trace span — `normalizeRouteLabel` collapses ids, UUIDs
 * and hex, never reads the query string or the hash — and they go through the
 * same server-side screen on arrival.
 *
 * WHAT NEVER TRAVELS, and cannot be made to by any option: the control that
 * was pressed, the gesture that moved someone, a coordinate, an element's
 * text, its ARIA label, or anything read from the DOM. The kit knows some of
 * those (`controlMap.ts` watches presses so the panel can show them on the
 * device); the shape below has nowhere to put them. That structural
 * non-transmission is the point, not an oversight — see
 * docs/page-map-wire-contract.md.
 *
 * OFF BY DEFAULT. Nothing here runs unless the host passes
 * `sendPageMap: true`, and the server ignores the field unless it too is
 * switched on.
 */

import { getPageMapGraph, getPageMapStats } from "./pageMap";
import { MAX_PART_NAME, normalizeRouteLabel } from "./partName";

/** Wire version. Bumped only for a shape change; the server refuses any
 *  other value rather than guessing. */
export const PAGE_MAP_WIRE_VERSION = 1 as const;
/** Hard caps. These MUST match `@workspace/api-zod`'s `pageMapWire.ts` — the
 *  server rejects the whole map rather than trimming it. */
export const PAGE_MAP_WIRE_MAX_NODES = 200;
export const PAGE_MAP_WIRE_MAX_EDGES = 400;
export const PAGE_MAP_WIRE_MAX_LABEL_CHARS = MAX_PART_NAME;
/** UTF-8 BYTES, not string length: a non-ASCII label costs up to four bytes
 *  per character, so counting characters would break the promise quietly. */
export const PAGE_MAP_WIRE_MAX_BYTES = 24 * 1024;

export interface PageMapWireNode {
  label: string;
  /** Times this page was opened. A floor — see below. */
  opens: number;
}

export interface PageMapWireEdge {
  from: string;
  to: string;
  /** Times this path was walked. A floor. */
  count: number;
}

export interface PageMapWire {
  v: typeof PAGE_MAP_WIRE_VERSION;
  nodes: PageMapWireNode[];
  edges: PageMapWireEdge[];
  /** True when this map is a SUBSET of what the device saw — either the
   *  on-device store evicted something, or the trimming below dropped
   *  something to fit. The server keeps this so the page can say so. */
  truncated: boolean;
}

/** UTF-8 length of a serialized value, without allocating a second copy of
 *  the string where the platform can measure it properly. */
function byteLength(json: string): number {
  try {
    if (typeof TextEncoder !== "undefined") {
      return new TextEncoder().encode(json).length;
    }
  } catch {
    /* fall through */
  }
  // Worst case rather than a guess: never under-reports.
  return json.length * 4;
}

/**
 * The map as it would travel, or `null` when there is nothing to send.
 *
 * DROPS RATHER THAN GROWS. Over any cap the least-travelled paths go first
 * (the graph arrives busiest-first), then the least-visited pages, and the
 * result is marked `truncated`. A map is never sent over its byte ceiling and
 * never split across uploads: the next upload carries the whole map again,
 * and the server's newest-wins rule makes that correct rather than additive.
 */
export function buildPageMapWire(): PageMapWire | null {
  const graph = getPageMapGraph();
  const fits = (label: string): boolean => normalizeRouteLabel(label) !== null;

  let dropped = false;

  let nodes: PageMapWireNode[] = [];
  for (const n of graph.nodes) {
    if (!fits(n.label)) {
      dropped = true;
      continue;
    }
    nodes.push({ label: n.label, opens: n.count });
  }
  if (nodes.length > PAGE_MAP_WIRE_MAX_NODES) {
    nodes = nodes.slice(0, PAGE_MAP_WIRE_MAX_NODES);
    dropped = true;
  }
  if (nodes.length === 0) return null;

  const kept = new Set(nodes.map((n) => n.label));
  let edges: PageMapWireEdge[] = [];
  for (const e of graph.edges) {
    // Both ends must be pages that survived. An edge to a dropped page would
    // put that page back on the server's drawing.
    if (!kept.has(e.from) || !kept.has(e.to)) {
      dropped = true;
      continue;
    }
    edges.push({ from: e.from, to: e.to, count: e.count });
  }
  if (edges.length > PAGE_MAP_WIRE_MAX_EDGES) {
    edges = edges.slice(0, PAGE_MAP_WIRE_MAX_EDGES);
    dropped = true;
  }

  const stats = getPageMapStats();
  const build = (): PageMapWire => ({
    v: PAGE_MAP_WIRE_VERSION,
    nodes,
    edges,
    truncated: dropped || stats.truncated,
  });

  // Fit the byte ceiling: paths first (the pages are the more valuable half
  // and are what the navigation layer needs to show anything at all), then
  // pages. Halving rather than one-at-a-time keeps this bounded on a map that
  // is far over.
  let wire = build();
  while (byteLength(JSON.stringify(wire)) > PAGE_MAP_WIRE_MAX_BYTES) {
    if (edges.length > 0) {
      edges = edges.slice(0, Math.floor(edges.length / 2));
      dropped = true;
    } else if (nodes.length > 1) {
      nodes = nodes.slice(0, Math.floor(nodes.length / 2));
      dropped = true;
    } else {
      // One page whose label alone will not fit. Send nothing rather than
      // something malformed.
      return null;
    }
    wire = build();
  }
  return wire;
}
