/* ─── Boosthis: what may be read out of an AI provider's reply (Web) ───────
 *
 * THE WHOLE PRIVACY PROMISE OF THIS METER LIVES IN THIS FILE. Everything the
 * kit learns about an AI call's cost and shape is read HERE, from a narrow,
 * named set of fields, and the restriction is meant to be provable rather than
 * promised: every reader below takes an already-parsed reply (or one response
 * header) and returns NUMBERS. There is no code path in this module that can
 * return, store, log or forward a prompt, a completion, a message, a tool
 * argument, a system instruction, an address or a credential.
 *
 * WHAT IS READ, AND NOTHING ELSE:
 *   • token counts — in, out, and the ones the provider served from its own
 *     prompt cache (the difference between an expensive app and a cheap one);
 *   • a cost the provider itself put in the reply, where it does;
 *   • the reply's model identifier, compared against a closed price table and
 *     dropped — never stored and never uploaded;
 *   • a finish/stop/error CODE, compared against a closed list so a refusal
 *     can be told apart from a truncation; the error MESSAGE is never read;
 *   • rate-limit and service-timing values from a closed list of header names.
 *
 * WHAT IS NEVER READ: any field not named in the tables below. In particular
 * `choices[].message.content`, `content[].text`, `candidates[].content`,
 * `input`, `messages`, `system`, `tools`, `prompt`, and every error message
 * string. A streamed reply's content deltas are not even parsed — see
 * {@link streamLineIsInteresting}.
 *
 * PARITY: a copy of `lib/boosthis-runtime-node/src/aiUsage.ts` (the Python kit
 * ships the same tables in `boosthis/ai_usage.py`), kept identical below this
 * docblock. Copied rather than imported because a kit ships as a standalone
 * bundle with no runtime dependencies — and because the privacy boundary must
 * be readable in ONE file per kit.
 */

/* ── Token usage ─────────────────────────────────────────────────────────── */

/**
 * The named numeric fields this kit may read out of a reply, by path.
 *
 * Each entry is a path of plain object keys. Anything not listed here is
 * unreachable: {@link readNumberAt} walks ONLY these paths and returns a
 * finite number or null.
 */
export const AI_USAGE_PATHS = {
  /** Tokens the app sent. */
  tokensIn: [
    ["usage", "prompt_tokens"], // OpenAI chat/completions, Groq, Together, …
    ["usage", "input_tokens"], // OpenAI responses API, Anthropic
    ["usageMetadata", "promptTokenCount"], // Google
    ["meta", "billed_units", "input_tokens"], // Cohere
  ],
  /** Tokens the model produced. */
  tokensOut: [
    ["usage", "completion_tokens"],
    ["usage", "output_tokens"],
    ["usageMetadata", "candidatesTokenCount"],
    ["meta", "billed_units", "output_tokens"],
  ],
  /** Of the input tokens, the ones served from the provider's prompt cache. */
  cachedIn: [
    ["usage", "prompt_tokens_details", "cached_tokens"], // OpenAI chat
    ["usage", "input_tokens_details", "cached_tokens"], // OpenAI responses
    ["usage", "cache_read_input_tokens"], // Anthropic
    ["usageMetadata", "cachedContentTokenCount"], // Google
  ],
  /** A cost the provider put in the reply itself, in US dollars. */
  costUsd: [
    ["usage", "cost"], // OpenRouter
    ["usage", "total_cost"], // OpenRouter (older shape)
  ],
} as const;

/** Model identifiers, read only to look up a price and then dropped. */
const AI_MODEL_PATHS: ReadonlyArray<readonly string[]> = [
  ["model"],
  ["modelVersion"],
  ["message", "model"], // Anthropic streamed `message_start`
];

/** Longest a model identifier may be before the price lookup gives up. */
const MAX_MODEL_CHARS = 64;

/**
 * Walk ONE named path and return a finite number, or null.
 *
 * The only reader in this module that touches a reply body. It refuses
 * anything that is not a finite number, so a string field at a listed path
 * (a model name, an id, an error message) can never come back through it.
 */
function readNumberAt(body: unknown, path: readonly string[]): number | null {
  try {
    let cur: unknown = body;
    for (const key of path) {
      if (cur === null || typeof cur !== "object" || Array.isArray(cur)) {
        return null;
      }
      cur = (cur as Record<string, unknown>)[key];
    }
    return typeof cur === "number" && Number.isFinite(cur) ? cur : null;
  } catch {
    return null;
  }
}

/** First non-null value across a set of alternative paths. */
function firstNumber(
  body: unknown,
  paths: ReadonlyArray<readonly string[]>,
): number | null {
  for (const p of paths) {
    const v = readNumberAt(body, p);
    if (v !== null) return v;
  }
  return null;
}

/** What one reply told us about its own usage. Numbers only, by construction. */
export interface AiUsageRead {
  /** 1 when the reply reported ANY token count at all. */
  reported: 0 | 1;
  tokensIn: number;
  tokensOut: number;
  cachedIn: number;
  /** Micro-USD the provider itself reported, or null when it reported none. */
  reportedCostMicros: number | null;
  /** Price-table position for the model, or 0 when it is not in the table. */
  priceCode: number;
}

/** An empty read — used for a reply that reported nothing. */
export const NO_USAGE: AiUsageRead = {
  reported: 0,
  tokensIn: 0,
  tokensOut: 0,
  cachedIn: 0,
  reportedCostMicros: null,
  priceCode: 0,
};

/**
 * Read the usage numbers out of one parsed reply.
 *
 * Total and side-effect free. Returns {@link NO_USAGE} for anything that is
 * not an object or reports nothing — never a partial guess.
 */
export function readAiUsage(body: unknown): AiUsageRead {
  try {
    if (body === null || typeof body !== "object") return NO_USAGE;
    const tokensIn = firstNumber(body, AI_USAGE_PATHS.tokensIn);
    const tokensOut = firstNumber(body, AI_USAGE_PATHS.tokensOut);
    const cachedIn = firstNumber(body, AI_USAGE_PATHS.cachedIn);
    const costUsd = firstNumber(body, AI_USAGE_PATHS.costUsd);
    if (tokensIn === null && tokensOut === null && costUsd === null) {
      return NO_USAGE;
    }
    return {
      reported: 1,
      tokensIn: Math.max(0, Math.round(tokensIn ?? 0)),
      tokensOut: Math.max(0, Math.round(tokensOut ?? 0)),
      cachedIn: Math.max(0, Math.round(cachedIn ?? 0)),
      reportedCostMicros:
        costUsd !== null && costUsd >= 0
          ? Math.round(costUsd * 1_000_000)
          : null,
      priceCode: priceCodeForBody(body),
    };
  } catch {
    return NO_USAGE;
  }
}

/* ── Price table ─────────────────────────────────────────────────────────── */

/**
 * The day the prices below were published, as a plain calendar date.
 *
 * It ships WITH the numbers and reaches the dashboard as a day count, because
 * an estimate whose age nobody can see is how a stale table quietly starts
 * lying about money. A cost the provider reported itself always wins over
 * anything derived from this table.
 */
export const AI_PRICE_AS_OF = "2026-08-01" as const;

/** {@link AI_PRICE_AS_OF} as whole days since the epoch — the wire form. */
export const AI_PRICE_TABLE_DAY = Math.floor(
  Date.parse(`${AI_PRICE_AS_OF}T00:00:00Z`) / 86_400_000,
);

interface ModelPrice {
  /** Lower-case identifier prefix. Longest match wins. */
  readonly prefix: string;
  /** US dollars per million input tokens. */
  readonly inPerM: number;
  /** US dollars per million output tokens. */
  readonly outPerM: number;
  /** US dollars per million input tokens served from the provider's cache. */
  readonly cachedPerM: number;
}

/**
 * Published list prices, per million tokens, as of {@link AI_PRICE_AS_OF}.
 *
 * APPEND ONLY, like the provider table — a model's position is its wire code,
 * and `0` means "this kit has no price for that model", which is reported as
 * an unpriced call rather than a free one.
 */
export const AI_MODEL_PRICES: readonly ModelPrice[] = [
  { prefix: "gpt-4o-mini", inPerM: 0.15, outPerM: 0.6, cachedPerM: 0.075 },
  { prefix: "gpt-4o", inPerM: 2.5, outPerM: 10, cachedPerM: 1.25 },
  { prefix: "gpt-4.1-nano", inPerM: 0.1, outPerM: 0.4, cachedPerM: 0.025 },
  { prefix: "gpt-4.1-mini", inPerM: 0.4, outPerM: 1.6, cachedPerM: 0.1 },
  { prefix: "gpt-4.1", inPerM: 2, outPerM: 8, cachedPerM: 0.5 },
  { prefix: "gpt-5-mini", inPerM: 0.25, outPerM: 2, cachedPerM: 0.025 },
  { prefix: "gpt-5-nano", inPerM: 0.05, outPerM: 0.4, cachedPerM: 0.005 },
  { prefix: "gpt-5", inPerM: 1.25, outPerM: 10, cachedPerM: 0.125 },
  { prefix: "o4-mini", inPerM: 1.1, outPerM: 4.4, cachedPerM: 0.275 },
  { prefix: "o3-mini", inPerM: 1.1, outPerM: 4.4, cachedPerM: 0.55 },
  { prefix: "o3", inPerM: 2, outPerM: 8, cachedPerM: 0.5 },
  { prefix: "claude-3-5-haiku", inPerM: 0.8, outPerM: 4, cachedPerM: 0.08 },
  { prefix: "claude-3-5-sonnet", inPerM: 3, outPerM: 15, cachedPerM: 0.3 },
  { prefix: "claude-haiku-4", inPerM: 1, outPerM: 5, cachedPerM: 0.1 },
  { prefix: "claude-sonnet-4", inPerM: 3, outPerM: 15, cachedPerM: 0.3 },
  { prefix: "claude-opus-4", inPerM: 15, outPerM: 75, cachedPerM: 1.5 },
  { prefix: "gemini-2.5-pro", inPerM: 1.25, outPerM: 10, cachedPerM: 0.31 },
  { prefix: "gemini-2.5-flash", inPerM: 0.3, outPerM: 2.5, cachedPerM: 0.075 },
  { prefix: "gemini-2.0-flash", inPerM: 0.1, outPerM: 0.4, cachedPerM: 0.025 },
  { prefix: "deepseek-chat", inPerM: 0.27, outPerM: 1.1, cachedPerM: 0.07 },
  { prefix: "mistral-large", inPerM: 2, outPerM: 6, cachedPerM: 2 },
  { prefix: "mistral-small", inPerM: 0.2, outPerM: 0.6, cachedPerM: 0.2 },
] as const;

/**
 * Match the reply's model identifier against the closed price table.
 *
 * The identifier is bounded, lower-cased, compared, and DROPPED. It is never
 * stored on an object that outlives this call, never logged and never
 * uploaded — only the resulting table position leaves this function. That is
 * the same discipline the provider classifier uses on a hostname.
 */
function priceCodeForBody(body: unknown): number {
  try {
    let raw: string | null = null;
    for (const path of AI_MODEL_PATHS) {
      let cur: unknown = body;
      let ok = true;
      for (const key of path) {
        if (cur === null || typeof cur !== "object" || Array.isArray(cur)) {
          ok = false;
          break;
        }
        cur = (cur as Record<string, unknown>)[key];
      }
      if (ok && typeof cur === "string" && cur) {
        raw = cur;
        break;
      }
    }
    if (!raw) return 0;
    const model = raw.slice(0, MAX_MODEL_CHARS).toLowerCase();
    let bestIdx = 0;
    let bestLen = 0;
    for (let i = 0; i < AI_MODEL_PRICES.length; i++) {
      const p = AI_MODEL_PRICES[i]!.prefix;
      // A vendor prefix ("openai/gpt-4o", "models/gemini-2.5-pro") is common;
      // an inclusion test handles it without listing every vendor spelling.
      if (model.includes(p) && p.length > bestLen) {
        bestLen = p.length;
        bestIdx = i + 1;
      }
    }
    return bestIdx;
  } catch {
    return 0;
  }
}

/**
 * Cost in micro-USD for one call, from the price table.
 *
 * Returns null when the model is not in the table — an unpriced call is
 * reported as unpriced, never as free.
 */
export function estimateCostMicros(read: AiUsageRead): number | null {
  try {
    if (read.priceCode <= 0) return null;
    const p = AI_MODEL_PRICES[read.priceCode - 1];
    if (!p) return null;
    const cached = Math.min(read.cachedIn, read.tokensIn);
    const fresh = Math.max(0, read.tokensIn - cached);
    const usd =
      (fresh * p.inPerM + cached * p.cachedPerM + read.tokensOut * p.outPerM) /
      1_000_000;
    if (!Number.isFinite(usd) || usd < 0) return null;
    return Math.round(usd * 1_000_000);
  } catch {
    return null;
  }
}

/* ── How the answer ended ────────────────────────────────────────────────── */

/** Closed list: the reply says the answer was cut off before it finished. */
const TRUNCATED_CODES: ReadonlySet<string> = new Set([
  "length",
  "max_tokens",
  "MAX_TOKENS",
  "model_length",
]);

/** Closed list: the provider refused on content grounds. */
const FILTERED_CODES: ReadonlySet<string> = new Set([
  "content_filter",
  "refusal",
  "SAFETY",
  "BLOCKLIST",
  "PROHIBITED_CONTENT",
  "SPII",
  "IMAGE_SAFETY",
  "RECITATION",
]);

/** Closed list: the refusal is about money or a hard account limit, not rate. */
const QUOTA_CODES: ReadonlySet<string> = new Set([
  "insufficient_quota",
  "billing_hard_limit_reached",
  "billing_not_active",
  "quota_exceeded",
  "credit_limit_exceeded",
  "RESOURCE_EXHAUSTED",
]);

/** Closed list: the refusal is a rate limit. */
const RATE_CODES: ReadonlySet<string> = new Set([
  "rate_limit_error",
  "rate_limit_exceeded",
  "requests",
  "tokens",
  "overloaded_error",
]);

/**
 * Closed list: the provider says the answer ended ON PURPOSE.
 *
 * Not every provider closes a stream with an event of its own. The
 * OpenAI-shaped APIs send `[DONE]`, Anthropic sends `message_stop` and the
 * Responses API sends `response.completed` — but Gemini/Vertex simply sends a
 * last chunk carrying `finishReason: "STOP"` and then closes the connection.
 * Without this list that perfectly ordinary answer looked like a stream that
 * died halfway, and every Gemini call would have been reported as cut off.
 */
const CLEAN_FINISH_CODES: ReadonlySet<string> = new Set([
  "stop", // OpenAI-shaped (OpenAI, Mistral, Groq, DeepSeek, xAI, Together)
  "tool_calls", // OpenAI-shaped: ended to call a tool
  "function_call", // OpenAI-shaped (legacy)
  "STOP", // Gemini / Vertex
  "end_turn", // Anthropic
  "stop_sequence", // Anthropic
  "tool_use", // Anthropic
  "COMPLETE", // Cohere
  "STOP_SEQUENCE", // Cohere
  "TOOL_CALL", // Cohere
]);

/** Paths at which a provider puts a finish/stop/error CODE. Never a message. */
const OUTCOME_CODE_PATHS: ReadonlyArray<readonly string[]> = [
  ["choices", "0", "finish_reason"],
  ["stop_reason"],
  ["delta", "stop_reason"],
  ["candidates", "0", "finishReason"],
  ["promptFeedback", "blockReason"],
  ["error", "code"],
  ["error", "type"],
  ["error", "status"],
  ["type"], // Anthropic streamed `error` envelope
];

/** How a reply ended, as four independent counters' worth of truth. */
export interface AiOutcomeRead {
  truncated: 0 | 1;
  filtered: 0 | 1;
  quota: 0 | 1;
  rateLimited: 0 | 1;
  /**
   * The provider NAMED how this reply ended — cleanly, at a token cap, or on a
   * filter. A stream that carries one of those codes did not simply stop being
   * sent, whether or not the provider also has a terminal event of its own.
   */
  ended: 0 | 1;
}

export const NO_OUTCOME: AiOutcomeRead = {
  truncated: 0,
  filtered: 0,
  quota: 0,
  rateLimited: 0,
  ended: 0,
};

/**
 * Classify how a reply ended, using ONLY codes from the closed lists above.
 *
 * A code that is not on a list contributes nothing: the call is simply an
 * ordinary one. No message, reason text or detail string is read — the strings
 * that reach `has()` are compared to a fixed set and dropped.
 */
export function readAiOutcome(body: unknown): AiOutcomeRead {
  try {
    if (body === null || typeof body !== "object") return NO_OUTCOME;
    let truncated = 0;
    let filtered = 0;
    let quota = 0;
    let rateLimited = 0;
    let ended = 0;
    for (const path of OUTCOME_CODE_PATHS) {
      let cur: unknown = body;
      let ok = true;
      for (const key of path) {
        if (cur === null || typeof cur !== "object") {
          ok = false;
          break;
        }
        cur = Array.isArray(cur)
          ? (cur as unknown[])[Number(key)]
          : (cur as Record<string, unknown>)[key];
      }
      if (!ok || typeof cur !== "string" || !cur) continue;
      // Bounded before comparison: a set lookup on an unbounded customer
      // string is the one way a long value could cost real time here.
      const code = cur.slice(0, 64);
      if (TRUNCATED_CODES.has(code)) truncated = 1;
      if (FILTERED_CODES.has(code)) filtered = 1;
      if (QUOTA_CODES.has(code)) quota = 1;
      if (RATE_CODES.has(code)) rateLimited = 1;
      if (
        CLEAN_FINISH_CODES.has(code) ||
        TRUNCATED_CODES.has(code) ||
        FILTERED_CODES.has(code)
      ) {
        ended = 1;
      }
    }
    return {
      truncated: truncated as 0 | 1,
      filtered: filtered as 0 | 1,
      quota: quota as 0 | 1,
      rateLimited: rateLimited as 0 | 1,
      ended: ended as 0 | 1,
    };
  } catch {
    return NO_OUTCOME;
  }
}

/* ── Rate-limit headroom + service-side timing (headers only) ────────────── */

/**
 * The only response headers this kit reads. Every one of them is a number or
 * a duration the provider publishes about its own limits; none is a
 * credential, an address or anything the customer wrote.
 */
export const AI_HEADER_NAMES = {
  requestsRemaining: [
    "x-ratelimit-remaining-requests",
    "anthropic-ratelimit-requests-remaining",
  ],
  requestsLimit: [
    "x-ratelimit-limit-requests",
    "anthropic-ratelimit-requests-limit",
  ],
  tokensRemaining: [
    "x-ratelimit-remaining-tokens",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-input-tokens-remaining",
  ],
  tokensLimit: [
    "x-ratelimit-limit-tokens",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-input-tokens-limit",
  ],
  retryAfter: ["retry-after", "x-ratelimit-reset-requests"],
  /** One major provider reports its own service-side processing time. */
  serverMs: ["openai-processing-ms", "x-envoy-upstream-service-time"],
} as const;

export interface AiHeaderRead {
  requestsRemaining: number | null;
  requestsLimit: number | null;
  tokensRemaining: number | null;
  tokensLimit: number | null;
  retryAfterMs: number | null;
  serverMs: number | null;
}

export const NO_HEADERS: AiHeaderRead = {
  requestsRemaining: null,
  requestsLimit: null,
  tokensRemaining: null,
  tokensLimit: null,
  retryAfterMs: null,
  serverMs: null,
};

/** A provider writes `1s`, `6m0s`, `250ms` or a plain seconds count. */
function parseDurationMs(raw: string): number | null {
  const s = raw.trim().toLowerCase();
  if (!s) return null;
  if (/^\d+(\.\d+)?$/.test(s)) {
    // Bare number: `retry-after` is seconds by HTTP's own definition.
    const n = Number(s);
    return Number.isFinite(n) ? Math.round(n * 1000) : null;
  }
  const m = s.match(/^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$/);
  if (!m || !m.slice(1).some(Boolean)) return null;
  const ms =
    Number(m[1] ?? 0) * 3_600_000 +
    Number(m[2] ?? 0) * 60_000 +
    Number(m[3] ?? 0) * 1000 +
    Number(m[4] ?? 0);
  return Number.isFinite(ms) ? Math.round(ms) : null;
}

/**
 * Read the rate-limit headroom a provider published about this reply.
 *
 * `get` is a plain header lookup the caller supplies (the Headers object is
 * never handed to this module). Only the names above are ever asked for, so
 * an `authorization`, `set-cookie` or `openai-organization` header cannot be
 * reached through this function at all.
 */
export function readAiHeaders(
  get: (name: string) => string | null | undefined,
): AiHeaderRead {
  const num = (names: readonly string[]): number | null => {
    for (const n of names) {
      try {
        const raw = get(n);
        if (typeof raw !== "string" || !raw.trim()) continue;
        const v = Number(raw.trim());
        if (Number.isFinite(v) && v >= 0) return v;
      } catch {
        /* a header lookup must never disturb the host */
      }
    }
    return null;
  };
  const dur = (names: readonly string[]): number | null => {
    for (const n of names) {
      try {
        const raw = get(n);
        if (typeof raw !== "string" || !raw.trim()) continue;
        const v = parseDurationMs(raw);
        if (v !== null && v >= 0) return v;
      } catch {
        /* as above */
      }
    }
    return null;
  };
  return {
    requestsRemaining: num(AI_HEADER_NAMES.requestsRemaining),
    requestsLimit: num(AI_HEADER_NAMES.requestsLimit),
    tokensRemaining: num(AI_HEADER_NAMES.tokensRemaining),
    tokensLimit: num(AI_HEADER_NAMES.tokensLimit),
    retryAfterMs: dur(AI_HEADER_NAMES.retryAfter),
    serverMs: num(AI_HEADER_NAMES.serverMs),
  };
}

/* ── Streamed replies ────────────────────────────────────────────────────── */

/**
 * Markers that make a streamed line worth parsing AT ALL.
 *
 * This is the reason a streamed answer's text never reaches a JSON parser: a
 * content delta carries none of these substrings, so it is skipped without
 * being decoded into an object, without being inspected and without being
 * kept. Only a line that announces usage or an ending is parsed, and even then
 * only through {@link readAiUsage} / {@link readAiOutcome}.
 */
const STREAM_MARKERS: readonly string[] = [
  '"usage"',
  '"usageMetadata"',
  '"finish_reason"',
  '"stop_reason"',
  '"finishReason"',
  '"billed_units"',
  '"blockReason"',
  '"error"',
];

/** Does this streamed line announce usage or an ending? */
export function streamLineIsInteresting(line: string): boolean {
  for (const marker of STREAM_MARKERS) {
    if (line.includes(marker)) return true;
  }
  return false;
}

/** Terminal markers that mean the provider finished the stream on purpose. */
const STREAM_TERMINATORS: readonly string[] = [
  "[DONE]",
  '"message_stop"',
  '"response.completed"',
];

/** Did this streamed line say the stream ended cleanly? */
export function streamLineIsTerminal(line: string): boolean {
  for (const marker of STREAM_TERMINATORS) {
    if (line.includes(marker)) return true;
  }
  return false;
}
