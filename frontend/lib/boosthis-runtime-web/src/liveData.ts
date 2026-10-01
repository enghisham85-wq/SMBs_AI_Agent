/** Live perf reader for the web MCP server (read side).
 *
 * Reduced-scope port of `lib/boosthis-runtime-node/src/liveData.ts`: the web
 * runtime's MCP surface ships the snapshot reader plus the crash-risk and
 * full-stack-trace readers (parity with Node/Python/RN). The route-level
 * readers (session_summary / recent_samples / budgets / what_next) remain
 * unported: web measures inside the BROWSER while this reader runs Node-side,
 * so there is no in-process sample buffer to summarize — the snapshot IS the
 * web live view.
 *
 * SECURITY — why config is injected, never read from env here:
 * per-call creds are threaded through `resolveCreds` as a PARAMETER and NEVER
 * written to module state, so one caller's creds can never bleed into the
 * next. Only the stdio entrypoint (`mcp-stdio.ts`) reads env and calls
 * `configureLiveData`.
 *
 * Fail-open + fail-soft: any error (not configured, offline, non-200, bad
 * JSON, rate limited, kill-switch) yields `null` so the caller serves the
 * on-device note. Returned payloads are additionally run through the PII
 * guard (defense in depth) and dropped rather than thrown on a trip.
 */

import { unwatchedFetch } from "./callWatch";
import { checkNoPII } from "./no-pii";
import { isBoosthisDisabled } from "./runtimeFlags";

interface LiveDataConfig {
  installId: string;
  token: string;
  baseUrl: string;
}

let config: LiveDataConfig | null = null;

/** Per-call read credentials. Passed as a PARAMETER to the getLive* readers so
 *  a multi-tenant caller can read ONE install per request WITHOUT ever
 *  mutating module state. The stdio (single-tenant) entrypoint uses
 *  `configureLiveData`. */
export interface LiveCreds {
  installId: string;
  token: string;
  baseUrl?: string;
}

/** Normalize raw creds into a usable config, or null if installId/token blank. */
function normalizeCreds(c: {
  installId?: string;
  token?: string;
  baseUrl?: string;
}): LiveDataConfig | null {
  const installId = (c.installId ?? "").trim();
  const token = (c.token ?? "").trim();
  if (!installId || !token) return null;
  // `||` (not `??`) so a blank var never shadows the fallback.
  const base =
    (c.baseUrl && c.baseUrl.trim()) || "https://www.boosthis.com/api";
  return { installId, token, baseUrl: base.replace(/\/+$/, "") };
}

/** Resolve the config for ONE call: an explicit per-call override wins;
 *  otherwise fall back to the injected module config (stdio path). NEVER
 *  writes module state, so per-call creds can never leak between requests. */
function resolveCreds(override?: LiveCreds): LiveDataConfig | null {
  if (override) return normalizeCreds(override);
  return config;
}

/** Inject the read credentials for ONE install. Called only by the stdio
 *  entrypoint. Passing an empty install id or token clears the config (live
 *  data stays off and the tools serve the on-device note). */
export function configureLiveData(opts: {
  installId?: string;
  token?: string;
  baseUrl?: string;
}): void {
  config = normalizeCreds(opts);
}

export function isLiveDataConfigured(): boolean {
  return config !== null;
}

/** Test-only: clear the injected config so each test starts hermetic. */
export function _resetLiveDataForTests(): void {
  config = null;
}

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

/** GET a JSON body from the configured server with the install bearer token.
 *  Never throws; returns null on any failure. */
async function fetchJson(
  path: string,
  cfg: LiveDataConfig,
): Promise<unknown | null> {
  if (isBoosthisDisabled()) return null;
  try {
    const url = `${cfg.baseUrl}${path}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    try {
      const res = await (unwatchedFetch() ?? fetch)(url, {
        method: "GET",
        headers: {
          accept: "application/json",
          authorization: `Bearer ${cfg.token}`,
        },
        signal: controller.signal,
      });
      if (!res.ok) return null;
      const body = await res.text();
      if (body.length > 2_000_000) return null;
      return JSON.parse(body);
    } finally {
      clearTimeout(timer);
    }
  } catch {
    return null;
  }
}

/** Defense in depth: keep a payload only if it passes the PII guard. */
function piiSafe<T>(row: T): boolean {
  return checkNoPII(row) === null;
}

const NOTE_SNAPSHOT =
  "Live full-meter snapshot read from your Boosthis server (per-page rows, " +
  "axes, summary).";

/** Read the install's latest uploaded perf snapshot from the server. Returns
 *  null (→ the on-device note) on any failure. The `note`/`source` fields are
 *  shipped constants, not collected data, so they are added AFTER the guard
 *  pass over the server payload. */
export async function getLiveSnapshot(
  creds?: LiveCreds,
): Promise<unknown | null> {
  const cfg = resolveCreds(creds);
  if (!cfg) return null;
  const snap = await fetchJson(
    `/snapshot?installId=${encodeURIComponent(cfg.installId)}`,
    cfg,
  );
  if (snap === null || typeof snap !== "object" || Array.isArray(snap)) {
    return null;
  }
  if (!piiSafe(snap)) return null;
  return {
    available: true,
    source: "boosthis-server",
    snapshot: snap,
    note: NOTE_SNAPSHOT,
  };
}

// ── Potential-crashes risk feed ────────────────────────────────────────────
// The READ side of the "what is likely to crash my app" channel. The web
// runtime already records each crash class the page has actually hit
// (uncaught error, unhandled promise rejection, caught render near-miss) as a
// privacy-safe signature — error name, a redacted top frame, a count bucket.
// GET /crashes/risk joins those with the main-thread Stability summary from
// the latest snapshot; this reader fetches that joined view, fail-opens to
// null (caller serves the on-device note), and attaches STATIC rule-id
// pointers so the AI knows which web-pack rules to fetch with
// boosthis.get_rule.

const NOTE_CRASH_RISK =
  "Potential-crashes risk feed read from your Boosthis server: the crash " +
  "classes your page has ALREADY recorded (uncaught errors, unhandled promise " +
  "rejections, and caught render near-misses), newest-first, joined with the " +
  "main-thread (long-task) Stability summary from your latest snapshot. Each " +
  "crash class carries relatedRules — fetch them with boosthis.get_rule for the " +
  "fix. Signatures are code-derived (error name + redacted frame), never the " +
  "raw error message, so no user value is exposed.";

// Static, shipped map from a crash KIND to the web-pack rule ids most likely
// to prevent it. Deterministic (no server round-trip): the matcher/rule book
// holds the fix text; this only points the AI at the right rules for each
// crash class.
//
// NOTE: the Node runtime's crash-rule ids (unbounded-json-body,
// fetch-no-timeout-node, …) do NOT exist in the 58-rule web pack, so these are
// the nearest analogous web-pack ids (see checklist.ts): runaway regexes,
// storming network retries, and leaked SPA listeners are the browser-side
// crash/stability analogues.
const DEFAULT_CRASH_RULES = [
  "web-regex-redos",
  "fetch-retry-storm",
  "listener-leak-spa",
];
const CRASH_KIND_RULES: Record<string, string[]> = {
  uncaught: DEFAULT_CRASH_RULES,
  unhandledRejection: [
    "fetch-retry-storm",
    "client-fetch-waterfall",
    "websocket-reconnect-storm",
  ],
  render: [
    "excessive-rerender-storm",
    "hydration-mismatch-rework",
    "dom-size-explosion",
  ],
};

// Rules that address main-thread hangs / frozen-page stability (the
// snapshot's Stability axis), independent of any single crash class.
const STABILITY_RULES = [
  "long-tasks-block-input",
  "no-error-budget-vitals",
  "animation-triggers-layout",
];

interface CrashRiskItem {
  signature?: string;
  errorName?: string;
  kind?: string;
  redactedFrame?: string;
  countBucket?: string;
  occurrences?: number;
  firstSeenAt?: string;
  lastSeenAt?: string;
}

/** The install's already-recorded crash classes + Stability summary, with rule
 *  pointers. Fail-open: any failure (not configured, offline, non-200, bad JSON,
 *  PII trip) returns null so the caller serves the on-device note. NEVER mutates
 *  module state — per-call creds are threaded through resolveCreds only. */
export async function getLiveCrashRisk(
  creds?: LiveCreds,
): Promise<unknown | null> {
  const cfg = resolveCreds(creds);
  if (!cfg) return null;
  const data = (await fetchJson(
    `/crashes/risk?installId=${encodeURIComponent(cfg.installId)}`,
    cfg,
  )) as {
    crashClasses?: number;
    totalOccurrences?: number;
    crashes?: CrashRiskItem[];
    stability?: Record<string, unknown>;
  } | null;
  if (!data) return null;

  const crashesIn = Array.isArray(data.crashes) ? data.crashes : [];
  const crashes = crashesIn
    .map((c) => {
      const kind = typeof c.kind === "string" ? c.kind : "uncaught";
      return {
        signature: typeof c.signature === "string" ? c.signature : "",
        errorName: typeof c.errorName === "string" ? c.errorName : "",
        kind,
        redactedFrame:
          typeof c.redactedFrame === "string" ? c.redactedFrame : "",
        countBucket: typeof c.countBucket === "string" ? c.countBucket : "",
        occurrences: num(c.occurrences),
        firstSeenAt: typeof c.firstSeenAt === "string" ? c.firstSeenAt : null,
        lastSeenAt: typeof c.lastSeenAt === "string" ? c.lastSeenAt : null,
        relatedRules: CRASH_KIND_RULES[kind] ?? DEFAULT_CRASH_RULES,
      };
    })
    .filter(piiSafe);

  const stabilityRaw =
    data.stability &&
    typeof data.stability === "object" &&
    !Array.isArray(data.stability)
      ? (data.stability as Record<string, unknown>)
      : null;
  const stability = stabilityRaw && piiSafe(stabilityRaw) ? stabilityRaw : null;

  // Defense in depth over the SERVER-SUPPLIED data only. `note`/`source`/the
  // rule-id pointers are our own shipped constants, not server data — and the
  // field name "note" itself trips the guard — so the final check excludes them.
  if (!piiSafe({ crashes, stability })) return null;

  return {
    available: true,
    source: "boosthis-server",
    crashClasses: num(data.crashClasses),
    totalOccurrences: num(data.totalOccurrences),
    crashes,
    stability,
    stabilityRules: STABILITY_RULES,
    note: NOTE_CRASH_RISK,
  };
}

// ───────────────────────────────────────────────────────────────────────────
// Full-stack trace read (Stage 2 flagship)
// ───────────────────────────────────────────────────────────────────────────

const NOTE_FULL_STACK_TRACE =
  "Latest full-stack trace read from your Boosthis server: one user action " +
  "stitched across the layers this install recorded, as a waterfall of spans " +
  "(layer, code-defined route label, duration, start offset, rating) plus an " +
  "honest full-stack score rated against the shared TTI thresholds. The web " +
  "layer is the trace ROOT: the browser click/navigation that started the " +
  "action. Each span's slowestLayerRules point at the rules most likely to " +
  "fix that layer — fetch them with boosthis.get_rule. NOTE: a per-install " +
  "read token is self-scoped, so this shows only THIS install's own spans; " +
  "pass an account_token on the hosted MCP to unlock the full stitched " +
  "Web \u2192 Node \u2192 Python waterfall across all your apps.";

// Static, shipped map from a span's LAYER to the rule ids most likely to fix a
// slow span in that layer. Deterministic (no server round-trip): the matcher /
// rule book holds the fix text; this only points the AI at the right rules.
// The non-web ids live in the other runtimes' rule packs — the pointer tells
// the AI which rules to fetch from the matching runtime's server.
const LAYER_RULES: Record<string, string[]> = {
  web: [
    "client-fetch-waterfall",
    "long-tasks-block-input",
    "huge-initial-bundle",
    "render-blocking-scripts",
  ],
  rn: [
    "per-item-fetch-waterfall",
    "client-refetch-no-cache",
    "screen-load-budget-500ms-p75",
    "oversized-thumbnail-fetch",
  ],
  node: [
    "sync-fs-in-handler",
    "await-in-loop",
    "n-plus-one-orm-node",
    "no-keepalive-outbound",
  ],
  py: [
    "n-plus-one-orm-query",
    "sync-io-in-async-handler",
    "fastapi-sync-route-blocks-event-loop",
    "db-statement-timeout-missing",
  ],
};

interface TraceReadSpanItem {
  layer?: string;
  routeLabel?: string;
  durationMs?: number;
  startOffsetMs?: number;
  rating?: string;
  isRoot?: boolean;
}

/** The install's own latest full-stack trace waterfall, with rule pointers for
 *  the slowest span's layer. Fail-open: any failure (not configured, offline,
 *  non-200 / no trace yet, bad JSON, PII trip) returns null so the caller
 *  serves the on-device note. NEVER mutates module state — per-call creds are
 *  threaded through resolveCreds only. */
export async function getLiveFullStackTrace(
  creds?: LiveCreds,
): Promise<unknown | null> {
  const cfg = resolveCreds(creds);
  if (!cfg) return null;
  const data = (await fetchJson(
    `/traces/latest?installId=${encodeURIComponent(cfg.installId)}`,
    cfg,
  )) as {
    traceId?: string;
    spanCount?: number;
    layers?: unknown[];
    rootLayer?: string;
    rootDurationMs?: number;
    score?: number;
    scoreRating?: string;
    summary?: string;
    slowest?: TraceReadSpanItem;
    spans?: TraceReadSpanItem[];
  } | null;
  if (!data) return null;

  const mapSpan = (s: TraceReadSpanItem) => ({
    layer: typeof s.layer === "string" ? s.layer : "",
    routeLabel: typeof s.routeLabel === "string" ? s.routeLabel : "",
    durationMs: num(s.durationMs),
    startOffsetMs: num(s.startOffsetMs),
    rating: typeof s.rating === "string" ? s.rating : "",
    isRoot: s.isRoot === true,
  });

  const spansIn = Array.isArray(data.spans) ? data.spans : [];
  const spans = spansIn.map(mapSpan).filter(piiSafe);

  const slowestRaw =
    data.slowest && typeof data.slowest === "object" ? data.slowest : null;
  const slowestMapped = slowestRaw ? mapSpan(slowestRaw) : null;
  const slowest =
    slowestMapped && piiSafe(slowestMapped) ? slowestMapped : null;

  const layers = (Array.isArray(data.layers) ? data.layers : []).filter(
    (l): l is string => typeof l === "string",
  );

  const traceId = typeof data.traceId === "string" ? data.traceId : "";
  const summary = typeof data.summary === "string" ? data.summary : "";

  // Defense in depth over the SERVER-SUPPLIED data only. `note`/`source`/the
  // rule-id pointers are our own shipped constants, not server data — and the
  // field name "note" itself trips the guard — so the final check excludes them.
  if (!piiSafe({ traceId, layers, summary, slowest, spans })) return null;

  return {
    available: true,
    source: "boosthis-server",
    traceId,
    spanCount: num(data.spanCount),
    layers,
    rootLayer: typeof data.rootLayer === "string" ? data.rootLayer : null,
    rootDurationMs: num(data.rootDurationMs),
    score: num(data.score),
    scoreRating:
      typeof data.scoreRating === "string" ? data.scoreRating : null,
    summary,
    slowest,
    slowestLayerRules:
      slowest && LAYER_RULES[slowest.layer] ? LAYER_RULES[slowest.layer] : [],
    spans,
    note: NOTE_FULL_STACK_TRACE,
  };
}
