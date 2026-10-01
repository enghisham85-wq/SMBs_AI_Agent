/** Web-platform meter readers — the 2026-08 additive browser batch.
 *
 * Every reader here is FEATURE-DETECTED and SNAPSHOT-TIME (no new permanent
 * timer, loop, or observer unless it piggybacks an existing sampler). Each
 * returns numbers only — counts, durations, coarse buckets, ratings — never a
 * URL, hostname, path, header value, or any free text derived from host data.
 * A missing browser API means the axis is ABSENT (null), never an exception:
 * every function is guarded and never throws into the host page.
 *
 * The pure scoring for these axes lives inline in `snapshot.ts` using the
 * shared `linearScore`/`linearScoreHigh` + `ratingFor` (85/60 bands) — this
 * module ONLY observes and shapes the raw numbers.
 *
 * Two axes (DOM footprint, idle opportunity) are behind an explicit env opt-in
 * because they are host-invasive (counting the whole DOM is O(DOM); idle
 * sampling wants a callback). When the opt-in is OFF they are ABSENT (not
 * zero). Two axes (unhandled failures, report pressure) chain onto EXISTING
 * global surfaces via additive `addEventListener` / `ReportingObserver` — they
 * never replace a host handler and never swallow what the host sees.
 */

import { readFlagValue } from "./runtimeFlags";

const TRUE_VALUES = new Set(["1", "true", "yes", "on"]);

/** Read an explicit opt-in flag. Truthy = enabled.
 *
 *  Goes through the shared reader so the browser spellings
 *  (`globalThis.BOOSTHIS_DOM_FOOTPRINT`, `globalThis.__BOOSTHIS_DOM_FOOTPRINT__`)
 *  work in a real page: an environment variable is unreachable there, and
 *  these two meters are opt-in, so an env-only opt-in would mean they could
 *  never be turned on by anyone shipping a browser build. */
function optInEnabled(name: string): boolean {
  try {
    const v = readFlagValue(name);
    if (typeof v === "boolean") return v;
    if (typeof v === "string" && v.length > 0) {
      return TRUE_VALUES.has(v.toLowerCase());
    }
  } catch {
    // an unreadable flag is an un-set flag — never disturb the host page
  }
  return false;
}

function navEntry(): PerformanceNavigationTiming | null {
  try {
    if (
      typeof performance === "undefined" ||
      typeof performance.getEntriesByType !== "function"
    ) {
      return null;
    }
    const nav = performance.getEntriesByType(
      "navigation",
    )[0] as PerformanceNavigationTiming | undefined;
    return nav ?? null;
  } catch {
    return null;
  }
}

function resourceEntries(): PerformanceResourceTiming[] {
  try {
    if (
      typeof performance === "undefined" ||
      typeof performance.getEntriesByType !== "function"
    ) {
      return [];
    }
    return performance.getEntriesByType(
      "resource",
    ) as PerformanceResourceTiming[];
  } catch {
    return [];
  }
}

function fin(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/* ─── Navigation readiness (domInteractive / domComplete / loadEventEnd) ─── */

export interface NavReadinessStats {
  domInteractiveMs: number;
  domCompleteMs: number;
  loadEventMs: number;
}

let navReadinessOverride: NavReadinessStats | null = null;

/** domInteractive / domComplete / loadEventEnd for the main navigation, each
 *  measured from the navigation's own start (activationStart-adjusted so a
 *  prerendered nav is honest). Returns null until the load event has finished
 *  (loadEventEnd > 0), so a still-loading page never publishes a partial 0. */
export function getNavReadinessStats(): NavReadinessStats | null {
  if (navReadinessOverride) return navReadinessOverride;
  const nav = navEntry();
  if (!nav) return null;
  const start = fin((nav as { activationStart?: number }).activationStart) ?? 0;
  const domInteractive = fin(nav.domInteractive);
  const domComplete = fin(nav.domComplete);
  const loadEventEnd = fin(nav.loadEventEnd);
  // Only report once the page has fully loaded — before that the values are 0.
  if (domInteractive == null || domInteractive <= 0) return null;
  if (loadEventEnd == null || loadEventEnd <= 0) return null;
  const rel = (v: number | null): number =>
    v == null ? 0 : Math.max(0, Math.round(v - start));
  return {
    domInteractiveMs: rel(domInteractive),
    domCompleteMs: rel(domComplete),
    loadEventMs: rel(loadEventEnd),
  };
}

export function _setNavReadinessForTests(s: NavReadinessStats | null): void {
  navReadinessOverride = s;
}

/* ─── Redirect overhead ──────────────────────────────────────────────────── */

export interface RedirectStats {
  redirectMs: number;
  redirectCount: number;
}

let redirectOverride: RedirectStats | null = null;

/** Redirect time (ms) + count on the main navigation. Null when navigation
 *  timing is unavailable. A redirect-free navigation is an HONEST 0 (the field
 *  is always present on a supported browser), so this axis warms on any load. */
export function getRedirectStats(): RedirectStats | null {
  if (redirectOverride) return redirectOverride;
  const nav = navEntry();
  if (!nav) return null;
  const rStart = fin(nav.redirectStart);
  const rEnd = fin(nav.redirectEnd);
  const count = fin(nav.redirectCount);
  if (rStart == null || rEnd == null) return null;
  const ms = Math.max(0, Math.round(rEnd - rStart));
  return { redirectMs: ms, redirectCount: Math.max(0, Math.round(count ?? 0)) };
}

export function _setRedirectStatsForTests(s: RedirectStats | null): void {
  redirectOverride = s;
}

/* ─── Server processing time (Server-Timing) ─────────────────────────────── */

export interface ServerTimingStats {
  serverMs: number;
  serverEntryCount: number;
}

let serverTimingOverride: ServerTimingStats | null = null;

/** Worst Server-Timing `duration` the server reported on the main navigation
 *  (metric NAMES are never read — only the numeric durations). Null when no
 *  Server-Timing entries exist (the server sent none, or the browser lacks the
 *  field) so it is never faked. */
export function getServerTimingStats(): ServerTimingStats | null {
  if (serverTimingOverride) return serverTimingOverride;
  const nav = navEntry();
  if (!nav) return null;
  const st = (nav as unknown as {
    serverTiming?: ReadonlyArray<{ duration?: number }>;
  }).serverTiming;
  if (!Array.isArray(st) || st.length === 0) return null;
  let worst = 0;
  let counted = 0;
  for (const e of st) {
    const d = fin(e?.duration);
    if (d == null || d <= 0) continue;
    counted++;
    if (d > worst) worst = d;
  }
  if (counted === 0) return null;
  return { serverMs: Math.round(worst), serverEntryCount: counted };
}

export function _setServerTimingForTests(s: ServerTimingStats | null): void {
  serverTimingOverride = s;
}

/* ─── Connection / handshake cost (DNS + TCP + TLS) ──────────────────────── */

export interface HandshakeStats {
  handshakeMs: number;
  dnsMs: number;
  tcpMs: number;
  tlsMs: number;
}

let handshakeOverride: HandshakeStats | null = null;

/** DNS + TCP + TLS negotiation time (ms) on the main navigation. A warm reused
 *  connection is an HONEST 0. Null only when navigation timing is unavailable
 *  or the fields are masked (cross-origin without Timing-Allow-Origin). */
export function getHandshakeStats(): HandshakeStats | null {
  if (handshakeOverride) return handshakeOverride;
  const nav = navEntry();
  if (!nav) return null;
  const dnsStart = fin(nav.domainLookupStart);
  const dnsEnd = fin(nav.domainLookupEnd);
  const connStart = fin(nav.connectStart);
  const connEnd = fin(nav.connectEnd);
  const secureStart = fin(nav.secureConnectionStart);
  if (dnsStart == null || dnsEnd == null || connStart == null || connEnd == null) {
    return null;
  }
  const dnsMs = Math.max(0, Math.round(dnsEnd - dnsStart));
  // secureConnectionStart is 0 when no TLS was negotiated (reused/http).
  const tls =
    secureStart != null && secureStart > 0
      ? Math.max(0, Math.round(connEnd - secureStart))
      : 0;
  const tcpEnd = secureStart != null && secureStart > 0 ? secureStart : connEnd;
  const tcpMs = Math.max(0, Math.round(tcpEnd - connStart));
  const handshakeMs = dnsMs + tcpMs + tls;
  return { handshakeMs, dnsMs, tcpMs, tlsMs: tls };
}

export function _setHandshakeStatsForTests(s: HandshakeStats | null): void {
  handshakeOverride = s;
}

/* ─── Protocol mix (HTTP/2 · HTTP/3) ─────────────────────────────────────── */

export interface ProtocolMixStats {
  modernPct: number;
  h2Pct: number;
  h3Pct: number;
  protoResourceCount: number;
}

let protocolMixOverride: ProtocolMixStats | null = null;
const PROTOCOL_MIX_MIN = 3;

/** Share of timed resources on a modern protocol (HTTP/2 or HTTP/3), derived
 *  from `nextHopProtocol` (an ALPN token like "h2"/"h3"/"http/1.1" — never a
 *  URL). Null until ≥PROTOCOL_MIX_MIN entries expose the field. */
export function getProtocolMixStats(): ProtocolMixStats | null {
  if (protocolMixOverride) return protocolMixOverride;
  const entries = resourceEntries();
  let total = 0;
  let h2 = 0;
  let h3 = 0;
  for (const e of entries) {
    const proto = (e as { nextHopProtocol?: string }).nextHopProtocol;
    if (typeof proto !== "string" || proto.length === 0) continue;
    total++;
    const p = proto.toLowerCase();
    if (p === "h3" || p === "http/3") h3++;
    else if (p === "h2" || p === "http/2") h2++;
  }
  if (total < PROTOCOL_MIX_MIN) return null;
  const pct = (n: number): number => Math.round((n / total) * 100);
  return {
    modernPct: pct(h2 + h3),
    h2Pct: pct(h2),
    h3Pct: pct(h3),
    protoResourceCount: total,
  };
}

export function _setProtocolMixForTests(s: ProtocolMixStats | null): void {
  protocolMixOverride = s;
}

/* ─── Render-blocking pressure ───────────────────────────────────────────── */

export interface RenderBlockingStats {
  renderBlockingCount: number;
  observedCount: number;
}

let renderBlockingOverride: RenderBlockingStats | null = null;

/** Count of observed resources marked render-blocking via
 *  `renderBlockingStatus === "blocking"`. Null when the field is unsupported
 *  everywhere (never faked to 0); a supported browser with no blocking
 *  resources reports an honest 0. */
export function getRenderBlockingStats(): RenderBlockingStats | null {
  if (renderBlockingOverride) return renderBlockingOverride;
  const entries = resourceEntries();
  let observed = 0;
  let blocking = 0;
  let sawField = false;
  for (const e of entries) {
    const status = (e as { renderBlockingStatus?: string })
      .renderBlockingStatus;
    if (typeof status !== "string" || status.length === 0) continue;
    sawField = true;
    observed++;
    if (status === "blocking") blocking++;
  }
  if (!sawField) return null;
  return { renderBlockingCount: blocking, observedCount: observed };
}

export function _setRenderBlockingForTests(
  s: RenderBlockingStats | null,
): void {
  renderBlockingOverride = s;
}

/* ─── Transfer waste ─────────────────────────────────────────────────────── */

export interface TransferWasteStats {
  overheadPct: number;
  wasteKb: number;
  sizedCount: number;
}

let transferWasteOverride: TransferWasteStats | null = null;
const TRANSFER_WASTE_MIN = 5;

/** Bytes transferred beyond the decoded size across sized (CORS-timed)
 *  resources — over-transfer / poor compression, expressed as a % of decoded
 *  bytes plus the absolute KB. Null until ≥TRANSFER_WASTE_MIN sized resources.
 *  This is a DIFFERENT reading from resourceEfficiency (cached/compressed
 *  SHARE): here we measure how many EXCESS bytes crossed the wire. */
export function getTransferWasteStats(): TransferWasteStats | null {
  if (transferWasteOverride) return transferWasteOverride;
  const entries = resourceEntries();
  let decodedTotal = 0;
  let wasteBytes = 0;
  let sized = 0;
  for (const e of entries) {
    const transfer = fin(e.transferSize);
    const decoded = fin(e.decodedBodySize);
    if (decoded == null || decoded <= 0) continue;
    if (transfer == null || transfer <= 0) continue; // 0 = cache hit, skip
    sized++;
    decodedTotal += decoded;
    if (transfer > decoded) wasteBytes += transfer - decoded;
  }
  if (sized < TRANSFER_WASTE_MIN || decodedTotal <= 0) return null;
  return {
    overheadPct: Math.round((wasteBytes / decodedTotal) * 100),
    wasteKb: Math.round(wasteBytes / 1024),
    sizedCount: sized,
  };
}

export function _setTransferWasteForTests(s: TransferWasteStats | null): void {
  transferWasteOverride = s;
}

/* ─── Cache revalidation rate ────────────────────────────────────────────── */

export interface CacheRevalidationStats {
  revalidatedPct: number;
  revalidatedCount: number;
  cachedCount: number;
}

let cacheRevalidationOverride: CacheRevalidationStats | null = null;
const CACHE_REVALIDATION_MIN = 5;

/** Of cache-eligible resources (those that produced a body but were served
 *  from cache/revalidation), the share that still required a network
 *  revalidation (transferSize > 0 but small relative to body) vs a fresh cache
 *  hit (transferSize === 0). Null until ≥CACHE_REVALIDATION_MIN eligible. */
export function getCacheRevalidationStats(): CacheRevalidationStats | null {
  if (cacheRevalidationOverride) return cacheRevalidationOverride;
  const entries = resourceEntries();
  let cached = 0;
  let revalidated = 0;
  for (const e of entries) {
    const transfer = fin(e.transferSize);
    const encoded = fin(e.encodedBodySize);
    if (encoded == null || encoded <= 0) continue; // no body → not cacheable data
    if (transfer == null) continue;
    if (transfer === 0) {
      cached++; // fresh cache hit — nothing crossed the wire
    } else if (transfer > 0 && transfer < encoded) {
      // Small transfer relative to a larger body ⇒ a 304 revalidation round-trip
      // (headers crossed the wire but the body came from cache).
      revalidated++;
      cached++;
    }
  }
  if (cached < CACHE_REVALIDATION_MIN) return null;
  return {
    revalidatedPct: Math.round((revalidated / cached) * 100),
    revalidatedCount: revalidated,
    cachedCount: cached,
  };
}

export function _setCacheRevalidationForTests(
  s: CacheRevalidationStats | null,
): void {
  cacheRevalidationOverride = s;
}

/* ─── Cache reuse (what the page paid to fetch again) ────────────────────── */

export interface CacheReuseStats {
  /** Share of eligible resources the browser served from its own cache. */
  hitPct: number;
  /** Eligible resources served from cache — nothing (or almost nothing) travelled. */
  cachedCount: number;
  /** Eligible resources that actually crossed the network. */
  fetchedCount: number;
  /** Kilobytes those network-fetched resources really moved.
   *
   *  NOT a claim that any of them had been fetched before: the browser tells
   *  us what travelled, never whether the same thing travelled earlier. This
   *  is what the un-cached part of the page cost, which is the number a higher
   *  reuse share would reduce. */
  networkKb: number;
  /** Milliseconds those network-fetched resources spent on the wire, on the
   *  same terms as the kilobytes above. */
  networkMs: number;
  /** How many eligible resources the browser answered EXACTLY (deliveryType). */
  exactCount: number;
}

let cacheReuseOverride: CacheReuseStats | null = null;
const CACHE_REUSE_MIN = 5;

/** `deliveryType` where the browser states the delivery method outright
 *  (`"cache"` for a cache hit, `""` for an ordinary network fetch,
 *  `"navigational-prefetch"` for a prefetched navigation). Older browsers do
 *  not implement it at all, which is why the byte fallback below still exists.
 *  Returns null when the browser said nothing. */
function deliveryTypeOf(e: PerformanceResourceTiming): string | null {
  try {
    const v = (e as unknown as { deliveryType?: unknown }).deliveryType;
    return typeof v === "string" ? v : null;
  } catch {
    return null;
  }
}

/** Of everything this page loaded, how much came out of the browser's own
 *  cache and how much crossed the network — plus what the network half cost
 *  in bytes and time.
 *
 *  WHAT THIS IS NOT
 *  ----------------
 *  It is not a count of things fetched twice. The browser reports what
 *  travelled, not whether the same resource had travelled before, so a first
 *  visit loading five ordinary files is five network fetches and no waste at
 *  all. Calling those bytes "paid twice" would be a made-up number. What is
 *  true, and what this reports, is the SHARE the browser served from its own
 *  cache and what the rest cost — a share that a project can raise, and a cost
 *  that falls when it does.
 *
 *  Two signals, exact first:
 *   • `deliveryType === "cache"` — newer browsers state the delivery method
 *     outright, so a cache hit is a fact, not an inference.
 *   • `transferSize === 0` with a real body — the long-standing fallback for
 *     browsers that do not implement `deliveryType`: nothing travelled, so it
 *     came from cache.
 *
 *  A resource whose sizes the browser refuses to report (a cross-origin asset
 *  with no timing permission reports zeros for everything) is NOT counted
 *  either way — we genuinely cannot tell, and a guess would read as a cache
 *  miss and invent waste that may not exist. Null (absent, never a zero) until
 *  ≥CACHE_REUSE_MIN resources can be judged. Addresses are never read: only
 *  sizes, durations and the delivery signal. */
export function getCacheReuseStats(): CacheReuseStats | null {
  if (cacheReuseOverride) return cacheReuseOverride;
  const entries = resourceEntries();
  let cached = 0;
  let fetched = 0;
  let exact = 0;
  let fetchedBytes = 0;
  let fetchedMs = 0;
  for (const e of entries) {
    const delivery = deliveryTypeOf(e);
    const fromCacheExactly = delivery === "cache";
    const transfer = fin(e.transferSize);
    const encoded = fin(e.encodedBodySize);
    // A body the browser is willing to size is what makes an entry judgeable.
    // An exact "cache" verdict is judgeable on its own.
    if (!fromCacheExactly) {
      if (encoded == null || encoded <= 0) continue;
      if (transfer == null) continue;
    }
    if (delivery !== null) exact++;
    if (fromCacheExactly || transfer === 0) {
      cached++;
      continue;
    }
    fetched++;
    fetchedBytes += transfer ?? 0;
    const dur = fin(e.duration);
    if (dur != null && dur > 0) fetchedMs += dur;
  }
  const eligible = cached + fetched;
  if (eligible < CACHE_REUSE_MIN) return null;
  return {
    hitPct: Math.round((cached / eligible) * 100),
    cachedCount: cached,
    fetchedCount: fetched,
    networkKb: Math.round(fetchedBytes / 1024),
    networkMs: Math.round(fetchedMs),
    exactCount: exact,
  };
}

export function _setCacheReuseForTests(s: CacheReuseStats | null): void {
  cacheReuseOverride = s;
}

/* ─── Initiator balance ──────────────────────────────────────────────────── */

export interface InitiatorBalanceStats {
  scriptPct: number;
  cssPct: number;
  imgPct: number;
  fetchPct: number;
  dominantCount: number;
  resourceCount: number;
  /**
   * REQUESTS THE PAGE MADE AFTER IT HAD FINISHED LOADING, and which this
   * reading therefore does not judge. A section that refreshes itself every
   * two seconds, a search box, an infinite list: all of them pile up `fetch`
   * entries that have nothing to do with how the page was BUILT, and counting
   * them was how a deliberate live-updating section came to be rated as bad
   * page construction. Published so the excluded work is visible rather than
   * silently dropped.
   */
  postLoadCount: number;
}

let initiatorBalanceOverride: InitiatorBalanceStats | null = null;
const INITIATOR_BALANCE_MIN = 5;

/**
 * Which broad initiator class dominates resource COUNT (script / css+link /
 * img / fetch+xhr / other), by count share. URLs are never read — only
 * `initiatorType`. Null until ≥INITIATOR_BALANCE_MIN resources the page
 * loaded WHILE IT WAS BEING BUILT. Higher dominance share = more concentrated
 * load.
 *
 * WHY THE LOAD EVENT SPLITS THE SET. This reading exists to catch a page that
 * assembles itself out of `fetch` calls instead of using the platform's own
 * loading — script tags, stylesheets, images. It does not exist to catch a
 * page that keeps talking to its backend after it has loaded, which is what
 * every live-updating section, search box and infinite list does on purpose.
 * Counting both under one percentage rated our own dashboard — a section we
 * deliberately refresh every two seconds — as badly constructed, at 98%
 * fetch over 129 of 132 resources.
 *
 * A resource whose `startTime` is at or after the navigation's `loadEventEnd`
 * began after the page finished loading, so it cannot have been part of
 * building it. That is a timing the browser already publishes on entries the
 * kit is reading anyway: no URL is inspected, nothing is guessed from a path,
 * and a page with no navigation timing (a soft navigation, an engine that
 * reports none) simply judges everything, exactly as before.
 *
 * The case the meter exists to catch still fails: a page that really does
 * pull 129 of its 132 resources through `fetch` DURING construction has those
 * entries before `loadEventEnd`, so they are all judged and the dominance
 * share is unchanged.
 */
export function getInitiatorBalanceStats(): InitiatorBalanceStats | null {
  if (initiatorBalanceOverride) return initiatorBalanceOverride;
  const entries = resourceEntries();
  // When the load event has not happened (or is not reported), there is no
  // boundary to draw and every entry is judged — the previous behaviour.
  let loadEnd = 0;
  try {
    const nav = navEntry();
    const end = nav ? fin(nav.loadEventEnd) : null;
    if (end != null && end > 0) loadEnd = end;
  } catch {
    loadEnd = 0;
  }
  let script = 0;
  let css = 0;
  let img = 0;
  let fetch = 0;
  let other = 0;
  let total = 0;
  let postLoad = 0;
  for (const e of entries) {
    const it = e.initiatorType;
    if (typeof it !== "string") continue;
    if (loadEnd > 0) {
      const started = fin(e.startTime);
      if (started != null && started >= loadEnd) {
        postLoad++;
        continue;
      }
    }
    total++;
    if (it === "script") script++;
    else if (it === "css" || it === "link") css++;
    else if (it === "img" || it === "image" || it === "input") img++;
    else if (it === "fetch" || it === "xmlhttprequest") fetch++;
    else other++;
  }
  if (total < INITIATOR_BALANCE_MIN) return null;
  const pct = (n: number): number => Math.round((n / total) * 100);
  const dominant = Math.max(script, css, img, fetch, other);
  return {
    scriptPct: pct(script),
    cssPct: pct(css),
    imgPct: pct(img),
    fetchPct: pct(fetch),
    dominantCount: dominant,
    resourceCount: total,
    postLoadCount: postLoad,
  };
}

export function _setInitiatorBalanceForTests(
  s: InitiatorBalanceStats | null,
): void {
  initiatorBalanceOverride = s;
}

/* ─── Storage headroom ───────────────────────────────────────────────────── */

export interface StorageHeadroomStats {
  usedPct: number;
  usedMb: number;
  quotaMb: number;
}

let storageHeadroomOverride: StorageHeadroomStats | null = null;

/** Origin storage usage vs quota via `navigator.storage.estimate()` (async).
 *  A cached result is filled by {@link sampleStorageHeadroomNow} off the
 *  snapshot cadence — no new permanent timer. Absent (null) until the first
 *  reading lands or when the API is unsupported. */
let storageHeadroomReading: StorageHeadroomStats | null = null;

export function sampleStorageHeadroomNow(): void {
  try {
    const nav = typeof navigator !== "undefined" ? navigator : undefined;
    const storage = (nav as { storage?: { estimate?: () => Promise<unknown> } })
      ?.storage;
    if (!storage || typeof storage.estimate !== "function") return;
    void storage
      .estimate()
      .then((est) => {
        try {
          const usage = fin((est as { usage?: number }).usage);
          const quota = fin((est as { quota?: number }).quota);
          if (usage == null || quota == null || quota <= 0) return;
          storageHeadroomReading = {
            usedPct: Math.round((usage / quota) * 100),
            usedMb: Math.round(usage / (1024 * 1024)),
            quotaMb: Math.round(quota / (1024 * 1024)),
          };
        } catch {
          // ignore
        }
      })
      .catch(() => {
        // estimate rejected — stay absent
      });
  } catch {
    // ignore
  }
}

export function getStorageHeadroomStats(): StorageHeadroomStats | null {
  if (storageHeadroomOverride) return storageHeadroomOverride;
  return storageHeadroomReading;
}

export function _setStorageHeadroomForTests(
  s: StorageHeadroomStats | null,
): void {
  storageHeadroomOverride = s;
}

/* ─── Service-worker control ─────────────────────────────────────────────── */

export interface SwControlStats {
  controlled: number;
  /**
   * WHETHER THE APP EVER ASKED FOR ONE. 1 = this origin has at least one
   * service-worker registration, 0 = it has none, null = we have not been
   * able to find out (the async read has not landed, or the browser refused
   * it). Without this, "not controlled" was the same answer for an app that
   * never wanted a service worker and an app whose worker is registered but
   * is not driving this page — and only the second is a finding. Rating
   * every install that simply does not use service workers as orange is how
   * this axis came to say the same thing on every install it ever saw.
   */
  registered: number | null;
}

let swControlOverride: SwControlStats | null = null;
/** Filled by {@link sampleSwControlNow} off the snapshot cadence — the
 *  registration read is async, so it lags the first snapshot by design and
 *  reports null (not 0) until it lands. */
let swRegisteredReading: number | null = null;

export function sampleSwControlNow(): void {
  try {
    const nav = typeof navigator !== "undefined" ? navigator : undefined;
    const sw = (
      nav as {
        serviceWorker?: { getRegistrations?: () => Promise<unknown> };
      }
    )?.serviceWorker;
    if (!sw || typeof sw.getRegistrations !== "function") return;
    void sw
      .getRegistrations()
      .then((regs) => {
        try {
          swRegisteredReading = Array.isArray(regs) && regs.length > 0 ? 1 : 0;
        } catch {
          // ignore
        }
      })
      .catch(() => {
        // Refused (insecure context, storage blocked) — stay unknown rather
        // than reporting "none registered", which is a different claim.
      });
  } catch {
    // ignore
  }
}

/** Whether the current page is controlled by a service worker (1) or not (0),
 *  plus whether this origin has registered one at all. Null only when
 *  `navigator.serviceWorker` is unsupported (never faked). This is a runtime
 *  CAPABILITY-with-a-reading (controlled vs not), not a constant. */
export function getSwControlStats(): SwControlStats | null {
  if (swControlOverride) return swControlOverride;
  try {
    const nav = typeof navigator !== "undefined" ? navigator : undefined;
    const sw = (nav as { serviceWorker?: { controller?: unknown } })
      ?.serviceWorker;
    if (!sw || !("controller" in sw)) return null;
    return {
      controlled: sw.controller ? 1 : 0,
      // A controlled page proves a registration exists without waiting for
      // the async read.
      registered: sw.controller ? 1 : swRegisteredReading,
    };
  } catch {
    return null;
  }
}

export function _setSwControlForTests(s: SwControlStats | null): void {
  swControlOverride = s;
}

/* ─── Connection quality ─────────────────────────────────────────────────── */

export interface ConnectionQualityStats {
  rttMs: number;
  downlinkMbps: number;
  saveData: number;
}

let connectionQualityOverride: ConnectionQualityStats | null = null;

/** Browser-reported effective connection: round-trip time (ms), downlink
 *  (Mbps), and data-saver flag, from `navigator.connection`. Coarse buckets the
 *  browser already rounds. Null when the Network Information API is
 *  unsupported. */
export function getConnectionQualityStats(): ConnectionQualityStats | null {
  if (connectionQualityOverride) return connectionQualityOverride;
  try {
    const nav = typeof navigator !== "undefined" ? navigator : undefined;
    const conn = (
      nav as {
        connection?: { rtt?: number; downlink?: number; saveData?: boolean };
      }
    )?.connection;
    if (!conn) return null;
    const rtt = fin(conn.rtt);
    const downlink = fin(conn.downlink);
    if (rtt == null && downlink == null) return null;
    return {
      rttMs: rtt == null ? 0 : Math.max(0, Math.round(rtt)),
      downlinkMbps:
        downlink == null ? 0 : Math.round(Math.max(0, downlink) * 10) / 10,
      saveData: conn.saveData ? 1 : 0,
    };
  } catch {
    return null;
  }
}

export function _setConnectionQualityForTests(
  s: ConnectionQualityStats | null,
): void {
  connectionQualityOverride = s;
}

/* ─── Device capacity ────────────────────────────────────────────────────── */

export interface DeviceCapacityStats {
  cpuCores: number;
  memoryGb: number;
}

let deviceCapacityOverride: DeviceCapacityStats | null = null;

/** Coarse device class: logical CPU cores (`hardwareConcurrency`) and the
 *  browser's coarse memory bucket (`deviceMemory`, in GiB — already rounded to
 *  0.25/0.5/1/2/4/8 by the UA for privacy). Null when neither is exposed.
 *  These are COARSE CAPACITY BUCKETS, not device identifiers. */
export function getDeviceCapacityStats(): DeviceCapacityStats | null {
  if (deviceCapacityOverride) return deviceCapacityOverride;
  try {
    const nav = typeof navigator !== "undefined" ? navigator : undefined;
    const cores = fin((nav as { hardwareConcurrency?: number })?.hardwareConcurrency);
    const mem = fin((nav as { deviceMemory?: number })?.deviceMemory);
    if (cores == null && mem == null) return null;
    return {
      cpuCores: cores == null ? 0 : Math.max(0, Math.round(cores)),
      memoryGb: mem == null ? 0 : Math.max(0, Math.round(mem * 10) / 10),
    };
  } catch {
    return null;
  }
}

export function _setDeviceCapacityForTests(
  s: DeviceCapacityStats | null,
): void {
  deviceCapacityOverride = s;
}

/* ─── Prerender activation ───────────────────────────────────────────────── */

export interface PrerenderStats {
  prerendered: number;
  activationMs: number;
}

let prerenderOverride: PrerenderStats | null = null;

/** Whether this navigation was prerendered before activation (1/0) and, if so,
 *  how long (ms) it spent prerendering before activation
 *  (`PerformanceNavigationTiming.activationStart`). Null when neither
 *  `document.prerendering` nor `activationStart` is observable. */
export function getPrerenderStats(): PrerenderStats | null {
  if (prerenderOverride) return prerenderOverride;
  const nav = navEntry();
  const activation = nav ? fin((nav as { activationStart?: number }).activationStart) : null;
  let prerenderingKnown = false;
  let currentlyPrerendering = false;
  try {
    if (typeof document !== "undefined" && "prerendering" in document) {
      prerenderingKnown = true;
      currentlyPrerendering = Boolean(
        (document as { prerendering?: boolean }).prerendering,
      );
    }
  } catch {
    // ignore
  }
  if (activation == null && !prerenderingKnown) return null;
  const activationMs = activation != null ? Math.max(0, Math.round(activation)) : 0;
  const prerendered = currentlyPrerendering || activationMs > 0 ? 1 : 0;
  return { prerendered, activationMs };
}

export function _setPrerenderForTests(s: PrerenderStats | null): void {
  prerenderOverride = s;
}

/* ─── Font readiness ─────────────────────────────────────────────────────── */

export interface FontReadinessStats {
  fontLoaded: number;
  /** Time from NAVIGATION START to font readiness. Context, not the judged
   *  figure: it carries the whole server round trip and HTML download in
   *  front of it, neither of which any font choice can move. */
  fontMs: number;
  /**
   * THE JUDGED FIGURE: how long fonts took once they could first be asked
   * for, i.e. from the navigation's `responseEnd` to font readiness.
   *
   * Scoring `fontMs` meant a page served slowly got a bad FONT verdict with
   * instant fonts, and a page on a fast server got a good one while swapping
   * text seconds late. Anchoring at `responseEnd` — the moment the HTML
   * finished arriving, after which the CSS that declares the fonts can be
   * parsed and the fonts fetched — leaves exactly the interval the page's own
   * font decisions control.
   *
   * Equal to `fontMs` when the navigation entry is unreadable, so an engine
   * that publishes no navigation timing is judged exactly as before rather
   * than going silent.
   */
  fontDelayMs: number;
}

let fontReadinessOverride: FontReadinessStats | null = null;
let fontReadyAtMs: number | null = null;
let fontReadyArmed = false;

/** Arm font-readiness tracking: when `document.fonts.ready` resolves, stamp the
 *  elapsed ms from navigation start. Called once from startWebVitals — reuses
 *  the browser's OWN readiness promise, no polling loop. Guarded + idempotent. */
export function armFontReadiness(): void {
  if (fontReadyArmed) return;
  try {
    const doc = typeof document !== "undefined" ? document : undefined;
    const fonts = (doc as { fonts?: { ready?: Promise<unknown>; status?: string } })
      ?.fonts;
    if (!fonts || typeof fonts.ready?.then !== "function") return;
    fontReadyArmed = true;
    void fonts.ready
      .then(() => {
        try {
          fontReadyAtMs =
            typeof performance !== "undefined" &&
            typeof performance.now === "function"
              ? Math.max(0, Math.round(performance.now()))
              : 0;
        } catch {
          fontReadyAtMs = 0;
        }
      })
      .catch(() => {
        // fonts.ready never rejects in practice; stay absent if it does
      });
  } catch {
    // ignore
  }
}

/**
 * Font readiness: whether document fonts have finished loading (1/0), how long
 * that took from navigation start (`fontMs`, context) and the interval this
 * axis actually judges (`fontDelayMs`, from `responseEnd`). Null when the Font
 * Loading API is unsupported.
 *
 * WHILE FONTS ARE STILL LOADING there is no readiness time yet, and the two
 * ms figures are reported as the elapsed-so-far FLOOR with `fontLoaded: 0`.
 * They used to be reported as 0, which scored a full 100 — a page whose fonts
 * had not arrived read as the best possible font performance. The caller
 * withholds the verdict on that pairing instead.
 */
export function getFontReadinessStats(): FontReadinessStats | null {
  if (fontReadinessOverride) return fontReadinessOverride;
  try {
    const doc = typeof document !== "undefined" ? document : undefined;
    const fonts = (doc as { fonts?: { status?: string } })?.fonts;
    if (!fonts || typeof fonts.status !== "string") return null;
    const loaded = fonts.status === "loaded" ? 1 : 0;
    // Ready: the stamped readiness time. Not ready: how long they have been
    // loading so far — a floor, never a zero.
    let at = fontReadyAtMs;
    if (at == null) {
      at =
        typeof performance !== "undefined" &&
        typeof performance.now === "function"
          ? Math.max(0, Math.round(performance.now()))
          : 0;
    }
    const nav = navEntry();
    const respEnd =
      nav && Number.isFinite(nav.responseEnd) && nav.responseEnd > 0
        ? nav.responseEnd
        : null;
    const delay = respEnd == null ? at : Math.max(0, Math.round(at - respEnd));
    return { fontLoaded: loaded, fontMs: at, fontDelayMs: delay };
  } catch {
    return null;
  }
}


export function _setFontReadinessForTests(s: FontReadinessStats | null): void {
  fontReadinessOverride = s;
}

/* ─── Script / style footprint ───────────────────────────────────────────── */

export interface AssetFootprintStats {
  scriptCount: number;
  styleCount: number;
}

let assetFootprintOverride: AssetFootprintStats | null = null;

/** Count of `<script>` elements and stylesheets in the document
 *  (`document.scripts.length` + `document.styleSheets.length`). These are cheap
 *  live-collection length reads (O(1)), not a full DOM walk. Null in SSR. */
export function getAssetFootprintStats(): AssetFootprintStats | null {
  if (assetFootprintOverride) return assetFootprintOverride;
  try {
    if (typeof document === "undefined") return null;
    const scripts = (document as { scripts?: { length?: number } }).scripts;
    const sheets = (document as { styleSheets?: { length?: number } })
      .styleSheets;
    if (!scripts && !sheets) return null;
    return {
      scriptCount: Math.max(0, Math.round(scripts?.length ?? 0)),
      styleCount: Math.max(0, Math.round(sheets?.length ?? 0)),
    };
  } catch {
    return null;
  }
}

export function _setAssetFootprintForTests(
  s: AssetFootprintStats | null,
): void {
  assetFootprintOverride = s;
}

/* ─── Cross-origin isolation ─────────────────────────────────────────────── */

export interface CrossOriginIsolationStats {
  isolated: number;
}

let crossOriginIsolationOverride: CrossOriginIsolationStats | null = null;

/** Whether the page is cross-origin isolated (1/0) — the gate for
 *  high-precision timers + `measureUserAgentSpecificMemory`. Null only when
 *  `crossOriginIsolated` is not exposed (never faked). */
export function getCrossOriginIsolationStats(): CrossOriginIsolationStats | null {
  if (crossOriginIsolationOverride) return crossOriginIsolationOverride;
  try {
    const g = globalThis as unknown as { crossOriginIsolated?: boolean };
    if (typeof g.crossOriginIsolated !== "boolean") return null;
    return { isolated: g.crossOriginIsolated ? 1 : 0 };
  } catch {
    return null;
  }
}

export function _setCrossOriginIsolationForTests(
  s: CrossOriginIsolationStats | null,
): void {
  crossOriginIsolationOverride = s;
}

/* ─── DOM footprint (opt-in) ─────────────────────────────────────────────── */

export interface DomFootprintStats {
  elementCount: number;
}

let domFootprintOverride: DomFootprintStats | null = null;
/** Explicit opt-in env/global — counting the whole DOM is O(DOM). */
export const DOM_FOOTPRINT_OPT_IN = "BOOSTHIS_DOM_FOOTPRINT";

/** Total element count in the document — ONLY when the explicit opt-in flag is
 *  set (`BOOSTHIS_DOM_FOOTPRINT`), because `getElementsByTagName('*').length`
 *  is O(DOM). ABSENT (null), not zero, when the opt-in is off. */
export function getDomFootprintStats(): DomFootprintStats | null {
  if (domFootprintOverride) return domFootprintOverride;
  if (!optInEnabled(DOM_FOOTPRINT_OPT_IN)) return null;
  try {
    if (
      typeof document === "undefined" ||
      typeof document.getElementsByTagName !== "function"
    ) {
      return null;
    }
    const all = document.getElementsByTagName("*");
    return { elementCount: Math.max(0, Math.round(all.length)) };
  } catch {
    return null;
  }
}

export function _setDomFootprintForTests(s: DomFootprintStats | null): void {
  domFootprintOverride = s;
}

/* ─── Idle opportunity (opt-in) ──────────────────────────────────────────── */

export interface IdleOpportunityStats {
  idleCalls: number;
  missedDeadlines: number;
  missedPct: number;
}

let idleOpportunityOverride: IdleOpportunityStats | null = null;
/** Explicit opt-in env/global — schedules requestIdleCallback probes. */
export const IDLE_OPPORTUNITY_OPT_IN = "BOOSTHIS_IDLE_OPPORTUNITY";
const IDLE_OPPORTUNITY_MIN_CALLS = 5;

let idleCalls = 0;
let idleMissed = 0;
let idleOppArmed = false;

/** Arm idle-opportunity probing — ONLY when the explicit opt-in flag is set.
 *  Each fired requestIdleCallback re-schedules the NEXT one from inside itself
 *  (self-perpetuating chain, not a permanent interval/loop) and records whether
 *  the callback ran because its deadline was hit (`didTimeout`). Guarded +
 *  idempotent. ABSENT when the opt-in is off or rIC is unsupported. */
export function armIdleOpportunity(): void {
  if (idleOppArmed) return;
  if (!optInEnabled(IDLE_OPPORTUNITY_OPT_IN)) return;
  try {
    const g = globalThis as unknown as {
      requestIdleCallback?: (
        cb: (deadline: { didTimeout?: boolean }) => void,
        opts?: { timeout?: number },
      ) => number;
    };
    if (typeof g.requestIdleCallback !== "function") return;
    idleOppArmed = true;
    const schedule = (): void => {
      try {
        g.requestIdleCallback!(
          (deadline) => {
            try {
              idleCalls++;
              if (deadline && deadline.didTimeout) idleMissed++;
            } catch {
              // ignore
            }
            // Chain the next probe only while still armed.
            if (idleOppArmed) schedule();
          },
          { timeout: 1000 },
        );
      } catch {
        // ignore
      }
    };
    schedule();
  } catch {
    // ignore
  }
}

/** Idle opportunity: how often the page's idle callbacks fired only because
 *  their deadline was hit (a busy main thread starving idle work). ABSENT until
 *  the opt-in is on AND ≥IDLE_OPPORTUNITY_MIN_CALLS probes have fired. */
export function getIdleOpportunityStats(): IdleOpportunityStats | null {
  if (idleOpportunityOverride) return idleOpportunityOverride;
  if (!optInEnabled(IDLE_OPPORTUNITY_OPT_IN)) return null;
  if (idleCalls < IDLE_OPPORTUNITY_MIN_CALLS) return null;
  return {
    idleCalls,
    missedDeadlines: idleMissed,
    missedPct: Math.round((idleMissed / idleCalls) * 100),
  };
}

export function _setIdleOpportunityForTests(
  s: IdleOpportunityStats | null,
): void {
  idleOpportunityOverride = s;
}

/* ─── Layout-shift source count (piggybacks the layout-shift observer) ────── */

let shiftWorstSources = 0;
let shiftCount = 0;
let shiftObserved = false;
let shiftSourceOverride: {
  worstSourceCount: number;
  shiftCount: number;
} | null = null;

/** Fed from the EXISTING `layout-shift` observer in vitals.ts: record the
 *  number of contributing SOURCES on each shift (a count only — the actual
 *  nodes/selectors are never read or stored). */
export function noteLayoutShiftSources(sourceCount: number): void {
  try {
    shiftObserved = true;
    if (!Number.isFinite(sourceCount) || sourceCount < 0) return;
    shiftCount++;
    if (sourceCount > shiftWorstSources) shiftWorstSources = Math.round(sourceCount);
  } catch {
    // never break the observer
  }
}

export interface ShiftSourceStats {
  worstSourceCount: number;
  shiftCount: number;
}

/** Worst single layout-shift's contributing-source count. Null until at least
 *  one shift with sources has been observed (feature-detected via the observer
 *  actually populating `sources`). */
export function getShiftSourceStats(): ShiftSourceStats | null {
  if (shiftSourceOverride) return shiftSourceOverride;
  if (!shiftObserved || shiftCount === 0) return null;
  return { worstSourceCount: shiftWorstSources, shiftCount };
}

export function _setShiftSourceStatsForTests(
  s: ShiftSourceStats | null,
): void {
  shiftSourceOverride = s;
}

/* ─── Interaction volume (piggybacks the event observer) ─────────────────── */

let interactionIds = new Set<number>();
let interactionSupported = false;
let interactionVolumeOverride: { interactionCount: number } | null = null;

/** Fed from the EXISTING `event` observer in vitals.ts: dedupe distinct
 *  interactions by `interactionId` (a count only — no timing, no target). */
export function noteInteractionId(interactionId: unknown): void {
  try {
    if (typeof interactionId !== "number" || interactionId <= 0) return;
    interactionSupported = true;
    interactionIds.add(interactionId);
    // Bound the set so a marathon session can't grow it without limit.
    if (interactionIds.size > 2000) {
      interactionIds = new Set(Array.from(interactionIds).slice(-1000));
    }
  } catch {
    // never break the observer
  }
}

export interface InteractionVolumeStats {
  interactionCount: number;
}

/** Distinct interactions observed this session, WITHOUT timing any of them
 *  (that is what interactionToNextPaint is for). Null until ≥1 distinct
 *  interaction with an id has been seen (feature-detected). */
export function getInteractionVolumeStats(): InteractionVolumeStats | null {
  if (interactionVolumeOverride) return interactionVolumeOverride;
  if (!interactionSupported || interactionIds.size === 0) return null;
  return { interactionCount: interactionIds.size };
}

export function _setInteractionVolumeForTests(
  s: InteractionVolumeStats | null,
): void {
  interactionVolumeOverride = s;
}

/* ─── Media playback health (snapshot-time read of media elements) ───────── */

export interface MediaPlaybackStats {
  droppedPct: number;
  droppedFrames: number;
  totalFrames: number;
}

let mediaPlaybackOverride: MediaPlaybackStats | null = null;
const MEDIA_MIN_FRAMES = 60;

/** Dropped-video-frame share across playing media, via
 *  `HTMLVideoElement.getVideoPlaybackQuality()`. Aggregated over the video
 *  elements currently in the document (count-only; no src, no track info). Null
 *  when no media element exists or the API is unsupported (so a page with no
 *  video never shows a permanent warming tile). */
export function getMediaPlaybackStats(): MediaPlaybackStats | null {
  if (mediaPlaybackOverride) return mediaPlaybackOverride;
  try {
    if (
      typeof document === "undefined" ||
      typeof document.getElementsByTagName !== "function"
    ) {
      return null;
    }
    const videos = document.getElementsByTagName("video");
    if (!videos || videos.length === 0) return null;
    let dropped = 0;
    let total = 0;
    let sawApi = false;
    for (let i = 0; i < videos.length; i++) {
      const v = videos[i] as unknown as {
        getVideoPlaybackQuality?: () => {
          droppedVideoFrames?: number;
          totalVideoFrames?: number;
        };
      };
      if (typeof v.getVideoPlaybackQuality !== "function") continue;
      sawApi = true;
      const q = v.getVideoPlaybackQuality();
      const d = fin(q?.droppedVideoFrames);
      const t = fin(q?.totalVideoFrames);
      if (d != null && d >= 0) dropped += d;
      if (t != null && t >= 0) total += t;
    }
    if (!sawApi || total < MEDIA_MIN_FRAMES) return null;
    return {
      droppedPct: Math.round((dropped / total) * 1000) / 10,
      droppedFrames: Math.round(dropped),
      totalFrames: Math.round(total),
    };
  } catch {
    return null;
  }
}

export function _setMediaPlaybackForTests(s: MediaPlaybackStats | null): void {
  mediaPlaybackOverride = s;
}

/* ─── Reset hook (tests + teardown) ──────────────────────────────────────── */

/** @internal reset the piggyback counters + armed flags + cached readings. */
export function _resetWebPlatformForTests(): void {
  navReadinessOverride = null;
  redirectOverride = null;
  serverTimingOverride = null;
  handshakeOverride = null;
  protocolMixOverride = null;
  renderBlockingOverride = null;
  transferWasteOverride = null;
  cacheRevalidationOverride = null;
  initiatorBalanceOverride = null;
  storageHeadroomOverride = null;
  storageHeadroomReading = null;
  swControlOverride = null;
  swRegisteredReading = null;
  connectionQualityOverride = null;
  deviceCapacityOverride = null;
  prerenderOverride = null;
  fontReadinessOverride = null;
  fontReadyAtMs = null;
  fontReadyArmed = false;
  assetFootprintOverride = null;
  crossOriginIsolationOverride = null;
  domFootprintOverride = null;
  idleOpportunityOverride = null;
  idleCalls = 0;
  idleMissed = 0;
  idleOppArmed = false;
  shiftWorstSources = 0;
  shiftCount = 0;
  shiftObserved = false;
  shiftSourceOverride = null;
  interactionIds = new Set<number>();
  interactionSupported = false;
  interactionVolumeOverride = null;
  mediaPlaybackOverride = null;
}
