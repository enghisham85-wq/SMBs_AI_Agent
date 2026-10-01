/**
 * Ask the router for the whole route list — browser side.
 *
 * A site's map can only draw pages someone has opened. The router, though,
 * already holds the whole list, so this module asks it for one: hand
 * `registerRouter(router)` your Vue Router or React Router object and the map
 * gets the site's whole skeleton, with pages nobody has reached yet plainly
 * marked as not seen.
 *
 * The browser is the one place this cannot be automatic. There is no ambient
 * "the router" to find — a page may have none, two, or one built by hand — so
 * this is an OPT-IN hand-off, not a discovery. `registerWebRoutes()` still
 * takes a typed list, and the two merge.
 *
 * Three rules, the same as every other kit:
 *
 * * **Ask, never read.** This calls a live router's own accessor. It does not
 *   read source, crawl links, or parse a bundler manifest.
 * * **Nothing rather than a guess.** A router shape we do not recognise loses
 *   the WHOLE read. A half list drawn as a whole map is a lie the reader
 *   cannot see.
 * * **A quiet route is "not seen", never "dead".** Nothing here judges a route
 *   for being unvisited.
 */

import { MAX_PART_NAME, normalizeRouteLabel } from "./vitals";
import { transmitLabelHasPII } from "./no-pii";

/** Router families this kit can ask. The server holds the same closed list;
 *  a name added here and not there is refused on arrival. */
export const WEB_ROUTE_SOURCE_WORDS = ["vue-router", "react-router"] as const;
export type WebRouteSource = (typeof WEB_ROUTE_SOURCE_WORDS)[number];

/** How many entries may travel. A larger site still reports its `total`, so a
 *  reader says "at least this many" rather than believing the cap. */
export const MAX_ROUTE_LIST_ENTRIES = 200;

export type RouteListStatus = "read" | "unsupported" | "unreadable" | "off";
export type RouteListOrigin = "framework" | "declared" | "both";

export interface RouteListEntry {
  label: string;
  from: RouteListOrigin;
}

export interface RouteListReport {
  status: RouteListStatus;
  source?: WebRouteSource;
  entries: RouteListEntry[];
  /** How many the merge held BEFORE the cap. */
  total: number;
}

let routerHandle: unknown = null;
let enabledOverride: boolean | null = null;

/**
 * Hand the kit your router so it can list the site's routes itself.
 *
 * A reference assignment and nothing else — the router is not walked here, so
 * this costs the page nothing at start-up and never sits in front of a paint.
 * The walk happens when a snapshot asks for one.
 *
 * Vue Router and React Router are understood today. Anything else is reported
 * as a router we could not ask, which is a different answer from a site with
 * no routes.
 */
export function registerRouter(router: unknown): void {
  try {
    routerHandle = router ?? null;
  } catch {
    // a hand-off must never break the host page
  }
}

/** Switch the whole route list off. Switched off, the block still travels
 *  saying `off`, so the server can tell a developer who turned it off from a
 *  kit too old to have it. */
export function setRouteListEnabled(on: boolean): void {
  enabledOverride = on === false ? false : true;
}

export function routeListEnabled(): boolean {
  if (enabledOverride !== null) return enabledOverride;
  try {
    const g = globalThis as { BOOSTHIS_ROUTE_LIST?: unknown };
    const v = g.BOOSTHIS_ROUTE_LIST;
    if (v === false || v === 0) return false;
    if (typeof v === "string") {
      const s = v.trim().toLowerCase();
      if (s === "0" || s === "false" || s === "off" || s === "no") return false;
    }
  } catch {
    /* a page may seal globalThis */
  }
  return true;
}

export function _resetRouteInventoryForTests(): void {
  routerHandle = null;
  enabledOverride = null;
}

// ── Labels ───────────────────────────────────────────────────────────────────

/**
 * One route pattern, made safe to travel — or `null`.
 *
 * A pattern is code, not user data: the literal segments are the developer's
 * own words and are kept. Every framework's parameter spelling collapses to
 * one placeholder so a pattern cannot carry a name through, and anything that
 * no longer looks like a pattern is dropped rather than reshaped into
 * something that would read as a route.
 */
export function safeRoutePattern(raw: unknown): string | null {
  try {
    if (typeof raw !== "string") return null;
    let text = raw.trim();
    if (!text) return null;
    if (text.length > MAX_PART_NAME) return null;
    // A fragment never belongs in a route pattern.
    if (text.includes("#")) return null;
    // Collapse the parameters FIRST, then look for leftovers. A `?` is both a
    // query string and Vue's optional-parameter marker; rejecting on it up
    // front would silently drop every optional route in the app.
    // Vue/React :param, Vue's custom-regexp :param(\d+), and the catch-alls.
    text = text
      .replace(/:[A-Za-z_$][\w$]*\([^)]*\)[?*+]?/g, ":id")
      .replace(/:[A-Za-z_$][\w$]*[?*+]?/g, ":id")
      .replace(/\{[^}]*\}/g, ":id")
      .replace(/\*{1,2}/g, ":id");
    if (!text.startsWith("/")) text = "/" + text;
    text = text.replace(/\/{2,}/g, "/");
    if (text.length > 1) text = text.replace(/\/+$/, "") || "/";
    // A pattern that still carries router syntax — or query-string syntax —
    // is one we did not fully understand. Report nothing for it rather than a
    // half-decoded label.
    if (/[()[\]{}|\\^$+?*<>\s=&]/.test(text)) return null;
    const label = normalizeRouteLabel(text);
    if (label === null) return null;
    if (transmitLabelHasPII(label) !== null) return null;
    return label;
  } catch {
    return null;
  }
}

// ── Reading a router ─────────────────────────────────────────────────────────

export interface RouterRead {
  status: Exclude<RouteListStatus, "off">;
  source?: WebRouteSource;
  labels: string[];
}

function isObj(v: unknown): v is Record<string, unknown> {
  return (typeof v === "object" || typeof v === "function") && v !== null;
}

/**
 * Ask a router object for its whole route table.
 *
 * `unsupported` — nothing here could be asked.
 * `unreadable`  — a router we recognised, in a shape we did not.
 */
export function readRouterRoutes(router: unknown): RouterRead {
  if (router === null || router === undefined) {
    return { status: "unsupported", labels: [] };
  }
  try {
    if (!isObj(router)) return { status: "unsupported", labels: [] };

    // Vue Router 4 — `getRoutes()` is a public accessor returning every
    // record, already flattened, each with its FULL path.
    if (typeof router.getRoutes === "function") {
      return readVueRouter(router);
    }

    // React Router — the route config is readable when the router object is
    // handed over (createBrowserRouter's `routes`), and children nest.
    const reactRoutes = reactRouteArray(router);
    if (reactRoutes) return readReactRouter(reactRoutes);

    return { status: "unsupported", labels: [] };
  } catch {
    // Never into the host page.
    return { status: "unreadable", labels: [] };
  }
}

function readVueRouter(router: Record<string, unknown>): RouterRead {
  const records = (router.getRoutes as () => unknown)();
  if (!Array.isArray(records)) {
    return { status: "unreadable", source: "vue-router", labels: [] };
  }
  const labels: string[] = [];
  for (const record of records) {
    if (!isObj(record)) {
      return { status: "unreadable", source: "vue-router", labels: [] };
    }
    const path = record.path;
    if (typeof path !== "string") {
      return { status: "unreadable", source: "vue-router", labels: [] };
    }
    // A record that only groups children has no page of its own; Vue still
    // lists the children separately with their whole paths.
    if (path === "") continue;
    const label = safeRoutePattern(path);
    if (label && !labels.includes(label)) labels.push(label);
  }
  return { status: "read", source: "vue-router", labels };
}

/** React Router hands its config over in more than one place depending on how
 *  it was built. Only shapes that are unmistakably a route array are taken. */
function reactRouteArray(router: Record<string, unknown>): unknown[] | null {
  for (const key of ["routes"]) {
    const v = router[key];
    if (Array.isArray(v) && v.length > 0 && isObj(v[0])) {
      const first = v[0] as Record<string, unknown>;
      if (
        "path" in first ||
        "index" in first ||
        "children" in first ||
        "element" in first
      ) {
        return v;
      }
    }
  }
  return null;
}

function readReactRouter(routes: unknown[]): RouterRead {
  const labels: string[] = [];
  if (!walkReactRoutes(routes, "", labels, 0)) {
    return { status: "unreadable", source: "react-router", labels: [] };
  }
  return { status: "read", source: "react-router", labels };
}

function walkReactRoutes(
  routes: unknown[],
  prefix: string,
  out: string[],
  depth: number,
): boolean {
  if (depth > 10) return false;
  for (const route of routes) {
    if (!isObj(route)) return false;
    const path = route.path;
    const isIndex = route.index === true;
    if (path !== undefined && typeof path !== "string") return false;
    if (path === undefined && !isIndex && !Array.isArray(route.children)) {
      // Neither a path, nor an index route, nor a layout with children: a
      // shape we do not recognise.
      return false;
    }
    // An absolute child path replaces the prefix; React Router allows both.
    const joined =
      typeof path === "string" && path.startsWith("/")
        ? path
        : `${prefix}/${typeof path === "string" ? path : ""}`;
    const children = route.children;
    if (Array.isArray(children) && children.length > 0) {
      // A route with a path of its own is matchable whether or not any child
      // matches — React Router renders it with an empty outlet — so it is
      // part of the app's route list and omitting it would report a partial
      // list as a whole one. An index child resolves to the same URL, which
      // the dedupe below folds into one entry. A PATHLESS layout is skipped:
      // it contributes no URL of its own, and emitting its prefix would
      // invent a route the router does not have.
      if (typeof path === "string" && path !== "") {
        const parentLabel = safeRoutePattern(joined);
        if (parentLabel && !out.includes(parentLabel)) out.push(parentLabel);
      }
      if (!walkReactRoutes(children, joined, out, depth + 1)) return false;
      continue;
    }
    const label = safeRoutePattern(joined);
    if (label && !out.includes(label)) out.push(label);
  }
  return true;
}

// ── The merged answer ────────────────────────────────────────────────────────

/**
 * The whole route list: what the router said, merged with what the developer
 * declared, each entry saying where it came from. Neither list overwrites the
 * other — an entry in both is marked `both`.
 */
export function routeListReport(declared: readonly string[]): RouteListReport {
  if (!routeListEnabled()) return { status: "off", entries: [], total: 0 };
  const read = readRouterRoutes(routerHandle);
  const origin = new Map<string, RouteListOrigin>();
  for (const label of read.labels) {
    if (!origin.has(label)) origin.set(label, "framework");
  }
  for (const raw of declared) {
    const label = safeRoutePattern(raw);
    if (!label) continue;
    origin.set(label, origin.has(label) ? "both" : "declared");
  }
  const all = [...origin.keys()].sort();
  const entries = all
    .slice(0, MAX_ROUTE_LIST_ENTRIES)
    .map((label) => ({ label, from: origin.get(label) as RouteListOrigin }));
  const report: RouteListReport = {
    status: read.status,
    entries,
    total: all.length,
  };
  if (read.source) report.source = read.source;
  return report;
}

/**
 * The block a snapshot carries.
 *
 * A kit that HAS this feature always says something, even when the answer is
 * "no router was handed over": that is `unsupported`, an answer the page can
 * word. Carrying nothing at all is reserved for a kit too old to know the
 * question — which is a different fact, and the only way the server can tell
 * the two apart. The single exception is a throw, where the honest answer is
 * that this kit could not produce a block at all.
 */
export function routeListForSnapshot(
  declared: readonly string[],
): RouteListReport | undefined {
  try {
    return routeListReport(declared);
  } catch {
    return undefined;
  }
}
