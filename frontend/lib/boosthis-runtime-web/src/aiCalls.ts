/* ─── Boosthis: AI-call meter (Web) ────────────────────────────────────────
 *
 * WHY THIS EXISTS. An AI product built as a front end usually calls the model
 * FROM THE PAGE. Until now the browser kit saw "an outbound call took 8.2
 * seconds" and nothing else — outbound calls are deliberately reduced to a
 * group token and a duration — so the slowest and by far the most expensive
 * thing the page does was the one thing we said nothing about.
 *
 * Streaming made it worse. When an answer is streamed, total duration is close
 * to meaningless: what matters is how long before the first words appeared and
 * whether the stream stalled halfway. Nothing measured either.
 *
 * WHERE IT WATCHES, AND WHY THERE. The kit already wraps the page's `fetch`
 * and `XMLHttpRequest` (callWatch.ts) to see when a call starts and how it
 * ends. This module is fed BY those wrappers — no second wrapper, no second
 * pass-through — and adds the one thing they never read: what the provider's
 * own reply says about usage, cost and rate limits.
 *
 * THE THREE BROWSER TRUTHS, SAID OUT LOUD RATHER THAN HIDDEN:
 *
 *   • A cross-origin reply only shows the headers the provider CHOSE to
 *     expose. Where the rate-limit headers are hidden from the page, the
 *     headroom reading abstains with a reason ({@link AiCallStats.headersHiddenCalls})
 *     instead of reporting a comfortable zero.
 *   • A page can reach a provider through transports this kit does not wrap —
 *     `EventSource`, `sendBeacon`, a `<script>` tag. Those are counted as a
 *     blind spot from the browser's own resource timeline rather than quietly
 *     reported as an app with no AI calls.
 *   • The unit of work is the USER'S TURN, not a server request. A turn opens
 *     at the input that preceded the call (or at the call itself, when no
 *     input plausibly caused it) and closes when the AI work under it goes
 *     quiet — so "share of the wait spent on the model" means the share of the
 *     wait a person actually sat through.
 *
 * WHAT NEVER HAPPENS HERE:
 *   • No prompt and no answer leaves. Reading of the reply is confined to
 *     `aiUsage.ts`, which can only return numbers; a streamed answer's content
 *     deltas are not even handed to a JSON parser.
 *   • No address and no credential leaves. A destination becomes a NUMBER from
 *     the maintained provider list (`aiProviders.ts`) or it is not an AI call
 *     at all, and no request header — the authorization header above all — is
 *     ever read.
 *   • The page's own call is never changed. Every observation is individually
 *     guarded, and nothing is buffered on the app's behalf: the reply is
 *     observed as the app itself reads it.
 *   • The kit's own uploads are never counted as the app's AI traffic. They go
 *     out through `unwatchedFetch()`, which is the page's ORIGINAL fetch, and
 *     the classifier refuses our own endpoint besides.
 *
 * PROMPT IDENTITY, AND WHY IT IS SAFE. "The same prompt sent repeatedly" is
 * one of the fixes this reading is here to name, and repetition is only
 * judgeable if two calls can be told apart. The request body is therefore
 * folded into a 32-bit in-page hash on the stack and dropped. The text is
 * never stored, never logged and NEVER leaves the page; only the count of how
 * many times one identity recurred inside one turn is reported.
 *
 * ADDITIVE. Three axes (`aiCalls`, `aiSpend`, `aiHeadroom`), the same field
 * names the Node kit produces, none of which touches the composite Speed
 * score.
 *
 * HONEST WHEN THERE IS NOTHING TO SAY. A page that calls no AI provider
 * uploads NO axis at all, which every surface reads as "cannot tell" — never a
 * row of zeros.
 */

import { isBoosthisDisabled } from "./runtimeFlags";
import {
  aiProviderCodeOrDeclared,
  AI_PROVIDERS,
  DECLARED_PROVIDER_CODE,
} from "./aiProviders";
import {
  estimateCostMicros,
  readAiHeaders,
  readAiOutcome,
  readAiUsage,
  streamLineIsInteresting,
  streamLineIsTerminal,
  type AiUsageRead,
} from "./aiUsage";
import { isOwnEndpointReply, pageOriginOf } from "./edgeCache";
import type { AiCallStatsLike } from "./meterAxes";

/* ── Limits (memory can never grow with traffic) ─────────────────────────── */

/** Most distinct prompt identities tracked inside ONE turn. */
const MAX_DISTINCT_PROMPTS = 128;
/** Longest slice of a request body folded into a prompt identity. */
const MAX_PROMPT_CHARS = 4096;
/** Ring of call durations kept for the percentile readings. */
const DURATION_RING = 256;
/** Longest partial line held while splitting a streamed reply. A usage event
 *  is a few hundred bytes; past this the carry is dropped rather than grown. */
const MAX_STREAM_CARRY = 8192;
/** Longest error body read back to classify a refusal. */
const MAX_ERROR_BODY_CHARS = 8192;

/**
 * A gap this long between two pieces of a streamed answer is a STALL.
 *
 * Chosen against what a person waiting for words actually experiences: below
 * ten seconds a reader assumes the model is thinking, past it they assume the
 * app is broken. The same threshold the Node kit uses, so one number cannot
 * mean two things in two kits.
 */
export const AI_STREAM_STALL_MS = 10_000;

/**
 * Average input tokens above which a cold prompt cache is worth naming.
 *
 * Below this, prompt caching would save little and its absence is not a
 * finding. Mirrors the smallest cacheable prefix the major providers support.
 */
export const AI_CACHEABLE_INPUT_TOKENS = 2048;

/** AI calls in one turn needed before "these ran one after another" is a
 *  finding rather than an app that simply makes one call. */
export const AI_SERIAL_THRESHOLD = 2;

/**
 * How long an input can precede an AI call and still be read as its cause.
 *
 * The browser's honest denominator is the wait a PERSON sat through, which
 * begins at the click or keystroke that sent the prompt. Past this gap the
 * input plainly did not cause the call (a timer, a poll, a page-load fetch),
 * so the turn begins at the call itself — a wait that is entirely the model's
 * is reported as entirely the model's, never diluted by minutes of idle page.
 */
export const AI_TURN_LEAD_MS = 5_000;

/**
 * Quiet after the last AI call in a turn before the next call opens a NEW one.
 *
 * Two calls back to back (classify, then answer) are one unit of work and must
 * be judgeable as "these ran one after another". A call a minute later is a
 * new turn. Only used when no input has intervened — an input always starts a
 * fresh turn on its own.
 */
export const AI_TURN_IDLE_MS = 1_000;

/* ── Transports that can carry an AI call past this meter ────────────────── */

/**
 * Resource-timing initiator types this kit does NOT wrap.
 *
 * The honest half of the deal: a page can reach a provider through an
 * `EventSource`, a beacon or an injected script, and none of those pass
 * through `fetch` or `XMLHttpRequest`. Seeing one in the browser's own
 * resource timeline, addressed to a known provider, is evidence — not a guess
 * — that this reading is incomplete, and the tile must say so rather than
 * report a clean picture built on the calls it happened to catch.
 *
 * `fetch` and `xmlhttprequest` are on the list too: they are only a blind spot
 * when the corresponding wrapper did NOT land (a frozen global, a locked-down
 * embedding), which {@link setWatchedTransports} records at install time.
 */
const UNWATCHABLE_INITIATORS: readonly string[] = [
  "beacon",
  "eventsource",
  "script",
  "iframe",
  "other",
  "fetch",
  "xmlhttprequest",
];

/** Most distinct unwatched transports remembered (the list above bounds it). */
const MAX_UNWATCHED_TRANSPORTS = UNWATCHABLE_INITIATORS.length;

/* ── Per-turn tally ──────────────────────────────────────────────────────── */

/** One user turn's AI work. Opaque to callers. */
interface AiTurn {
  /** Which input opened this turn (0 = none yet). */
  inputSeq: number;
  /** When the person's wait began (ms, monotonic). */
  startedAt: number;
  /** When the last AI call in this turn finished (ms, monotonic; -1 = none). */
  lastEndAt: number;
  /** AI calls still running under this turn. */
  inFlight: number;
  /** Prompt identity → how many times it was sent in this turn. */
  prompts: Map<number, number>;
  /** AI calls that finished inside this turn. */
  calls: number;
  /** Ms this turn spent on AI calls (wall time of each call). */
  ms: number;
  /** Longest run of AI calls that did NOT overlap each other. */
  serialRun: number;
  /** Current run's high-water mark for {@link serialRun}. */
  runLength: number;
  /** Micro-USD attributed to this turn. */
  costMicros: number;
  /** Already counted in `watchedRequests`. */
  counted: boolean;
  /** Wall ms already folded into `watchedRequestMs` (delta bookkeeping). */
  contributedWall: number;
  /** AI ms already folded into `aiMs` (delta bookkeeping). */
  contributedAi: number;
  /** Already counted in `promptRepeatRequests`. */
  repeatCounted: boolean;
  /** Already counted in `serialRequests`. */
  serialCounted: boolean;
}

/** The turn AI calls are currently filed under. One at a time; bounded. */
let currentTurn: AiTurn | null = null;
/** Bumped by every user input, so a new turn can be told from the old one. */
let inputSeq = 0;
/** When the most recent user input landed (ms, monotonic; 0 = none). */
let inputAt = 0;

/* ── Session totals (the only things that ever leave) ────────────────────── */

let watchedRequests = 0;
let watchedRequestMs = 0;
let aiMs = 0;

let callCount = 0;
let streamCount = 0;
let stallCount = 0;
let failCount = 0;
let rateLimitedCount = 0;
let quotaCount = 0;
let timeoutCount = 0;
let truncatedCount = 0;
let filteredCount = 0;
let noTimeLimitCalls = 0;
let retryNoBackoffCount = 0;

let tokensIn = 0;
let tokensOut = 0;
let cachedIn = 0;
let costMicros = 0;
let reportedCostCalls = 0;
let pricedCalls = 0;
let unpricedCalls = 0;
let worstRequestCostMicros = 0;
let usageMissingCalls = 0;
let streamUsageMissingCalls = 0;
/** Calls that went to an endpoint the app declared as its own. */
let declaredCalls = 0;
/** Declared calls with usage that we deliberately refused to list-price. */
let declaredUnpricedCalls = 0;

/**
 * Paths an OpenAI-compatible inference server answers on.
 *
 * WHAT THIS LIST IS FOR. A page can call a model that is not on the
 * maintained provider list and was never declared: the team's own gateway,
 * a regional endpoint, a sovereign host. The classifier reads the HOSTNAME,
 * so it says nothing about any of them, and without this list the page would
 * look like a page that calls no AI at all. A POST to one of these paths is
 * grounds for saying "there is AI-shaped traffic here I could not classify",
 * and for nothing else.
 *
 * WHAT IT MUST NOT DO. A matched call is never timed, never scored, never
 * counted as an AI call and never priced — the kit does not know whose model
 * it is. It increments one number. The list is the Node kit's, kept short and
 * closed for the same reason: a shape unrelated services also serve would
 * turn an honest blind-spot note into a false alarm.
 */
const AI_SHAPED_PATHS: readonly string[] = [
  // OpenAI-compatible — the shape almost every self-hosted server exposes.
  "/v1/chat/completions",
  "/v1/completions",
  "/v1/responses",
  "/v1/embeddings",
  // Anthropic-compatible.
  "/v1/messages",
  // Ollama's native API.
  "/api/generate",
  "/api/chat",
  "/api/embeddings",
  // Hugging Face text-generation-inference.
  "/generate_stream",
];

/**
 * Calls whose PATH matched {@link AI_SHAPED_PATHS} while their host matched
 * neither the maintained provider list nor anything this page declared.
 *
 * A count and nothing else. The address is compared inside the page and never
 * stored, so this number can say how much AI-shaped traffic went unclassified
 * and can never say where it went.
 */
let unclassifiedAiShaped = 0;

let promptRepeatWorst = 0;
let promptRepeatRequests = 0;
let serialWorst = 0;
let serialRequests = 0;

let headroomReads = 0;
let headersHiddenCalls = 0;
let worstRequestsPct: number | null = null;
let worstTokensPct: number | null = null;
let worstRetryAfterMs = 0;

let serverMsTotal = 0;
let serverMsCalls = 0;

/** Providers seen this session, by wire code. */
const providersSeen = new Set<number>();
/** Calls per provider code, to name the one this app mostly uses. */
const callsByProvider = new Map<number, number>();

/** Ring of total call durations (ms) for the percentile readings. */
const durations: number[] = [];
/** Ring of time-to-first-words (ms) for streamed replies. */
const ttfts: number[] = [];

/** When this meter started collecting (ms, monotonic) — the cost window. */
let windowStartedAt = 0;

/**
 * Rate-limit refusals, by provider code, with the wait the provider asked for.
 * Used to tell an app that retried too soon apart from one that waited.
 */
const lastRefusal = new Map<number, { at: number; waitMs: number }>();

/** Unwatched transports seen reaching a provider (initiator type strings). */
const unwatchedTransports = new Set<string>();
/** Which transports this kit actually wrapped, from the install that landed. */
let watchedFetch = false;
let watchedXhr = false;

/* ── Identity (local, non-reversible, never uploaded) ────────────────────── */

/** 32-bit FNV-1a. Fast, allocation-free, stable within one page — which is all
 *  an identity needs to be, since it never leaves. */
function fnv1a(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/**
 * Fold a request body into a prompt identity, or 0 when there is nothing to
 * fold.
 *
 * Only a STRING body is read — every provider SDK sends JSON text — and only a
 * bounded prefix of it, plus its length so two long prompts sharing a prefix
 * stay distinct. The string is hashed on this stack and dropped. A stream, a
 * Blob or a FormData body is not read at all: reading it would consume the
 * app's own request.
 */
function promptIdentity(body: unknown): number {
  try {
    if (typeof body !== "string" || !body) return 0;
    const head =
      body.length > MAX_PROMPT_CHARS ? body.slice(0, MAX_PROMPT_CHARS) : body;
    return fnv1a(`${body.length}:${head}`) || 1;
  } catch {
    return 0;
  }
}

/* ── One call in flight ──────────────────────────────────────────────────── */

interface AiCallCtx {
  provider: number;
  turn: AiTurn | null;
  startedAt: number;
  headersAt: number;
  firstChunkAt: number;
  lastChunkAt: number;
  worstGapMs: number;
  promptId: number;
  hadTimeLimit: boolean;
  streamed: boolean;
  sawTerminator: boolean;
  usage: AiUsageRead | null;
  /** The reply came from another origin, so the browser decides what we see. */
  crossOrigin: boolean;
  /** Bits for the refusal kinds already tallied for this one call. */
  counted: number;
  /** Set once, so a call can never be counted twice. */
  filed: boolean;
}

/** The clock every AI measurement is taken on: sub-millisecond and monotonic,
 *  which is what "did these calls run one after another?" needs. */
function nowMs(): number {
  try {
    const p = (globalThis as { performance?: { now?: () => number } })
      .performance;
    if (typeof p?.now === "function") return p.now();
  } catch {
    /* fall through to the wall clock */
  }
  return Date.now();
}

/* ── Turns ───────────────────────────────────────────────────────────────── */

/**
 * A person just drove the page.
 *
 * Called from the kit's existing passive input listeners — no new listener, no
 * new sampling timer. Only the timestamp is read; what was typed or clicked is
 * never seen by this module.
 */
export function noteAiTurnBoundary(): void {
  try {
    if (!feedOn) return;
    inputSeq++;
    inputAt = nowMs();
  } catch {
    /* never disturb the page */
  }
}

/** The turn a call starting now belongs to, opening a new one when the
 *  previous turn's work is over. */
function turnForStart(startedAt: number): AiTurn {
  let turn = currentTurn;
  if (
    turn &&
    (turn.inputSeq !== inputSeq ||
      (turn.inFlight === 0 &&
        turn.lastEndAt >= 0 &&
        startedAt - turn.lastEndAt > AI_TURN_IDLE_MS))
  ) {
    turn = null;
  }
  if (!turn) {
    const causedByInput = inputAt > 0 && startedAt - inputAt <= AI_TURN_LEAD_MS;
    turn = {
      inputSeq,
      startedAt: causedByInput ? inputAt : startedAt,
      lastEndAt: -1,
      inFlight: 0,
      prompts: new Map(),
      calls: 0,
      ms: 0,
      serialRun: 0,
      runLength: 0,
      costMicros: 0,
      counted: false,
      contributedWall: 0,
      contributedAi: 0,
      repeatCounted: false,
      serialCounted: false,
    };
    currentTurn = turn;
  }
  turn.inFlight++;
  return turn;
}

/**
 * Fold a turn's current state into the session totals.
 *
 * Idempotent by construction: each turn remembers what it has already
 * contributed and only the DELTA is applied, so a turn that grows a second and
 * a third call is never counted twice — and a turn still open when the
 * snapshot is taken has already contributed everything known about it.
 */
function syncTurn(turn: AiTurn): void {
  if (turn.calls === 0) return;
  if (!turn.counted) {
    watchedRequests++;
    turn.counted = true;
  }
  const wall = Math.max(0, turn.lastEndAt - turn.startedAt);
  watchedRequestMs += wall - turn.contributedWall;
  turn.contributedWall = wall;
  // Concurrency can make the summed call time exceed the turn's own wall time;
  // clamping keeps the share honest rather than past 100%.
  const ai = wall > 0 ? Math.min(turn.ms, wall) : turn.ms;
  aiMs += ai - turn.contributedAi;
  turn.contributedAi = ai;

  let worst = 0;
  for (const n of turn.prompts.values()) if (n > worst) worst = n;
  if (worst > promptRepeatWorst) promptRepeatWorst = worst;
  if (worst > 1 && !turn.repeatCounted) {
    promptRepeatRequests++;
    turn.repeatCounted = true;
  }
  if (turn.serialRun > serialWorst) serialWorst = turn.serialRun;
  if (turn.serialRun >= AI_SERIAL_THRESHOLD && !turn.serialCounted) {
    serialRequests++;
    turn.serialCounted = true;
  }
}

/* ── Filing ──────────────────────────────────────────────────────────────── */

function pushRing(ring: number[], value: number): void {
  ring.push(value);
  if (ring.length > DURATION_RING) ring.shift();
}

/**
 * Finish one AI call: fold it into the session totals and into the turn that
 * issued it.
 *
 * Called exactly once per call, on every path out (clean finish, refusal,
 * transport error, abandoned stream). Never throws.
 */
function fileCall(ctx: AiCallCtx): void {
  if (ctx.filed) return;
  ctx.filed = true;
  try {
    const end = nowMs();
    const totalMs = Math.max(0, end - ctx.startedAt);
    callCount++;
    providersSeen.add(ctx.provider);
    callsByProvider.set(
      ctx.provider,
      (callsByProvider.get(ctx.provider) ?? 0) + 1,
    );
    pushRing(durations, totalMs);
    if (!ctx.hadTimeLimit) noTimeLimitCalls++;
    if (ctx.streamed) {
      streamCount++;
      if (ctx.firstChunkAt > 0) {
        pushRing(ttfts, Math.max(0, ctx.firstChunkAt - ctx.startedAt));
      }
      if (ctx.worstGapMs >= AI_STREAM_STALL_MS) stallCount++;
      // A stream that ended without the provider's own terminal event did not
      // finish its answer — the same failure as a completion cut off by a
      // token cap, and told apart from an ordinary success.
      if (!ctx.sawTerminator) truncatedCount++;
      if (!ctx.usage || ctx.usage.reported === 0) streamUsageMissingCalls++;
    }
    const declared = ctx.provider === DECLARED_PROVIDER_CODE;
    if (declared) declaredCalls++;
    if (!ctx.usage || ctx.usage.reported === 0) {
      usageMissingCalls++;
      unpricedCalls++;
    } else {
      const u = ctx.usage;
      tokensIn += u.tokensIn;
      tokensOut += u.tokensOut;
      cachedIn += u.cachedIn;
      let micros: number | null = u.reportedCostMicros;
      if (micros !== null) {
        reportedCostCalls++;
        pricedCalls++;
      } else if (declared) {
        // A private deployment's model name does not establish its price.
        // Tokens stay counted; only the endpoint's own reported cost is taken.
        micros = null;
        unpricedCalls++;
        declaredUnpricedCalls++;
      } else {
        micros = estimateCostMicros(u);
        if (micros !== null) pricedCalls++;
        else unpricedCalls++;
      }
      if (micros !== null && micros > 0) {
        costMicros += micros;
        if (ctx.turn) ctx.turn.costMicros += micros;
      }
    }

    // Turn-scoped folding. The turn was captured when the call was ISSUED,
    // never read here: a reply arrives long after the person moved on, and
    // reading the CURRENT turn at completion time would file a call against a
    // turn that did not make it.
    const turn = ctx.turn;
    if (turn) {
      if (turn.inFlight > 0) turn.inFlight--;
      turn.calls++;
      turn.ms += totalMs;
      if (ctx.promptId !== 0) {
        if (
          turn.prompts.has(ctx.promptId) ||
          turn.prompts.size < MAX_DISTINCT_PROMPTS
        ) {
          turn.prompts.set(
            ctx.promptId,
            (turn.prompts.get(ctx.promptId) ?? 0) + 1,
          );
        }
      }
      // "Ran one after another" = this call started at or after the previous
      // one in the same turn had already finished.
      if (turn.lastEndAt >= 0 && ctx.startedAt >= turn.lastEndAt) {
        turn.runLength = Math.max(2, turn.runLength + 1);
      } else {
        turn.runLength = 1;
      }
      if (turn.runLength > turn.serialRun) turn.serialRun = turn.runLength;
      turn.lastEndAt = end;
      if (turn.costMicros > worstRequestCostMicros) {
        worstRequestCostMicros = turn.costMicros;
      }
      syncTurn(turn);
    }
  } catch {
    /* a meter must never disturb the page */
  }
}

/**
 * Count a call that never produced an answer, and tell a deadline apart from
 * an ordinary transport failure.
 *
 * The error's own CLASS is read — its `name` and its `code`, both of which are
 * fixed identifiers from a runtime, never text a provider wrote. The message
 * is not read: it is the one field on an error that can contain a prompt, a
 * URL or a key.
 */
function noteTransportFailure(err: unknown): void {
  try {
    const name = String((err as { name?: unknown })?.name ?? "");
    const code = String((err as { code?: unknown })?.code ?? "");
    failCount++;
    if (
      /timeout/i.test(name) ||
      /timeout/i.test(code) ||
      name === "AbortError" ||
      name === "TimeoutError"
    ) {
      timeoutCount++;
    }
  } catch {
    /* never disturb the page */
  }
}

/**
 * Fold the quiet that ran from the last chunk to now into the worst gap.
 *
 * Called where the SOURCE stopped — the stream ended, or it broke — never
 * where the app stopped reading.
 */
function noteQuietSinceLastChunk(ctx: AiCallCtx): void {
  try {
    if (ctx.lastChunkAt > 0) {
      ctx.worstGapMs = Math.max(ctx.worstGapMs, nowMs() - ctx.lastChunkAt);
    }
  } catch {
    /* never disturb the page */
  }
}

/* ── Reply observation ───────────────────────────────────────────────────── */

/**
 * Note the rate-limit headroom the provider published on one reply.
 *
 * Reads through a getter so a `Response` and an `XMLHttpRequest` are handled by
 * the same code. When a cross-origin reply exposes NOTHING — the browser's
 * default, unless the provider opted in with `Access-Control-Expose-Headers` —
 * the call is counted as one whose ceiling was HIDDEN from the page, which is
 * what lets the headroom reading abstain out loud instead of reporting a
 * comfortable zero.
 */
function noteHeadroomFrom(
  ctx: AiCallCtx,
  get: (name: string) => string | null,
  status: number,
): void {
  try {
    const read = readAiHeaders(get);
    let sawAny = false;
    if (read.requestsRemaining !== null && read.requestsLimit) {
      const pct = Math.max(
        0,
        Math.min(100, (read.requestsRemaining / read.requestsLimit) * 100),
      );
      worstRequestsPct =
        worstRequestsPct === null ? pct : Math.min(worstRequestsPct, pct);
      sawAny = true;
    }
    if (read.tokensRemaining !== null && read.tokensLimit) {
      const pct = Math.max(
        0,
        Math.min(100, (read.tokensRemaining / read.tokensLimit) * 100),
      );
      worstTokensPct =
        worstTokensPct === null ? pct : Math.min(worstTokensPct, pct);
      sawAny = true;
    }
    if (read.retryAfterMs !== null) {
      worstRetryAfterMs = Math.max(worstRetryAfterMs, read.retryAfterMs);
      sawAny = true;
    }
    if (sawAny) headroomReads++;
    if (read.serverMs !== null) {
      serverMsTotal += read.serverMs;
      serverMsCalls++;
    }
    // The abstention evidence: another origin answered and the page was shown
    // no ceiling at all. Same-origin replies are excluded — there the provider
    // (or the app's own proxy) simply sent no rate-limit headers, which is a
    // different fact and already told by `reads === 0`.
    if (!sawAny && read.serverMs === null && ctx.crossOrigin) {
      headersHiddenCalls++;
    }
    if (status === 429) {
      const prev = lastRefusal.get(ctx.provider);
      const waited = prev ? ctx.startedAt - prev.at : Infinity;
      if (prev && waited < prev.waitMs) retryNoBackoffCount++;
      lastRefusal.set(ctx.provider, {
        at: nowMs(),
        waitMs: read.retryAfterMs ?? 1000,
      });
    }
  } catch {
    /* never disturb the page */
  }
}

/* One refusal can announce itself twice — once in the HTTP status, once in a
 * code inside the body — and a streamed answer can repeat its ending on every
 * event. Each kind is therefore counted at most ONCE per call, whichever place
 * says it first, so a tally can never exceed the number of calls behind it. */
const COUNT_QUOTA = 1;
const COUNT_RATE = 2;
const COUNT_FILTERED = 4;
const COUNT_TRUNCATED = 8;

function countOnce(ctx: AiCallCtx, bit: number): boolean {
  if ((ctx.counted & bit) !== 0) return false;
  ctx.counted |= bit;
  return true;
}

/** Count a refusal's own status, before its body is looked at. */
function noteRefusalStatus(ctx: AiCallCtx, status: number): void {
  failCount++;
  if (status === 429 && countOnce(ctx, COUNT_RATE)) rateLimitedCount++;
  if (status === 402 && countOnce(ctx, COUNT_QUOTA)) quotaCount++;
}

/** Read a refused reply's own code so a rate limit, a billing stop and a
 *  content refusal are told apart. Runs on already-decoded text, never on a
 *  body the app has not asked for. */
function classifyRefusalText(ctx: AiCallCtx, text: unknown): void {
  try {
    if (typeof text !== "string" || !text) return;
    if (text.length > MAX_ERROR_BODY_CHARS) return;
    const outcome = readAiOutcome(JSON.parse(text));
    if (outcome.quota) {
      if (countOnce(ctx, COUNT_QUOTA)) quotaCount++;
    } else if (outcome.rateLimited && countOnce(ctx, COUNT_RATE)) {
      rateLimitedCount++;
    }
    if (outcome.filtered && countOnce(ctx, COUNT_FILTERED)) filteredCount++;
  } catch {
    /* a refusal we cannot classify stays an ordinary failure */
  }
}

/** A refused `fetch` reply: classify it from OUR OWN clone, so the app's
 *  response object is never touched, delayed or consumed. */
function classifyRefusal(ctx: AiCallCtx, res: Response): void {
  let copy: Response | null = null;
  try {
    copy = res.clone();
  } catch {
    copy = null;
  }
  if (!copy) return;
  void copy
    .text()
    .then((text) => classifyRefusalText(ctx, text))
    .catch(() => {
      /* never disturb the page */
    });
}

/** Fold one parsed reply's usage + ending into the call in flight. */
function noteBody(ctx: AiCallCtx, parsed: unknown): void {
  try {
    const usage = readAiUsage(parsed);
    if (usage.reported === 1) {
      ctx.usage = ctx.usage
        ? {
            reported: 1,
            tokensIn: Math.max(ctx.usage.tokensIn, usage.tokensIn),
            tokensOut: Math.max(ctx.usage.tokensOut, usage.tokensOut),
            cachedIn: Math.max(ctx.usage.cachedIn, usage.cachedIn),
            reportedCostMicros:
              usage.reportedCostMicros ?? ctx.usage.reportedCostMicros,
            priceCode: usage.priceCode || ctx.usage.priceCode,
          }
        : usage;
    }
    const outcome = readAiOutcome(parsed);
    if (outcome.truncated && countOnce(ctx, COUNT_TRUNCATED)) truncatedCount++;
    if (outcome.filtered && countOnce(ctx, COUNT_FILTERED)) filteredCount++;
    // The provider said how this answer ended. Some providers (Gemini/Vertex)
    // have no terminal event at all and say it only here, so a reply that
    // names its ending is a finished reply — and one already counted as cut
    // off by a token cap must not be counted a second time at EOF.
    if (outcome.ended) ctx.sawTerminator = true;
  } catch {
    /* never disturb the page */
  }
}

/**
 * Watch a streamed reply as the app itself reads it.
 *
 * Content deltas are never parsed: {@link streamLineIsInteresting} skips every
 * line that does not announce usage or an ending, so an answer's words are
 * split on newlines, tested for a handful of marker substrings, and dropped.
 * Nothing is buffered: the wrapper hands each chunk straight on.
 */
function observeStream(ctx: AiCallCtx, res: Response): void {
  let wrapped: ReadableStream<Uint8Array> | null | undefined;
  const original = res.body;
  if (!original) {
    fileCall(ctx);
    return;
  }
  const build = (): ReadableStream<Uint8Array> | null => {
    if (wrapped !== undefined) return wrapped ?? null;
    try {
      const reader = original.getReader();
      const decoder = new TextDecoder();
      let carry = "";
      wrapped = new ReadableStream<Uint8Array>({
        async pull(controller) {
          let out: { done?: boolean; value?: Uint8Array };
          try {
            out = await reader.read();
          } catch (err) {
            // The stream broke part-way. Whatever the cause, this call did not
            // deliver its answer, so it is a failure — and an aborted read is
            // the shape a deadline takes on a stream.
            noteQuietSinceLastChunk(ctx);
            noteTransportFailure(err);
            fileCall(ctx);
            controller.error(err);
            return;
          }
          if (out.done) {
            // The quiet BEFORE the end counts. A gap is otherwise only noticed
            // when a next chunk arrives, so the commonest stall of all — an
            // answer that goes silent half-way and then just stops — would be
            // filed as a clean, if slow, stream. Folded here and on the error
            // path, where the source went quiet, and deliberately NOT on
            // cancel, where it was the app that stopped reading.
            noteQuietSinceLastChunk(ctx);
            try {
              controller.close();
            } finally {
              fileCall(ctx);
            }
            return;
          }
          try {
            const at = nowMs();
            if (ctx.firstChunkAt === 0) ctx.firstChunkAt = at;
            else ctx.worstGapMs = Math.max(ctx.worstGapMs, at - ctx.lastChunkAt);
            ctx.lastChunkAt = at;
            carry += decoder.decode(out.value, { stream: true });
            if (carry.length > MAX_STREAM_CARRY && !carry.includes("\n")) {
              // One line longer than any usage event can be — drop it rather
              // than hold an answer's text in memory waiting for a newline.
              carry = "";
            }
            let nl = carry.indexOf("\n");
            while (nl >= 0) {
              const line = carry.slice(0, nl);
              carry = carry.slice(nl + 1);
              if (streamLineIsTerminal(line)) ctx.sawTerminator = true;
              if (streamLineIsInteresting(line)) {
                const brace = line.indexOf("{");
                if (brace >= 0) {
                  try {
                    noteBody(ctx, JSON.parse(line.slice(brace)));
                  } catch {
                    /* not JSON — nothing to read */
                  }
                }
              }
              nl = carry.indexOf("\n");
            }
          } catch {
            /* observation only — the chunk below still reaches the app */
          }
          if (out.value !== undefined) controller.enqueue(out.value);
        },
        cancel(reason) {
          try {
            void reader.cancel(reason);
          } finally {
            fileCall(ctx);
          }
        },
      });
    } catch {
      wrapped = null;
    }
    return wrapped ?? null;
  };
  try {
    Object.defineProperty(res, "body", {
      configurable: true,
      get(): ReadableStream<Uint8Array> | null {
        return build() ?? original;
      },
    });
  } catch {
    // Could not shadow the body — file what is already known rather than leave
    // the call in flight forever.
    fileCall(ctx);
  }
}

/**
 * Watch a plain (non-streamed) reply, by shadowing the two readers an SDK
 * uses. Nothing is read on the app's behalf: if the app never reads the body,
 * the call is filed at header time with no usage, which is the truth.
 */
function observeBody(ctx: AiCallCtx, res: Response): void {
  let done = false;
  /** The app has CALLED a reader. Reading a body is real I/O, so the backstop
   *  below must not fire while one is in flight — filing early would close the
   *  call with no usage and then discard the token counts that arrive a moment
   *  later, which is how a perfectly ordinary `await res.json()` ends up
   *  reported as "this provider told us nothing". */
  let reading = false;
  const finish = (): void => {
    if (done) return;
    done = true;
    fileCall(ctx);
  };
  const shadow = (name: "json" | "text"): void => {
    try {
      const original = (res as unknown as Record<string, unknown>)[name];
      if (typeof original !== "function") return;
      Object.defineProperty(res, name, {
        configurable: true,
        writable: true,
        value: async function (...args: unknown[]): Promise<unknown> {
          reading = true;
          let value: unknown;
          try {
            value = await (
              original as (...a: unknown[]) => Promise<unknown>
            ).apply(res, args);
          } catch (err) {
            finish();
            throw err;
          }
          try {
            if (name === "json") noteBody(ctx, value);
            else if (
              typeof value === "string" &&
              value.length <= MAX_ERROR_BODY_CHARS
            ) {
              noteBody(ctx, JSON.parse(value));
            }
          } catch {
            /* observation only */
          }
          finish();
          return value;
        },
      });
    } catch {
      /* leave the reader alone */
    }
  };
  shadow("json");
  shadow("text");
  // An app that reads neither still gets its call counted — at header time,
  // with no usage, which is exactly what was knowable. An app that HAS begun
  // reading is left alone: its own read files the call, on success or throw.
  try {
    queueMicrotask(() => {
      // Give the app a turn to start reading before assuming it never will.
      setTimeout(() => {
        if (!reading) finish();
      }, 0);
    });
  } catch {
    finish();
  }
}

/* ── Classification ──────────────────────────────────────────────────────── */

/** The page's own origin, used as the base for a relative address and to tell
 *  a cross-origin reply apart from a same-origin one. */
function originHere(): string | null {
  try {
    return pageOriginOf(globalThis);
  } catch {
    return null;
  }
}

/** The address of a `fetch` argument, without reading anything else off it. */
function hrefOf(input: unknown): string | null {
  try {
    if (typeof input === "string") return input;
    if (input && typeof input === "object") {
      const asUrl = input as { href?: unknown; url?: unknown };
      if (typeof asUrl.href === "string") return asUrl.href;
      if (typeof asUrl.url === "string") return asUrl.url;
    }
  } catch {
    /* fall through */
  }
  return null;
}

/**
 * The method a call will be sent with, as the browser resolves it:
 * `init.method` wins, then a Request's own, then GET.
 *
 * Read only to decide whether an unclassified call was AI-SHAPED. Never
 * stored, never uploaded.
 */
function methodOf(input: unknown, init: unknown): string {
  try {
    const fromInit = (init as { method?: unknown } | undefined)?.method;
    if (typeof fromInit === "string" && fromInit) return fromInit.toUpperCase();
    const fromInput = (input as { method?: unknown } | null)?.method;
    if (typeof fromInput === "string" && fromInput) {
      return fromInput.toUpperCase();
    }
    return "GET";
  } catch {
    return "GET";
  }
}

/**
 * Tally an unclassified call whose path looked like an AI inference request.
 *
 * Count only — see {@link AI_SHAPED_PATHS}. Never throws: a page's outbound
 * call is worth nothing to disturb.
 */
function noteUnclassifiedAiShape(href: string | null, method: string): void {
  try {
    if (!href) return;
    // The evidence is a POST to an inference shape. A GET or a HEAD on the
    // same path is a health check or a probe, and counting one would turn the
    // note into a false alarm on a page with no AI in it at all.
    if (method !== "POST") return;
    if (isOwnEndpointReply(href)) return;
    const here = originHere();
    const url = new URL(href, here ?? "http://localhost");
    // Same-origin means the page's OWN back end, which a server kit installed
    // beside it measures properly. Counting it here would report one call
    // twice and would name the app's own server as an unknown AI host.
    if (here && url.origin === here) return;
    const path = url.pathname.toLowerCase().replace(/\/+$/, "");
    if (!path || !AI_SHAPED_PATHS.includes(path)) return;
    unclassifiedAiShaped++;
  } catch {
    /* observation must never break the page */
  }
}

/**
 * The XHR wrapper's way in.
 *
 * `open()` is the one moment an XHR has both its method and its address in
 * hand, and it is where the wrapper already decides whether the call went to
 * a provider it knows. A call it did not recognise comes here instead.
 */
export function noteAiShapedXhr(url: unknown, method: unknown): void {
  if (!feedOn) return;
  noteUnclassifiedAiShape(
    typeof url === "string" ? url : null,
    typeof method === "string" ? method.toUpperCase() : "GET",
  );
}

/** Is this a known AI provider, and if so which one? Reads only the host. */
function providerFor(input: unknown, init: unknown): number {
  try {
    const href = hrefOf(input);
    if (!href) return 0;
    // Our own uploads are never the app's AI traffic. They already go out
    // through the page's ORIGINAL fetch, so they do not reach this meter at
    // all; this is the belt to that pair of braces.
    if (isOwnEndpointReply(href)) return 0;
    const host = new URL(href, originHere() ?? "http://localhost").hostname;
    return aiProviderCodeOrDeclared(host);
  } catch {
    return 0;
  }
  // `init` is deliberately unread here: classification is a hostname question,
  // and the options object is where the authorization header lives.
  void init;
}

/**
 * Is this address a known AI provider's?
 *
 * The one classification the XHR wrapper can make at `open()` time, before the
 * body and the deadline exist. Answers a boolean and keeps nothing.
 */
export function isAiProviderUrl(url: unknown): boolean {
  return providerFor(typeof url === "string" ? url : null, null) !== 0;
}

/** Did this address answer from another origin, where the browser — not the
 *  provider — decides which headers the page may read? */
function isCrossOrigin(href: string | null): boolean {
  try {
    const here = originHere();
    if (!href) return false;
    const target = new URL(href, here ?? "http://localhost").origin;
    if (!here) return true;
    return target !== here;
  } catch {
    return false;
  }
}

/** A `Response` knows its own kind; fall back to comparing origins for the
 *  plain objects a test double hands over. */
function responseCrossOrigin(res: unknown, requested: boolean): boolean {
  try {
    const type = (res as { type?: unknown } | null)?.type;
    // The browser's own verdict, where it gives one: "cors" and "opaque" mean
    // another origin answered and the browser is filtering what we may read.
    if (type === "cors" || type === "opaque" || type === "opaqueredirect") {
      return true;
    }
    // "basic" is the browser saying our own origin answered. "default" is NOT:
    // it is the type a hand-built `Response` carries (a service worker, a
    // cache replay, a test double), which says nothing about where the bytes
    // came from — so that case falls through to the address we dialled.
    if (type === "basic") return false;
    const url = (res as { url?: unknown } | null)?.url;
    if (typeof url === "string" && url) return isCrossOrigin(url);
  } catch {
    /* fall through to what the request said */
  }
  return requested;
}

/* ── The feed (called by callWatch's wrappers) ───────────────────────────── */

let armed = false;
let feedOn = false;

/**
 * Begin watching. Idempotent, and never armed under the kill switch.
 *
 * Called from `installCallWatch`, so the AI reading exists exactly when the
 * outbound watch it rides on exists — and disappears with it.
 */
export function armAiCalls(): void {
  if (armed || isBoosthisDisabled()) return;
  armed = true;
  feedOn = true;
  if (windowStartedAt === 0) windowStartedAt = nowMs();
}

/** Stop watching. The readings already taken stay readable. */
export function unarmAiCalls(): void {
  feedOn = false;
  armed = false;
}

/** Which transports the outbound watch actually managed to wrap. A transport
 *  that refused to be wrapped is a blind spot, not a silence. */
export function setWatchedTransports(fetchOk: boolean, xhrOk: boolean): void {
  watchedFetch = fetchOk;
  watchedXhr = xhrOk;
}

/**
 * One `fetch` is starting. Returns the call in flight, or null when this is
 * not an AI call (which is every call in most apps, and costs one hostname
 * lookup).
 */
export function aiFetchStarted(
  input: unknown,
  init: unknown,
): AiCallCtx | null {
  if (!feedOn) return null;
  try {
    const provider = providerFor(input, init);
    if (provider === 0) {
      // Not a provider we know and not one this page declared. Before the
      // call passes out of sight, ask the one question that costs nothing:
      // did it LOOK like inference? That answer is the difference between a
      // page with no AI and a page whose AI we cannot see.
      noteUnclassifiedAiShape(hrefOf(input), methodOf(input, init));
      return null;
    }
    const opts = (init ?? null) as { signal?: unknown; body?: unknown } | null;
    const startedAt = nowMs();
    return {
      provider,
      turn: turnForStart(startedAt),
      startedAt,
      headersAt: 0,
      firstChunkAt: 0,
      lastChunkAt: 0,
      worstGapMs: 0,
      promptId: promptIdentity(opts?.body),
      // A `Request` object always carries a signal of its own, so only the
      // options form can honestly answer "did this call have a time limit?".
      hadTimeLimit: !!opts && opts.signal != null,
      streamed: false,
      sawTerminator: false,
      usage: null,
      crossOrigin: isCrossOrigin(hrefOf(input)),
      counted: 0,
      filed: false,
    };
  } catch {
    return null;
  }
}

/** The `fetch` answered. */
export function aiFetchSettled(ctx: AiCallCtx | null, res: unknown): void {
  if (!ctx) return;
  try {
    const response = res as Response | null;
    const headers = (response as { headers?: { get?: unknown } } | null)
      ?.headers;
    const get = headers?.get;
    if (typeof get !== "function") {
      fileCall(ctx);
      return;
    }
    const read = (name: string): string | null => {
      const v = (get as (n: string) => unknown).call(headers, name);
      return typeof v === "string" ? v : null;
    };
    ctx.headersAt = nowMs();
    ctx.crossOrigin = responseCrossOrigin(response, ctx.crossOrigin);
    const status = Number((response as { status?: unknown } | null)?.status);
    noteHeadroomFrom(ctx, read, status);
    if (Number.isFinite(status) && status >= 400) {
      noteRefusalStatus(ctx, status);
      if (response && typeof response.clone === "function") {
        classifyRefusal(ctx, response);
      }
      fileCall(ctx);
      return;
    }
    const ctype = (read("content-type") ?? "").toLowerCase();
    ctx.streamed = ctype.includes("event-stream") || ctype.includes("x-ndjson");
    if (ctx.streamed && response) observeStream(ctx, response);
    else if (response) observeBody(ctx, response);
    else fileCall(ctx);
  } catch {
    fileCall(ctx);
  }
}

/** The `fetch` never answered. */
export function aiFetchFailed(ctx: AiCallCtx | null, err: unknown): void {
  if (!ctx) return;
  try {
    noteTransportFailure(err);
  } catch {
    /* never disturb the page */
  }
  fileCall(ctx);
}

/**
 * An `XMLHttpRequest` is being sent.
 *
 * A streamed answer over XHR cannot be observed chunk by chunk without
 * buffering it, so no first-words time is claimed here — the call is measured
 * end to end, and its usage read from the reply the app already has.
 */
export function aiXhrStarted(
  url: unknown,
  body: unknown,
  timeoutMs: unknown,
): AiCallCtx | null {
  if (!feedOn) return null;
  try {
    const href = typeof url === "string" ? url : null;
    const provider = providerFor(href, null);
    if (provider === 0) return null;
    const startedAt = nowMs();
    return {
      provider,
      turn: turnForStart(startedAt),
      startedAt,
      headersAt: 0,
      firstChunkAt: 0,
      lastChunkAt: 0,
      worstGapMs: 0,
      promptId: promptIdentity(body),
      hadTimeLimit: typeof timeoutMs === "number" && timeoutMs > 0,
      streamed: false,
      sawTerminator: true,
      usage: null,
      crossOrigin: isCrossOrigin(href),
      counted: 0,
      filed: false,
    };
  } catch {
    return null;
  }
}

/** The XHR loaded. `xhr` is read through fixed accessors only — the exposed
 *  headers, the status, and the text the app itself already has. */
export function aiXhrSettled(ctx: AiCallCtx | null, xhr: unknown): void {
  if (!ctx) return;
  try {
    const target = xhr as {
      status?: unknown;
      response?: unknown;
      responseText?: unknown;
      responseType?: unknown;
      getResponseHeader?: unknown;
    } | null;
    const getHeader = target?.getResponseHeader;
    const read = (name: string): string | null => {
      try {
        if (typeof getHeader !== "function") return null;
        const v = (getHeader as (n: string) => unknown).call(target, name);
        return typeof v === "string" ? v : null;
      } catch {
        return null;
      }
    };
    ctx.headersAt = nowMs();
    const status = Number(target?.status);
    noteHeadroomFrom(ctx, read, status);
    // What the app already has in hand. Nothing is fetched, cloned or decoded
    // on its behalf: `responseText` is only readable at all when the app chose
    // a text-shaped response, and a longer body than an error is left alone.
    const asJson =
      target?.responseType === "json" && target.response !== null
        ? target.response
        : null;
    const text =
      typeof target?.responseText === "string" ? target.responseText : null;
    if (Number.isFinite(status) && status >= 400) {
      noteRefusalStatus(ctx, status);
      classifyRefusalText(ctx, text);
      fileCall(ctx);
      return;
    }
    if (asJson !== null) noteBody(ctx, asJson);
    else if (text !== null && text.length <= MAX_STREAM_CARRY * 8) {
      try {
        noteBody(ctx, JSON.parse(text));
      } catch {
        /* not JSON — nothing to read */
      }
    }
    fileCall(ctx);
  } catch {
    fileCall(ctx);
  }
}

/** The XHR ended without an answer. */
export function aiXhrFailed(
  ctx: AiCallCtx | null,
  kind: "error" | "abort" | "timeout",
): void {
  if (!ctx) return;
  try {
    failCount++;
    if (kind !== "error") timeoutCount++;
  } catch {
    /* never disturb the page */
  }
  fileCall(ctx);
}

/* ── Blind spots ─────────────────────────────────────────────────────────── */

/**
 * One entry from the browser's own resource timeline.
 *
 * The ONLY use made of the address is the hostname, and the only use made of
 * the hostname is the provider list: an entry to somewhere we do not recognise
 * is dropped without a trace. What survives is a transport NAME from the fixed
 * list above.
 */
export function noteAiResourceEntry(name: unknown, initiator: unknown): void {
  try {
    if (!feedOn) return;
    if (typeof name !== "string" || !name) return;
    const kind = String(initiator ?? "").toLowerCase();
    if (!UNWATCHABLE_INITIATORS.includes(kind)) return;
    // A transport we DID wrap is watched, not blind.
    if (kind === "fetch" && watchedFetch) return;
    if (kind === "xmlhttprequest" && watchedXhr) return;
    if (isOwnEndpointReply(name)) return;
    const host = new URL(name, originHere() ?? "http://localhost").hostname;
    if (aiProviderCodeOrDeclared(host) === 0) return;
    if (unwatchedTransports.size >= MAX_UNWATCHED_TRANSPORTS) return;
    unwatchedTransports.add(kind);
  } catch {
    /* observation must never break the page */
  }
}

/**
 * Transports that carried a call to a known provider past this meter.
 *
 * Evidence, never a guess: each one was seen in the page's own resource
 * timeline, addressed to a provider host, on a transport this kit does not
 * wrap. `0` means nothing of the sort was seen — which is not the same as
 * proof that nothing happened, because a call issued inside a Worker never
 * appears on the document's timeline at all.
 */
export function unwatchedAiClientCount(): number {
  return unwatchedTransports.size;
}

/* ── Readers ─────────────────────────────────────────────────────────────── */

function percentile(ring: readonly number[], p: number): number {
  if (ring.length === 0) return 0;
  const sorted = [...ring].sort((a, b) => a - b);
  const idx = Math.min(
    sorted.length - 1,
    Math.max(0, Math.ceil((p / 100) * sorted.length) - 1),
  );
  return Math.round(sorted[idx]!);
}

/** The label-free aggregate the scorers consume. Numbers only. */
export function getAiCallStats(): AiCallStatsLike {
  let topProvider = 0;
  let topCalls = 0;
  for (const [code, n] of callsByProvider) {
    if (n > topCalls) {
      topCalls = n;
      topProvider = code;
    }
  }
  return {
    watchedRequests,
    watchedRequestMs: Math.round(watchedRequestMs),
    aiMs: Math.round(aiMs),
    callCount,
    providerCount: providersSeen.size,
    topProvider,
    p75Ms: percentile(durations, 75),
    worstMs: durations.length ? Math.round(Math.max(...durations)) : 0,
    streamCount,
    ttftP75Ms: percentile(ttfts, 75),
    stallCount,
    failCount,
    rateLimitedCount,
    quotaCount,
    timeoutCount,
    truncatedCount,
    filteredCount,
    serverMsP75:
      serverMsCalls > 0 ? Math.round(serverMsTotal / serverMsCalls) : 0,
    serverMsCalls,
    unwatchedClients: unwatchedAiClientCount(),
    unclassifiedCalls: unclassifiedAiShaped,
    tokensIn,
    tokensOut,
    cachedIn,
    costMicros,
    reportedCostCalls,
    pricedCalls,
    unpricedCalls,
    worstRequestCostMicros,
    windowMs: windowStartedAt > 0 ? Math.max(0, nowMs() - windowStartedAt) : 0,
    usageMissingCalls,
    streamUsageMissingCalls,
    declaredCalls,
    declaredUnpricedCalls,
    promptRepeatWorst,
    promptRepeatRequests,
    serialWorst,
    serialRequests,
    noTimeLimitCalls,
    retryNoBackoffCount,
    headroomReads,
    headersHiddenCalls,
    worstRequestsPct,
    worstTokensPct,
    worstRetryAfterMs,
  };
}

/** Reset everything and stop watching. Test + kill-switch path. */
export function clearAiCalls(): void {
  unarmAiCalls();
  watchedRequests = 0;
  watchedRequestMs = 0;
  aiMs = 0;
  callCount = 0;
  streamCount = 0;
  stallCount = 0;
  failCount = 0;
  rateLimitedCount = 0;
  quotaCount = 0;
  timeoutCount = 0;
  truncatedCount = 0;
  filteredCount = 0;
  noTimeLimitCalls = 0;
  retryNoBackoffCount = 0;
  tokensIn = 0;
  tokensOut = 0;
  cachedIn = 0;
  costMicros = 0;
  reportedCostCalls = 0;
  pricedCalls = 0;
  unpricedCalls = 0;
  worstRequestCostMicros = 0;
  usageMissingCalls = 0;
  streamUsageMissingCalls = 0;
  declaredCalls = 0;
  declaredUnpricedCalls = 0;
  promptRepeatWorst = 0;
  promptRepeatRequests = 0;
  serialWorst = 0;
  serialRequests = 0;
  headroomReads = 0;
  headersHiddenCalls = 0;
  worstRequestsPct = null;
  worstTokensPct = null;
  worstRetryAfterMs = 0;
  serverMsTotal = 0;
  serverMsCalls = 0;
  unclassifiedAiShaped = 0;
  providersSeen.clear();
  callsByProvider.clear();
  lastRefusal.clear();
  unwatchedTransports.clear();
  watchedFetch = false;
  watchedXhr = false;
  durations.length = 0;
  ttfts.length = 0;
  windowStartedAt = 0;
  currentTurn = null;
  inputSeq = 0;
  inputAt = 0;
}

/* ── Test seams ──────────────────────────────────────────────────────────── */

export const __aiCallsInternals = {
  promptIdentity,
  providerFor,
  isArmed: () => armed,
  providerCount: () => AI_PROVIDERS.length,
  turnCount: () => watchedRequests,
  /** File one already-measured call without going through a wrapper. Used by
   *  the tests and the parity fixtures; never on a live path. */
  fileSynthetic(ctx: Partial<AiCallCtx> & { provider: number }): void {
    const startedAt = ctx.startedAt ?? nowMs();
    fileCall({
      provider: ctx.provider,
      turn: ctx.turn ?? turnForStart(startedAt),
      startedAt,
      headersAt: ctx.headersAt ?? 0,
      firstChunkAt: ctx.firstChunkAt ?? 0,
      lastChunkAt: ctx.lastChunkAt ?? 0,
      worstGapMs: ctx.worstGapMs ?? 0,
      promptId: ctx.promptId ?? 0,
      hadTimeLimit: ctx.hadTimeLimit ?? true,
      streamed: ctx.streamed ?? false,
      sawTerminator: ctx.sawTerminator ?? true,
      usage: ctx.usage ?? null,
      crossOrigin: ctx.crossOrigin ?? false,
      counted: 0,
      filed: false,
    });
  },
};
