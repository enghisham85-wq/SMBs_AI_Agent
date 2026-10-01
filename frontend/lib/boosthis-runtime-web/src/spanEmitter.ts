/** Full-stack trace — span emission (Web browser root layer).
 *
 * `trace.ts` only *propagates* a 32-hex correlation id on the
 * `x-boosthis-trace` header. This module lets the web layer emit ONE
 * privacy-safe root span per outbound `fetch` so the server can stitch a
 * waterfall (Web / RN → Node → Python) keyed by that id.
 *
 * A span is a tiny, closed, code-derived shape:
 *   { traceId, layer:"web", routeLabel, durationMs, startOffsetMs, rating }
 *
 * `routeLabel` is `${METHOD} ${normalizedPath}` (ids redacted, query/hash never
 * read), capped at 100 chars — the SAME normalization the page sampler uses, so
 * no raw path segment (numeric/uuid/long-hex id) ever travels. Nothing here is
 * page-bearing beyond that code-defined label, and it rides the SAME PII guard
 * as every other transmit (see `telemetry.transmitSpans`).
 *
 * PRIVACY-BY-DEFAULT: `enqueueSpan` is a no-op until a submitter is wired. The
 * telemetry client only wires the span submitter when the snapshot mirror is
 * allowed (full mode, or issues-only after the developer connects an AI), so a
 * private app RETAINS nothing span-shaped until sharing is authorized. The
 * emergency kill-switch (`BOOSTHIS_DISABLED`) and `forget()` both stop and clear
 * it, exactly like the snapshot mirror.
 *
 * Everything here is best-effort: span bookkeeping must NEVER change the host
 * request's behavior or crash the page, so `traceFetch` records the span in a
 * `finally` and swallows any error from that path.
 */

import { MAX_PART_NAME, normalizeRouteLabel } from "./vitals";
import { isBoosthisDisabled } from "./runtimeFlags";
import { isRuntimeInert } from "./killSwitch";
import { PARENT_HEADER, beginSpan, sanitizeSpanId } from "./spanScope";
import { judgeNovelty, isNovel, resetNovelty } from "./spanNovelty";
import {
  outcomeForStatus,
  sanitizeWorkKind,
  sanitizeOutcome,
  type SpanWorkKind,
  type SpanOutcome,
} from "./spanWork";
import {
  TRACE_HEADER,
  ELAPSED_HEADER,
  MAX_TRACE_ELAPSED_MS,
  sanitizeTraceId,
} from "./trace";

/** The web emitter only ever produces `"web"` spans; the server's `POST
 *  /api/spans` schema accepts the full `rn|node|py|web` union. */
export type SpanLayer = "web";
export type SpanRating = "good" | "needs-work" | "poor";

/** One privacy-safe root/child span. Wire shape is byte-identical across all
 *  runtimes and matches the server's `POST /api/spans` schema. */
export interface TraceSpan {
  traceId: string;
  layer: SpanLayer;
  routeLabel: string;
  durationMs: number;
  startOffsetMs: number;
  rating: SpanRating;
  spanId?: string | null;
  parentSpanId?: string | null;
  /** What KIND of work this was, and whether it WORKED. Same rules as the
   *  causality pair: optional, drawn from the shared vocabulary in
   *  `spanWork.ts`, and omitted entirely when the kit has nothing to say —
   *  never sent as a placeholder, because an absent field reads as "this kit
   *  does not report it" and a value would read as a measurement. */
  kind?: SpanWorkKind | null;
  outcome?: SpanOutcome | null;
}

/** Server accepts at most 50 spans per batch. */
export const MAX_SPAN_BATCH = 50;
/** Hard cap on the in-memory buffer (drop-oldest on overflow) so a burst of
 *  requests between flushes can never grow memory without bound. */
export const MAX_BUFFERED_SPANS = 50;
/** `routeLabel` server cap. */
/** Auto-flush cadence. */
export const SPAN_FLUSH_MS = 15_000;
/** Duration hard clamp (matches the server `durationMs` 0..600000 bound). */
export const MAX_SPAN_DURATION_MS = 600_000;

/* ─── Eager sending, on novelty only ────────────────────────────────────────
 * The cadence above is right for repetition and wrong for the first sight of
 * a part: a developer clicking through a new app is watching a dashboard that
 * cannot show a route until the batch carrying it goes out. So a span that is
 * NEW to this kit (see spanNovelty.ts) brings the next flush forward. It is a
 * scheduling change and nothing else — same buffer, same submitter, same
 * transport, same gates, same back-off, no second uploader, and no extra
 * payload.
 */

/**
 * How long a burst is allowed to settle before the eager flush fires.
 *
 * A page opening for the first time discovers several parts within a few
 * hundred milliseconds; waiting this long turns that burst into ONE request
 * rather than one per part. Short enough that the dashboard's own refresh
 * (about two seconds) still lands the whole thing inside the three-second
 * budget the map is held to.
 */
export const NOVELTY_SETTLE_MS = 800;

/**
 * The ceiling: at most one eager flush per this many milliseconds.
 *
 * An app minting new labels continuously cannot turn novelty into a flood —
 * past this rate the extra novelty simply rides the ordinary timer. Novelty
 * arriving inside the gap is not dropped; its flush is scheduled for the
 * moment the gap expires, which is still far sooner than the 15s cadence.
 */
export const NOVELTY_MIN_GAP_MS = 5_000;

/* A trace span is a NETWORK round-trip, not a page paint, so it is rated on the
 * SAME cross-runtime span band as every other layer (the shared TTI band:
 * good ≤500ms / poor ≥1500ms, `poor` boundary INCLUSIVE), NOT the browser LCP
 * band (`rateDuration` default). Otherwise the very same 1.2s call would read
 * "good" from the browser and "poor" from Node inside one waterfall. Keeping
 * the band identical is what makes the one honest full-stack score honest. */
export const SPAN_GOOD_MS = 500;
export const SPAN_POOR_MS = 1500;

/** Classify a request duration against the shared cross-runtime span band —
 *  byte-identical to RN `rateSpanDuration`, Node `rateDuration`, and Python
 *  `rate_duration`. */
export function rateSpanDuration(ms: number): SpanRating {
  if (ms <= SPAN_GOOD_MS) return "good";
  if (ms >= SPAN_POOR_MS) return "poor";
  return "needs-work";
}

/** Extract just the pathname from a URL-ish string (never the scheme/host,
 *  query, or hash), so `spanLabel` never carries an origin or query value. */
function pathOf(url: string): string {
  try {
    // Resolve against a dummy base so relative URLs parse too.
    return new URL(url, "http://x").pathname;
  } catch {
    const q = url.search(/[?#]/);
    return q >= 0 ? url.slice(0, q) : url;
  }
}

/** Build the code-defined `${METHOD} ${normalizedPath}` label, or refuse it. */
export function spanLabel(method: string, url: string): string | null {
  const path = normalizeRouteLabel(pathOf(url));
  if (path === null) return null;
  const label = `${String(method).toUpperCase()} ${path}`;
  return label.length <= MAX_PART_NAME ? label : null;
}

export type SpanSubmitter = (spans: TraceSpan[]) => Promise<number>;

let submitter: SpanSubmitter | null = null;
let buffer: TraceSpan[] = [];
let timer: ReturnType<typeof setTimeout> | null = null;
let active = false;
let inFlight = false;

/* Eager-flush state. `eagerTimer` being non-null IS the coalescing window: a
 * second novel span inside it joins the flush already scheduled instead of
 * scheduling another. */
let eagerTimer: ReturnType<typeof setTimeout> | null = null;
let lastEagerAt = 0;
let eagerFlushes = 0;
let eagerDeferrals = 0;
/* One re-arm per settle window, no more: a transport that never finishes must
 * not leave a timer rescheduling itself for the life of the page. */
let eagerRearmed = false;

/* ─── Trace-root clock (elapsed chain) ──────────────────────────────────────
 * The web layer is a trace ROOT: the first request of a trace defines T0, and
 * every later request on the SAME trace id carries `now − T0` as its
 * startOffsetMs AND forwards it on the elapsed header so downstream hops can
 * chain their own offsets from it. The map holds only { random 32-hex id →
 * Date.now() } — no PII — and is bounded drop-oldest, cleared by `forget()`
 * and the test reset. */
const MAX_TRACE_ROOTS = 50;
const traceRoots = new Map<string, number>();

/* ─── Traces whose T0 was dropped ───────────────────────────────────────────
 * A page firing more parallel calls than the table holds — or one long call
 * still open while dozens of quick ones come and go — can push its OWN trace's
 * T0 out of the table before the next leg is recorded. The ids of the dropped
 * traces are remembered here (bounded the same way), because a trace the kit
 * has FORGOTTEN must not be re-rooted as if it were new: that reports the leg
 * at offset 0, draws it at the left edge of the waterfall, and lets a long
 * later leg out-rank the leg the action really began on and be named the
 * root — exactly the fault the per-trace clock exists to prevent. So the kit
 * says "unknown" instead, and the leg goes unreported and counted. Past this
 * many further evictions even that memory is gone and the id looks new. */
const MAX_FORGOTTEN_TRACES = 50;
const forgottenTraces = new Set<string>();
let unplaced = 0;

/** Return this trace's root start time, registering `now` as T0 for a trace
 *  seen for the first time, or `null` for a trace whose T0 this kit has
 *  already dropped. Bounded drop-oldest (Map preserves insert order). */
function rootStartFor(traceId: string, now: number): number | null {
  const existing = traceRoots.get(traceId);
  if (existing !== undefined) return existing;
  if (forgottenTraces.has(traceId)) {
    unplaced += 1;
    return null;
  }
  traceRoots.set(traceId, now);
  while (traceRoots.size > MAX_TRACE_ROOTS) {
    const oldest = traceRoots.keys().next().value;
    if (oldest === undefined) break;
    traceRoots.delete(oldest);
    forgottenTraces.add(oldest);
    while (forgottenTraces.size > MAX_FORGOTTEN_TRACES) {
      const stale = forgottenTraces.values().next().value;
      if (stale === undefined) break;
      forgottenTraces.delete(stale);
    }
  }
  return now;
}

/** Wire (or clear) the transport that ships buffered spans. Clearing it makes
 *  `enqueueSpan` inert immediately — the privacy-by-default gate. */
export function setSpanSubmitter(fn: SpanSubmitter | null): void {
  submitter = fn;
}

/** Drop the in-memory span buffer + trace-root clocks (called by `forget()`). */
export function clearBufferedSpans(): void {
  buffer = [];
  traceRoots.clear();
  forgottenTraces.clear();
  unplaced = 0;
  cancelEagerFlush();
  resetNovelty();
}

/** Stand down an armed eager flush. Nothing is lost: whatever it would have
 *  carried stays in the buffer for the ordinary cadence. */
function cancelEagerFlush(): void {
  if (eagerTimer) {
    clearTimeout(eagerTimer);
    eagerTimer = null;
  }
  eagerRearmed = false;
}

function armEagerFlush(waitMs: number): void {
  eagerTimer = setTimeout(() => {
    eagerTimer = null;
    fireEagerFlush();
  }, waitMs);
  // Node (tests, SSR) keeps the process alive for a pending timer; a browser
  // has no such notion. Guarded so neither environment needs its own path.
  (eagerTimer as unknown as { unref?: () => void }).unref?.();
}

function fireEagerFlush(): void {
  if (!submitter || isBoosthisDisabled()) {
    eagerRearmed = false;
    return;
  }
  if (inFlight) {
    // Something is already on the wire. It may well be carrying these very
    // rows, and a second request alongside it is exactly the extra round trip
    // this path promises not to add — so wait for the ceiling and look again,
    // once.
    eagerDeferrals += 1;
    if (!eagerRearmed) {
      eagerRearmed = true;
      armEagerFlush(NOVELTY_MIN_GAP_MS);
    }
    return;
  }
  eagerRearmed = false;
  if (buffer.length === 0) return;
  lastEagerAt = Date.now();
  eagerFlushes += 1;
  void tick();
}

/** Bring the next flush forward because this span showed the kit something it
 *  has never reported. Coalesced by the settle window and bounded by the rate
 *  ceiling; declines entirely when the ordinary cadence would get there
 *  first, so the buffer is never flushed twice for one burst. */
function considerEagerFlush(row: TraceSpan): void {
  // A flush hands the batch to the submitter and the batch is gone whether or
  // not the gate below accepted it. While the kit is inert — not yet
  // activated, revoked, paused, grace expired — every upload is refused, so
  // bringing a flush forward would throw away the first spans of a brand-new
  // install: precisely the page this exists to show. Stand down, and
  // deliberately do NOT judge the label, so the first sighting the kit can act
  // on is still the one that fires. The ordinary cadence keeps the spans.
  if (isRuntimeInert()) return;
  if (!isNovel(judgeNovelty(row))) return;
  if (eagerTimer) return; // already settling — this span joins that flush
  const sinceLast = Date.now() - lastEagerAt;
  const wait = Math.max(NOVELTY_SETTLE_MS, NOVELTY_MIN_GAP_MS - sinceLast);
  if (wait >= SPAN_FLUSH_MS) return;
  armEagerFlush(wait);
}

/** Buffer one root span for the next flush. No-op unless a submitter is wired
 *  (sharing authorized) and the kill-switch is off — so nothing span-shaped is
 *  even RETAINED on a private app. Clamps duration and drops the oldest span
 *  when the buffer is full. */
export function enqueueSpan(span: TraceSpan): void {
  if (span.routeLabel.length > MAX_PART_NAME) return;
  if (!submitter || isBoosthisDisabled()) return;
  const durationMs = Math.max(
    0,
    Math.min(MAX_SPAN_DURATION_MS, Math.round(span.durationMs)),
  );
  const startOffsetMs = Math.max(
    0,
    Math.min(MAX_SPAN_DURATION_MS, Math.round(span.startOffsetMs || 0)),
  );
  // Rebuild a closed span (never spread unknown keys) so the buffered shape
  // stays byte-identical to the other runtimes' closed span shape. The causality pair is validated
  // here and ADDED ONLY WHEN PRESENT: a malformed id is dropped rather than
  // forwarded, and a span with nothing to say about its caller sends the same
  // six keys it always did.
  const row: TraceSpan = {
    traceId: span.traceId,
    layer: span.layer,
    routeLabel: span.routeLabel,
    durationMs,
    startOffsetMs,
    rating: span.rating,
  };
  const spanId = sanitizeSpanId(span.spanId);
  if (spanId !== null) row.spanId = spanId;
  const parentSpanId = sanitizeSpanId(span.parentSpanId);
  if (parentSpanId !== null) row.parentSpanId = parentSpanId;
  const kind = sanitizeWorkKind(span.kind);
  if (kind !== null) row.kind = kind;
  const outcome = sanitizeOutcome(span.outcome);
  if (outcome !== null) row.outcome = outcome;
  buffer.push(row);
  while (buffer.length > MAX_BUFFERED_SPANS) buffer.shift();
  // Novelty is judged on the REBUILT row — the same bytes that will go on the
  // wire — and only after the privacy gate above has let the span be kept at
  // all. A kit with no submitter wired remembers nothing and schedules
  // nothing.
  considerEagerFlush(row);
}

/** Flush up to one batch. Guarded against re-entrancy + the kill-switch;
 *  swallows any transport error (returns 0). */
async function tick(): Promise<number> {
  if (!submitter || isBoosthisDisabled() || inFlight || buffer.length === 0) {
    return 0;
  }
  inFlight = true;
  const batch = buffer.splice(0, MAX_SPAN_BATCH);
  try {
    return await submitter(batch);
  } catch {
    return 0;
  } finally {
    inFlight = false;
  }
}

/** Manually flush the span buffer now (best-effort). */
export function flushSpansNow(): Promise<number> {
  return tick();
}

/** Start the periodic auto-flush loop. Idempotent. Does NOT fire an immediate
 *  flush (the buffer is empty at wire time). */
export function startSpanAutoFlush(): void {
  if (active) return;
  active = true;
  const loop = (): void => {
    if (!active) return;
    timer = setTimeout(() => {
      void tick().finally(() => {
        if (active) loop();
      });
    }, SPAN_FLUSH_MS);
  };
  loop();
}

/** Stop the auto-flush loop. Takes the eager path down with it: they are one
 *  schedule over one buffer, and a kit told to stop sending must not have a
 *  settle window still running. */
export function stopSpanAutoFlush(): void {
  active = false;
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
  cancelEagerFlush();
}

/* ─── traceFetch ────────────────────────────────────────────────────────────
 * A drop-in `fetch` wrapper that (1) adopts-or-mints the trace id and echoes it
 * on the `x-boosthis-trace` request header so the Node/Python hops can stitch
 * to it, and (2) times the round trip and buffers ONE root span. Headers are
 * duck-typed to `[k,v][]` tuples so it works with any `HeadersInit` shape, and
 * timing uses `Date.now()`, never an AbortController (which would change the
 * host request's cancellation semantics).
 */

type HeaderTuple = [string, string];

/** Normalize any `HeadersInit` (tuple[], plain object, or Headers-like) to
 *  tuples without relying on a specific `Headers` implementation. */
function headerEntries(h: unknown): HeaderTuple[] {
  if (!h) return [];
  if (Array.isArray(h)) {
    return h
      .filter((e) => Array.isArray(e) && e.length >= 2)
      .map(([k, v]) => [String(k), String(v)] as HeaderTuple);
  }
  const anyH = h as {
    forEach?: (cb: (v: string, k: string) => void) => void;
  };
  if (typeof anyH.forEach === "function") {
    const out: HeaderTuple[] = [];
    anyH.forEach((v, k) => out.push([String(k), String(v)]));
    return out;
  }
  return Object.entries(h as Record<string, unknown>).map(
    ([k, v]) => [k, String(v)] as HeaderTuple,
  );
}

/** Merge header sources (last wins, case-insensitive) into deduped tuples. */
function mergeHeaders(...sources: unknown[]): HeaderTuple[] {
  const map = new Map<string, HeaderTuple>();
  for (const src of sources) {
    for (const [k, v] of headerEntries(src)) {
      map.set(k.toLowerCase(), [k, v]);
    }
  }
  return [...map.values()];
}

function urlString(input: unknown): string {
  if (typeof input === "string") return input;
  const anyInput = input as { href?: unknown; url?: unknown };
  if (typeof anyInput?.href === "string") return anyInput.href;
  if (typeof anyInput?.url === "string") return anyInput.url;
  return String(input);
}

function methodOf(input: unknown, init: RequestInit | undefined): string {
  const m =
    init?.method ??
    (typeof input === "object" && input
      ? (input as { method?: unknown }).method
      : undefined) ??
    "GET";
  return String(m).toUpperCase();
}

/** Drop-in `fetch` that propagates the trace id and buffers a root span.
 *  Behaves exactly like `fetch` from the host's perspective — it returns the
 *  same Response and rethrows the same error; span bookkeeping happens in a
 *  `finally` and can never change the outcome. */
export async function traceFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
  fetchImpl?: typeof fetch,
): Promise<Response> {
  const f = fetchImpl ?? (globalThis as { fetch?: typeof fetch }).fetch;
  if (typeof f !== "function") {
    throw new TypeError("traceFetch: global fetch is not available");
  }

  // Adopt-or-mint the trace id from any existing header, then re-attach it.
  const merged = mergeHeaders(
    typeof input === "object" && input
      ? (input as { headers?: unknown }).headers
      : undefined,
    init?.headers,
  );
  let existing: string | undefined;
  const finalHeaders = merged.filter(([k, v]) => {
    const key = k.toLowerCase();
    if (key === TRACE_HEADER) {
      existing = v;
      return false;
    }
    // Web is the trace ROOT: it OWNS the elapsed clock, so any caller-supplied
    // elapsed header is discarded (sanitize-on-send, mirrors the id strip).
    if (key === ELAPSED_HEADER) return false;
    // Same reasoning for the parent: this call's caller is whatever scope is
    // open in THIS page, never something a caller handed us in a header.
    if (key === PARENT_HEADER) return false;
    return true;
  });
  const traceId = sanitizeTraceId(existing);
  finalHeaders.push([TRACE_HEADER, traceId]);

  // Causality: mint this call's own identity and take its caller from the span
  // scope open at this exact call site (null in the ordinary case of a fetch
  // fired straight from a screen). The next hop adopts `spanId` as ITS parent,
  // which is what turns a correlated timeline into a real parent-child chain.
  const span = beginSpan();
  finalHeaders.push([PARENT_HEADER, span.spanId]);

  const startedAt = Date.now();
  // Elapsed chain: first request of a trace defines T0 (offset 0); later
  // requests on the same trace carry `now − T0`, clamped to the shared bound.
  // String(≤600000) is ≤6 digits, so the outbound value always re-passes the
  // receive regex on the next hop. A trace whose T0 was dropped has NO honest
  // offset, so this leg is neither placed on the wire nor reported.
  const rootStart = rootStartFor(traceId, startedAt);
  const startOffsetMs =
    rootStart === null
      ? null
      : Math.min(MAX_TRACE_ELAPSED_MS, Math.max(0, startedAt - rootStart));
  if (startOffsetMs !== null) {
    finalHeaders.push([ELAPSED_HEADER, String(startOffsetMs)]);
  }

  // What the call answered with, for the outcome. `null` status with `threw`
  // set is a call that never got an answer at all — refused, blocked, reset,
  // timed out — which is the failure a status code can never express.
  let status: number | null = null;
  let threw = false;
  try {
    const res = await f(input, { ...init, headers: finalHeaders });
    const code = (res as { status?: unknown } | null)?.status;
    if (typeof code === "number") status = code;
    return res;
  } catch (err) {
    threw = true;
    throw err;
  } finally {
    // Best-effort span emission — never let it affect the host request.
    try {
      // The part name is settled by the one rule BEFORE anything is filed
      // under it. A name the rule refuses means this leg goes unrecorded
      // rather than being recorded under a name two parts could share.
      const routeLabel = spanLabel(methodOf(input, init), urlString(input));
      if (startOffsetMs !== null) {
        if (routeLabel !== null) enqueueSpan({
          traceId,
          layer: "web",
          routeLabel,
          durationMs: Date.now() - startedAt,
          startOffsetMs,
          rating: rateSpanDuration(Date.now() - startedAt),
          spanId: span.spanId,
          parentSpanId: span.parentSpanId,
          // This kit's span is always an outbound call it stood inside, so
          // the kind is `http`. The outcome is what the call itself did: a
          // 404 is the server answering, a 500 or a thrown transport failure
          // is the work not completing. Never the rating — a fast failure is
          // still a failure.
          kind: "http",
          outcome: outcomeForStatus(status, threw),
        });
      }
    } catch {
      // swallow — instrumentation must never take down the host.
    }
  }
}

/** Test/introspection hooks (mirrors `_prodSamplerInternals`). */
export const _spanInternals = {
  hasSubmitter: (): boolean => submitter !== null,
  bufferLen: (): number => buffer.length,
  isAutoRunning: (): boolean => active,
  flushMs: (): number => SPAN_FLUSH_MS,
  traceRootCount: (): number => traceRoots.size,
  /** Legs this kit refused to place because their trace's T0 had been dropped
   *  — the count that keeps that loss from being silent. */
  unplacedCount: (): number => unplaced,
  /** Flushes the novelty path brought forward. */
  eagerFlushCount: (): number => eagerFlushes,
  /** Times an eager flush stood down because a request was already on the
   *  wire — the count that proves it adds no round trip to one in flight. */
  eagerDeferralCount: (): number => eagerDeferrals,
  /** True while a settle window is open (a flush is scheduled). */
  eagerPending: (): boolean => eagerTimer !== null,
  settleMs: (): number => NOVELTY_SETTLE_MS,
  minGapMs: (): number => NOVELTY_MIN_GAP_MS,
  reset: (): void => {
    stopSpanAutoFlush();
    submitter = null;
    buffer = [];
    inFlight = false;
    traceRoots.clear();
    forgottenTraces.clear();
    unplaced = 0;
    cancelEagerFlush();
    resetNovelty();
    lastEagerAt = 0;
    eagerFlushes = 0;
    eagerDeferrals = 0;
  },
};
