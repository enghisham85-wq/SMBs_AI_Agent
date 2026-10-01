/** Hosting-platform cache verdict — what the platform itself said.
 *
 * Every major hosting platform and CDN states, in the reply itself, whether it
 * served that reply from its own cache or fetched it fresh. Nobody looks. This
 * module reads that one verdict off the replies the app's OWN calls receive
 * and keeps four counters.
 *
 * WHAT IS READ, EXACTLY
 * ---------------------
 * A fixed, code-defined list of header NAMES (below). Nothing else is read —
 * no address, no path, no other header, no body. The header's value is matched
 * against a fixed table of the platform's own words ("HIT", "MISS", …) and
 * immediately discarded; only a counter moves. An unrecognised word counts as
 * nothing at all, so a platform we do not know cannot be turned into a
 * confident verdict.
 *
 * WHY THE COUNTERS ARE SPLIT THE WAY THEY ARE
 * -------------------------------------------
 * A reply the platform deliberately refuses to cache (`BYPASS`, `DYNAMIC`) is
 * not waste — a personalised page SHOULD be served fresh. So bypasses are
 * counted and shown but kept OUT of the share: the share is "of the replies
 * the platform treated as cacheable, how many did it actually serve from
 * cache". A miss there is a page served fresh that need not have been, which
 * is exactly the money story.
 *
 * ABSTENTION
 * ----------
 * No verdict header, an unknown platform, a cross-origin reply whose headers
 * the browser will not expose — all produce nothing. The reading is absent
 * (never a zero) until enough cacheable replies have actually declared
 * themselves.
 *
 * The Node runtime carries the same reading for the replies a server receives
 * (`lib/boosthis-runtime-node/src/edgeCache.ts`); the verdict vocabulary and
 * the emitted field shape are identical on both sides.
 */

/** What the platform said about one reply, in our own closed vocabulary. */
export type EdgeVerdict = "hit" | "miss" | "stale" | "bypass";

/** Header names we read, in priority order. Code-defined and closed. */
export const EDGE_CACHE_HEADERS = [
  "x-vercel-cache",
  "cf-cache-status",
  "x-nextjs-cache",
  "cache-status",
  "x-cache",
] as const;

/** Longest header value we will even look at. A verdict is one short word; a
 *  longer value is some other header we have no business parsing. */
const MAX_VERDICT_LEN = 120;

/** Vercel's own words. PRERENDER means it came from the prerender cache, so it
 *  is a hit; STALE/REVALIDATED were served from cache while refreshed behind
 *  the scenes. */
const VERCEL: Readonly<Record<string, EdgeVerdict>> = {
  HIT: "hit",
  PRERENDER: "hit",
  STALE: "stale",
  REVALIDATED: "stale",
  MISS: "miss",
  BYPASS: "bypass",
};

/** Cloudflare's own words. DYNAMIC means the route is not cacheable by
 *  configuration — deliberate, so it is a bypass rather than a miss. */
const CLOUDFLARE: Readonly<Record<string, EdgeVerdict>> = {
  HIT: "hit",
  MISS: "miss",
  EXPIRED: "stale",
  STALE: "stale",
  UPDATING: "stale",
  REVALIDATED: "stale",
  BYPASS: "bypass",
  DYNAMIC: "bypass",
  IGNORED: "bypass",
};

/** Next.js's own words (self-hosted and on Vercel). */
const NEXTJS: Readonly<Record<string, EdgeVerdict>> = {
  HIT: "hit",
  MISS: "miss",
  STALE: "stale",
};

/** `X-Cache` from CloudFront / Fastly / Varnish reads like "Hit from
 *  cloudfront" — the verdict is the FIRST word. */
const XCACHE_FIRST_WORD: Readonly<Record<string, EdgeVerdict>> = {
  HIT: "hit",
  MISS: "miss",
  REFRESHHIT: "stale",
  ERROR: "bypass",
};

/** RFC 9211 `Cache-Status` (Netlify, Fastly and anything standards-based):
 *  `"Netlify Edge"; hit` or `"Netlify Edge"; fwd=miss; stored`. Only the
 *  member closest to the user is read — the first one. */
function fromCacheStatus(raw: string): EdgeVerdict | null {
  const first = raw.split(",")[0];
  if (first === undefined) return null;
  const params = first.split(";").slice(1);
  let sawFwd = false;
  let fwdReason = "";
  for (const p of params) {
    const key = p.trim().toLowerCase();
    if (key === "hit") return "hit";
    if (key.startsWith("fwd=")) {
      sawFwd = true;
      fwdReason = key.slice(4).trim();
    }
  }
  if (!sawFwd) return null;
  if (fwdReason === "stale" || fwdReason === "request") return "stale";
  if (fwdReason === "bypass" || fwdReason === "method") return "bypass";
  if (fwdReason === "uri-miss" || fwdReason === "vary-miss" || fwdReason === "miss") {
    return "miss";
  }
  return null;
}

/** Read one reply's verdict. `read` returns a header's value or null; it is
 *  called only with the fixed names above. Returns null whenever the platform
 *  said nothing we recognise — never a guess. */
export function classifyCacheVerdict(
  read: (name: string) => string | null | undefined,
): EdgeVerdict | null {
  for (const name of EDGE_CACHE_HEADERS) {
    let raw: string | null | undefined;
    try {
      raw = read(name);
    } catch {
      continue;
    }
    if (typeof raw !== "string") continue;
    const trimmed = raw.trim();
    if (trimmed.length === 0 || trimmed.length > MAX_VERDICT_LEN) continue;
    if (name === "cache-status") {
      const v = fromCacheStatus(trimmed);
      if (v !== null) return v;
      continue;
    }
    if (name === "x-cache") {
      const word = trimmed.split(/[\s,]+/)[0];
      const v = word ? XCACHE_FIRST_WORD[word.toUpperCase()] : undefined;
      if (v !== undefined) return v;
      continue;
    }
    const upper = trimmed.toUpperCase();
    const table =
      name === "x-vercel-cache"
        ? VERCEL
        : name === "cf-cache-status"
          ? CLOUDFLARE
          : NEXTJS;
    const v = table[upper];
    if (v !== undefined) return v;
  }
  return null;
}

/* ── Our own replies ─────────────────────────────────
 *
 * The kit's own sends (consent, samples, snapshot, check-in) leave through
 * the page's own fetch, so the same wrapper that watches the app's calls also
 * sees OUR replies come back. Our hosting states its cache verdict in exactly
 * the same headers, so without this the reading would quietly become a report
 * on BOOSTHIS's cache — worst of all for the app this reading exists for, a
 * front end with no backend of its own, where our uploads could be most of
 * the replies that ever declare a verdict.
 *
 * The only address ever held here is OURS: the endpoint the kit was handed at
 * startup. Nothing about the app's own destinations is read, matched or kept.
 * The Node runtime does the same with the host it is told to ignore.
 */

/** Origin + path prefix of every endpoint this kit sends to. */
let ownEndpoints: string[] = [];

function normalise(url: string): string | null {
  try {
    const base =
      typeof location !== "undefined" && typeof location.href === "string"
        ? location.href
        : undefined;
    const u = base ? new URL(url, base) : new URL(url);
    const path = u.pathname.endsWith("/")
      ? u.pathname.slice(0, -1)
      : u.pathname;
    return u.origin + path;
  } catch {
    return null;
  }
}

/** Remember one endpoint of ours, so replies from it are never counted as the
 *  app's. Called wherever the kit is told where to send. */
export function noteOwnEndpoint(endpoint: string | null | undefined): void {
  try {
    if (typeof endpoint !== "string" || endpoint.length === 0) return;
    const key = normalise(endpoint);
    if (key === null || ownEndpoints.includes(key)) return;
    // A handful at most (endpoint + any re-configure). Bounded so a page that
    // re-starts the kit in a loop cannot grow this without end.
    if (ownEndpoints.length >= 8) return;
    ownEndpoints.push(key);
  } catch {
    /* observing must never disturb the page */
  }
}

/** Whether this reply came back from one of our own endpoints. Matched on the
 *  path prefix, not just the origin, so an app hosted on the same origin as
 *  the endpoint (our own site, dogfooding) keeps its own reading. */
export function isOwnEndpointReply(url: string | null | undefined): boolean {
  try {
    if (ownEndpoints.length === 0) return false;
    if (typeof url !== "string" || url.length === 0) return false;
    const key = normalise(url);
    if (key === null) return false;
    return ownEndpoints.some(
      (own) => key === own || key.startsWith(own + "/"),
    );
  } catch {
    return false;
  }
}

/** @internal test hook — forget every registered endpoint of ours. */
/* ─── Whose hosting is this? ───────────────────────────────────────────
 *
 * The verdict header says "this reply was served from a cache". It does NOT
 * say whose. A project with no CDN of its own that calls a dependency hosted
 * on Cloudflare would otherwise be shown that dependency's cache performance
 * under the words "your host" — a confident answer about a thing we never
 * measured.
 *
 * So a verdict is only filed when the reply came from the page's OWN origin:
 * the site the visitor is on, which is by definition the hosting this project
 * chose. Everything else — third-party APIs, CDNs, analytics, fonts — is read
 * by nobody and counted nowhere. The trade is deliberate: a project whose API
 * lives on another hostname will see fewer replies judged, which is the
 * honest failure (fewer readings) rather than the dishonest one (someone
 * else's numbers under this project's name).
 */

/** The page's own origin, or null when there is none to read (a worker, a
 *  test scope, a non-browser host). Null means nothing can be attributed. */
export function pageOriginOf(scope: unknown): string | null {
  try {
    const loc = (scope as { location?: { origin?: unknown } } | null)?.location;
    const origin = loc?.origin;
    if (typeof origin === "string" && origin.length > 0 && origin !== "null") {
      return origin;
    }
  } catch {
    // fall through — no origin to read
  }
  return null;
}

/** Is this reply from the site the page itself is served by?
 *
 *  A relative address (`/api/things`) is same-origin by definition. An
 *  absolute one must match the page's origin exactly. When we cannot tell —
 *  no origin to compare against, an address that will not parse — the answer
 *  is NO: an unattributable verdict is dropped rather than guessed. */
export function isOwnHostingReply(
  url: string | null | undefined,
  pageOrigin: string | null,
): boolean {
  try {
    if (pageOrigin === null) return false;
    if (typeof url !== "string" || url.length === 0) return false;
    const trimmed = url.trim();
    if (trimmed.length === 0) return false;
    // Relative to the page: same origin by construction.
    if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(trimmed) && !trimmed.startsWith("//")) {
      return true;
    }
    const parsed = new URL(trimmed, pageOrigin);
    return parsed.origin === pageOrigin;
  } catch {
    return false;
  }
}

export function _clearOwnEndpointsForTests(): void {
  ownEndpoints = [];
}
/* ── Counters ─────────────────────────────────────────────────────────────*/

let hits = 0;
let misses = 0;
let staleServed = 0;
let bypassed = 0;

/** Stop the counters growing without bound in a very long-lived page. Past
 *  this the share is already settled and further replies add nothing. */
const MAX_COUNTED = 100_000;

/** File one reply's verdict. Safe to call for every reply: a reply with no
 *  verdict header costs a handful of missing-header lookups and files nothing. */
export function noteEdgeCacheReply(
  read: (name: string) => string | null | undefined,
): void {
  try {
    if (hits + misses + staleServed + bypassed >= MAX_COUNTED) return;
    const verdict = classifyCacheVerdict(read);
    if (verdict === null) return;
    if (verdict === "hit") hits++;
    else if (verdict === "miss") misses++;
    else if (verdict === "stale") staleServed++;
    else bypassed++;
  } catch {
    /* observing must never disturb the page */
  }
}

export interface EdgeCacheStats {
  /** Replies that declared a verdict at all. */
  checked: number;
  /** Served from the platform's cache. */
  hits: number;
  /** The platform could have cached it and did not. */
  misses: number;
  /** Served from cache while being refreshed behind the scenes. */
  stale: number;
  /** The platform deliberately does not cache this — counted, never scored. */
  bypass: number;
  /** Of the replies the platform treated as cacheable, the share it served
   *  from cache (hits + stale). Bypasses are excluded from both sides. */
  hitPct: number;
}

/** Cacheable replies needed before the share means anything. */
const EDGE_CACHE_MIN = 5;

/** The platform's verdict so far, or null when too few cacheable replies have
 *  declared themselves — an honest "we cannot tell", never a zero. */
export function getEdgeCacheStats(): EdgeCacheStats | null {
  const cacheable = hits + misses + staleServed;
  if (cacheable < EDGE_CACHE_MIN) return null;
  return {
    checked: cacheable + bypassed,
    hits,
    misses,
    stale: staleServed,
    bypass: bypassed,
    hitPct: Math.round(((hits + staleServed) / cacheable) * 100),
  };
}

/** Wipe every counter. Wired into the kit's forget path so nothing
 *  Boosthis-shaped keeps counting after erasure. */
export function clearEdgeCache(): void {
  hits = 0;
  misses = 0;
  staleServed = 0;
  bypassed = 0;
}

/** @internal test hook — file a verdict directly, without a reply. */
export function _noteEdgeVerdictForTests(verdict: EdgeVerdict): void {
  noteEdgeCacheReply((name) =>
    name === "x-vercel-cache"
      ? verdict === "hit"
        ? "HIT"
        : verdict === "miss"
          ? "MISS"
          : verdict === "stale"
            ? "STALE"
            : "BYPASS"
      : null,
  );
}
