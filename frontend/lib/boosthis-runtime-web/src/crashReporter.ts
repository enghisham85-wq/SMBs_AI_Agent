/* ─── Boosthis: privacy-safe crash reporter (Web) ────────────────────
 *
 * Faithful port of `lib/boosthis-runtime-node/src/crashReporter.ts`, adapting
 * ONLY the install layer to the browser. Captures REAL uncaught crashes from
 * the host page and turns each one into a tiny, scrubbed fingerprint that is
 * safe to send off-device. This is NOT a perf checklist rule — it is a separate
 * always-on channel (for registered apps) that installs once from
 * `enableTelemetry` and reports on its own. Nothing here runs as an import
 * side-effect; `installCrashHandlers()` must be called explicitly.
 *
 * INSTALL LAYER — browser-specific, guest-code-safe:
 * We register PASSIVE listeners on `window`:
 *   - `"error"`            → kind `"uncaught"`  (reads `event.error`)
 *   - `"unhandledrejection"` → kind `"unhandledRejection"` (reads `event.reason`)
 * Both listeners NEVER call `preventDefault()`, so the browser's own default
 * behavior (logging the error/rejection to the console, dev-tools breakpoints)
 * is completely unchanged — a drop-in library must not alter host behavior.
 * Resource-load failures (a broken <img>/<script>) also fire `"error"` on
 * window but carry no `error` object and no message; those are IGNORED (they
 * are not exceptions). The module no-ops entirely outside a browser
 * (`typeof window === "undefined"`), e.g. SSR, tests, the Node-side MCP
 * entrypoint.
 *
 * PRIVACY: the DEFAULT payload carries only the error type, a hashed signature,
 * a redacted top frame, and a bucketed count — never source, values, or PII.
 * OPT-IN detailed mode (per app) additionally carries a PII-scrubbed first
 * message line (`summary`) and sanitized stack `frames` (function + file
 * BASENAME + line/col — absolute paths, URLs, query strings and arguments are
 * stripped in-process). `summary` and any frame that trips the PII guard are
 * dropped individually so the rest of the crash still reports. The field names
 * deliberately avoid the PII denylist (no `message`/`text`/`body`).
 *
 * Crash-safety: every entry point is wrapped in try/catch so a bug in the
 * reporter itself can never crash the host page. Persistence is synchronous
 * (`localStorage` via storage) so a crash that immediately unloads the page is
 * still reported on the next launch.
 */

import { isBoosthisDisabled } from "./runtimeFlags";
import { currentAction } from "./spanScope";
import {
  currentFailurePart,
  FAILURE_PART_UNKNOWN,
  type FailurePartUnknown,
} from "./failurePart";
import { checkNoPII } from "./no-pii";
import { storage } from "./storage";

/** Persisted-pending crash buffer key. Mirrors the RN/Node store key. */
const STORAGE_KEY = "crash-pending:v1";
/** Bound the in-memory + persisted crash set so a pathological app that throws
 *  a unique error class on every tick can never grow memory/storage without
 *  limit. Distinct crash SIGNATURES (not crash frequency) are what counts. */
const MAX_CRASHES = 200;
/** Server caps a batch at 50 reports (CrashBatch.maxItems). */
const MAX_BATCH = 50;
/** Server caps a single signature's occurrences at 100000. */
const MAX_OCCURRENCES = 100000;
/** Server caps detailed frames at 20 (CrashReport.frames.maxItems). */
const MAX_FRAMES = 20;

export type CrashKind = "uncaught" | "unhandledRejection" | "render";

/** One sanitized stack frame (opt-in detailed mode only). */
export interface CrashFrameData {
  func: string;
  file: string;
  line?: number;
  column?: number;
}

/** Privacy-safe crash fingerprint. Shape matches the OpenAPI `CrashReport`
 *  schema (camelCase on the wire). NEVER carries source code, user values, or
 *  PII. */
export interface CrashReportPayload {
  signature: string;
  errorName: string;
  kind: CrashKind;
  redactedFrame: string;
  countBucket: string;
  occurrences: number;
  /** OPT-IN ONLY. PII-scrubbed first message line. */
  summary?: string;
  /** OPT-IN ONLY. Sanitized stack frames. */
  frames?: CrashFrameData[];
  /** The ACTION this crash happened in: the 32-hex trace id, and the 16-hex
   *  id of the call that was open. Both are omitted when the kit held no
   *  action at that moment — an omitted key means "no action recorded", which
   *  is not the same as "happened outside any action", and the server never
   *  invents one. Neither is screen-bearing: both are random correlation tags
   *  and neither can carry a value from the page. */
  traceId?: string;
  spanId?: string;
  /** WHERE it happened — the screen the document was on, through the one
   *  part-name rule. Exactly one of these two is ever set: the part when the
   *  kit could name it, the term when it looked and could not. The two ids
   *  above answer a different question and never stand in for either. See
   *  `failurePart.ts`. */
  routeLabel?: string;
  partUnknown?: FailurePartUnknown;
}

/** Network submitter wired by the telemetry client (→ `transmitCrashes`).
 *  Registered on `enableTelemetry`, cleared on `forget()`. */
export type CrashSubmitter = (
  crashes: readonly CrashReportPayload[],
) => Promise<number>;

interface CrashEntry {
  signature: string;
  errorName: string;
  kind: CrashKind;
  redactedFrame: string;
  summary?: string;
  frames?: CrashFrameData[];
  /** The action the LAST occurrence of this signature happened in, when one
   *  was open. Absent means no occurrence has been able to name one. */
  traceId?: string;
  spanId?: string;
  /** Where the LAST occurrence of this signature happened. One or the other,
   *  never both, and a later occurrence that could name a screen replaces a
   *  term an earlier one left. */
  routeLabel?: string;
  partUnknown?: FailurePartUnknown;
  /** Lifetime occurrences observed (across restored sessions); drives the
   *  bucketed count. */
  sessionTotal: number;
  /** Occurrences not yet acknowledged by the server (the delta the process
   *  sends; the server ADDS it to the row's lifetime total). */
  unsent: number;
  lastSeen: number;
}

/** Is this value one of the three words a kit may use for "we looked and
 *  could not tell"? Asked of anything read back out of local storage: the
 *  closed vocabulary is only closed if the restore re-checks it rather than
 *  trusting what it finds there. */
function isRestoredUnknownTerm(v: unknown): v is FailurePartUnknown {
  return (
    v === FAILURE_PART_UNKNOWN.noPartOpen ||
    v === FAILURE_PART_UNKNOWN.notTracked ||
    v === FAILURE_PART_UNKNOWN.refused
  );
}

let activeSubmitter: CrashSubmitter | null = null;
let active = false;
let detailedMode = false;
let errorListener: ((event: Event) => void) | null = null;
let rejectionListener: ((event: Event) => void) | null = null;
let restoredOnce = false;
/** Count of crashes captured this session (uncaught / unhandledRejection /
 *  render). Read by the crashFree meter axis; zeroed on forget()/reset. */
let crashTotal = 0;

const pending = new Map<string, CrashEntry>();

/** Register the crash auto-submitter. Pass `null` to clear (on opt-out). */
export function setCrashSubmitter(submitter: CrashSubmitter | null): void {
  activeSubmitter = submitter;
}

/* ─── Redaction helpers ──────────────────────────────────────────── */

/** Cheap stable djb2 hash. Input is already redacted/non-reversible content;
 *  the output is used purely to GROUP identical crash classes — it carries no
 *  recoverable user data. */
function hash(input: string): string {
  let h = 5381;
  for (let i = 0; i < input.length; i++) h = (h * 33) ^ input.charCodeAt(i);
  return (h >>> 0).toString(36);
}

/** Coarse, privacy-safe occurrence bucket. Always <= 12 chars (server cap). */
function bucketCount(n: number): string {
  if (n <= 1) return "1";
  if (n <= 5) return "2-5";
  if (n <= 20) return "6-20";
  if (n <= 100) return "21-100";
  return "100+";
}

/** Reduce a possibly-non-Error throw to an Error-like shape. */
function toError(error: unknown): {
  name: string;
  message: string;
  stack?: string;
} {
  if (error instanceof Error) {
    return { name: error.name, message: error.message, stack: error.stack };
  }
  if (error && typeof error === "object") {
    const o = error as { name?: unknown; message?: unknown; stack?: unknown };
    return {
      name: typeof o.name === "string" ? o.name : "Error",
      message: typeof o.message === "string" ? o.message : String(error),
      stack: typeof o.stack === "string" ? o.stack : undefined,
    };
  }
  return {
    name: "Error",
    message: typeof error === "string" ? error : String(error),
  };
}

/** Keep only identifier characters so an error NAME can never carry an
 *  email/path/URL/value. Class names are code-defined; anything else is
 *  collapsed to "Error". */
function sanitizeErrorName(name: string): string {
  const cleaned = name.replace(/[^A-Za-z0-9_$]/g, "").slice(0, 80);
  return cleaned.length > 0 ? cleaned : "Error";
}

/** Reduce a file path/URL to its bare basename, dropping directories, URL
 *  scheme/host, query strings, and fragments. */
function fileBasename(raw: string): string {
  let s = raw;
  const q = s.search(/[?#]/);
  if (q >= 0) s = s.slice(0, q);
  // Normalize both separators, take the last segment.
  const segs = s.split(/[\\/]/);
  s = segs[segs.length - 1] ?? "";
  s = s.trim().slice(0, 120);
  return s.length > 0 ? s : "<unknown>";
}

interface RawFrame {
  func: string;
  file: string;
  line?: number;
  column?: number;
}

/** Parse a JS error stack into frames, handling both the V8/Chromium
 *  ("at fn (file:line:col)" / "at file:line:col") and JSC/Safari/Firefox
 *  ("fn@file:line:col") formats. File paths are reduced to basenames here so
 *  no absolute path or URL ever survives parsing. */
function parseStack(stack: string | undefined): RawFrame[] {
  if (!stack || typeof stack !== "string") return [];
  const frames: RawFrame[] = [];
  const lines = stack.split("\n");
  for (const lineRaw of lines) {
    const line = lineRaw.trim();
    if (!line) continue;
    let func = "<anonymous>";
    let loc = "";
    const v8 = line.match(/^at\s+(.*?)\s+\((.*)\)$/);
    if (v8) {
      func = v8[1] ?? "<anonymous>";
      loc = v8[2] ?? "";
    } else {
      const v8NoFn = line.match(/^at\s+(.*)$/);
      const jsc = line.match(/^(.*?)@(.*)$/);
      if (v8NoFn) {
        loc = v8NoFn[1] ?? "";
      } else if (jsc) {
        func = jsc[1] && jsc[1].length > 0 ? jsc[1] : "<anonymous>";
        loc = jsc[2] ?? "";
      } else {
        continue;
      }
    }
    // loc is "<path>:<line>:<col>" — split trailing :line:col off the path.
    let file = loc;
    let lineNo: number | undefined;
    let colNo: number | undefined;
    const m = loc.match(/^(.*):(\d+):(\d+)$/) ?? loc.match(/^(.*):(\d+)$/);
    if (m) {
      file = m[1] ?? loc;
      lineNo = clampInt(m[2]!);
      colNo = m[3] !== undefined ? clampInt(m[3]) : undefined;
    }
    func = func
      .replace(/[^A-Za-z0-9_$.<>\s]/g, "")
      .trim()
      .slice(0, 120);
    if (func.length === 0) func = "<anonymous>";
    frames.push({ func, file: fileBasename(file), line: lineNo, column: colNo });
    if (frames.length >= MAX_FRAMES) break;
  }
  return frames;
}

function clampInt(s: string): number | undefined {
  const n = parseInt(s, 10);
  if (!Number.isFinite(n) || n < 0) return undefined;
  return Math.min(n, 100_000_000);
}

function formatRedactedFrame(top: RawFrame | undefined): string {
  if (!top) return "<unknown>";
  const lineSuffix = top.line !== undefined ? `:${top.line}` : "";
  return `${top.func} (${top.file}${lineSuffix})`.slice(0, 160);
}

/** First line of the error message, scrubbed: dropped entirely if it trips the
 *  PII guard (email/JWT/bearer/IP/phone). Returns undefined when there is
 *  nothing safe to keep. */
function sanitizeSummary(message: string): string | undefined {
  const first = message.split(/\r?\n/)[0]?.trim().slice(0, 300);
  if (!first) return undefined;
  if (checkNoPII(first) !== null) return undefined;
  return first;
}

interface RedactedCrash {
  signature: string;
  errorName: string;
  kind: CrashKind;
  redactedFrame: string;
  summary?: string;
  frames?: CrashFrameData[];
}

/** Turn a raw throw into the closed, PII-safe crash shape. Detailed fields are
 *  added only when `detailedMode` is on and they pass the PII guard. */
function redactError(error: unknown, kind: CrashKind): RedactedCrash {
  const err = toError(error);
  const errorName = sanitizeErrorName(err.name);
  const frames = parseStack(err.stack);
  const top = frames[0];
  const redactedFrame = formatRedactedFrame(top);
  // The signature is ALWAYS sent (even in default mode), so its basis must never
  // contain user values. A redacted top frame is code-defined (function + file
  // basename + line) and safe; when there is NO frame (stackless Error or a
  // non-Error throw) we fall back to ONLY code-defined tokens — the sanitized
  // error name, the crash kind, and a constant marker — and NEVER the raw error
  // message, which could carry an email/token/user value that would otherwise
  // leave the device as a deterministic hash.
  const basis = top ? redactedFrame : `${errorName}|${kind}|<no-frame>`;
  const signature = `${errorName}:${hash(basis)}`.slice(0, 120);
  const out: RedactedCrash = { signature, errorName, kind, redactedFrame };
  if (detailedMode) {
    const summary = sanitizeSummary(err.message);
    if (summary) out.summary = summary;
    // Drop any individual frame that trips the PII guard rather than the whole
    // crash; func/file are sanitized above, so this is belt-and-braces.
    const safeFrames = frames
      .slice(0, MAX_FRAMES)
      .filter((f) => checkNoPII(f) === null)
      .map((f) => {
        const fr: CrashFrameData = { func: f.func, file: f.file };
        if (f.line !== undefined) fr.line = f.line;
        if (f.column !== undefined) fr.column = f.column;
        return fr;
      });
    if (safeFrames.length > 0) out.frames = safeFrames;
  }
  return out;
}

/* ─── Capture + flush ────────────────────────────────────────────── */

/** Record one crash. Idempotent-safe and never throws (fully guarded). No-op
 *  until `installCrashHandlers()` has run, so the reporter has zero effect on
 *  apps that never enabled telemetry, and a no-op under the kill-switch. */
function capture(error: unknown, kind: CrashKind): void {
  try {
    if (!active || isBoosthisDisabled()) return;
    crashTotal += 1;
    const r = redactError(error, kind);
    const now = Date.now();
    // WHICH ACTION THIS HAPPENED IN, when the kit actually holds one. Read
    // from the ambient span scope, which is synchronous by design, so it can
    // only ever name a call genuinely open on this stack — `window.onerror`
    // fires on the throwing stack, so a throw inside a scoped action is
    // placed. A rejection surfacing later has no scope open and this is null:
    // reported as "no action recorded", never guessed at from whatever fetch
    // happened to be in flight.
    const at = currentAction();
    // WHERE this crash happened, read NOW, on the same synchronous path as
    // the action above. `at` names an operation and never a place; this names
    // the place and never an operation. Both travel, neither substitutes.
    const where = currentFailurePart();
    const existing = pending.get(r.signature);
    if (existing) {
      existing.sessionTotal = Math.min(
        existing.sessionTotal + 1,
        MAX_OCCURRENCES,
      );
      existing.unsent = Math.min(existing.unsent + 1, MAX_OCCURRENCES);
      existing.kind = r.kind;
      existing.errorName = r.errorName;
      existing.redactedFrame = r.redactedFrame;
      existing.lastSeen = now;
      if (r.summary !== undefined) existing.summary = r.summary;
      if (r.frames !== undefined) existing.frames = r.frames;
      // A later occurrence that DOES know its action names it; one that does
      // not leaves the last known action alone rather than wiping it. Same
      // rule the server applies when it merges reports of one signature.
      if (at !== null) {
        existing.traceId = at.traceId;
        existing.spanId = at.spanId;
      }
      // WHERE, read at this occurrence's own moment of capture — never the
      // screen an earlier occurrence was on. A named screen replaces whatever
      // stood before it; a term replaces only another term, so one crash the
      // kit could place is not overwritten by a later one it could not.
      if (where.routeLabel !== undefined) {
        existing.routeLabel = where.routeLabel;
        existing.partUnknown = undefined;
      } else if (existing.routeLabel === undefined) {
        existing.partUnknown = where.partUnknown;
      }
    } else {
      pending.set(r.signature, {
        signature: r.signature,
        errorName: r.errorName,
        kind: r.kind,
        redactedFrame: r.redactedFrame,
        summary: r.summary,
        frames: r.frames,
        sessionTotal: 1,
        unsent: 1,
        lastSeen: now,
        ...(at !== null ? { traceId: at.traceId, spanId: at.spanId } : {}),
        ...where,
      });
      evictIfNeeded();
    }
    // Persist SYNCHRONOUSLY so a crash that immediately unloads the page before
    // the network flush completes is still reported on the next launch.
    persist();
    scheduleFlush();
  } catch {
    // The reporter must NEVER throw — a bug here can't be allowed to crash the
    // host page.
  }
}

/** Public entry for a Boosthis-internal render throw (SDK-internal). Kept for
 *  cross-runtime parity with RN's error boundary; tagged `kind: "render"`. */
export function reportRenderError(error: unknown): void {
  capture(error, "render");
}

/** Keep the crash set bounded: evict the least-recently-seen entries that have
 *  nothing left to send. */
function evictIfNeeded(): void {
  if (pending.size <= MAX_CRASHES) return;
  const sortable = [...pending.values()].sort((a, b) => a.lastSeen - b.lastSeen);
  for (const e of sortable) {
    if (pending.size <= MAX_CRASHES) break;
    if (e.unsent <= 0) pending.delete(e.signature);
  }
  // If everything still has unsent work, drop oldest regardless to honor the cap.
  while (pending.size > MAX_CRASHES) {
    const oldest = sortable.shift();
    if (!oldest) break;
    pending.delete(oldest.signature);
  }
}

let flushing = false;
let flushQueued = false;

function scheduleFlush(): void {
  void flush().catch(() => {
    // flush() is self-guarded; this is belt-and-braces so a rejected promise
    // never becomes an unhandled rejection.
  });
}

function buildBatch(): CrashReportPayload[] {
  const batch: CrashReportPayload[] = [];
  for (const e of pending.values()) {
    if (e.unsent <= 0) continue;
    const item: CrashReportPayload = {
      signature: e.signature,
      errorName: e.errorName,
      kind: e.kind,
      redactedFrame: e.redactedFrame,
      countBucket: bucketCount(e.sessionTotal),
      occurrences: Math.min(e.unsent, MAX_OCCURRENCES),
    };
    if (e.summary !== undefined) item.summary = e.summary;
    if (e.frames !== undefined) item.frames = e.frames;
    // Added only when present: a crash the kit could not place in an action
    // sends the keys it always did, and the server reads that as "no action
    // recorded" rather than as an action of its own.
    if (e.traceId !== undefined) item.traceId = e.traceId;
    if (e.spanId !== undefined) item.spanId = e.spanId;
    // WHERE, beside them and never instead of them. One of the two, never
    // both: a named screen and a term saying we could not name one are
    // answers to the same question and the name is the better one.
    if (e.routeLabel !== undefined) item.routeLabel = e.routeLabel;
    else if (e.partUnknown !== undefined) item.partUnknown = e.partUnknown;
    batch.push(item);
    if (batch.length >= MAX_BATCH) break;
  }
  return batch;
}

function applySent(batch: readonly CrashReportPayload[]): void {
  for (const sent of batch) {
    const cur = pending.get(sent.signature);
    if (!cur) continue;
    // Subtract only what we sent; any crashes that arrived while the request
    // was in flight remain queued for the next flush.
    cur.unsent = Math.max(0, cur.unsent - sent.occurrences);
  }
}

/** Flush pending crashes through the registered submitter. Always-on for
 *  registered apps (not gated by enable/disable) — only `BOOSTHIS_DISABLED`,
 *  `forget()`, or a missing submitter stop it. Never throws. */
async function flush(): Promise<void> {
  if (flushing) {
    flushQueued = true;
    return;
  }
  if (!activeSubmitter || isBoosthisDisabled() || pending.size === 0) return;
  flushing = true;
  try {
    const batch = buildBatch();
    if (batch.length === 0) return;
    let accepted = 0;
    try {
      accepted = await activeSubmitter(batch);
    } catch {
      accepted = 0;
    }
    if (accepted > 0) {
      applySent(batch);
      persist();
    }
  } finally {
    flushing = false;
    if (flushQueued) {
      flushQueued = false;
      scheduleFlush();
    }
  }
}

/* ─── Persistence (synchronous — see storage.set) ────────────────── */

function persist(): void {
  try {
    const list = [...pending.values()].slice(0, MAX_CRASHES);
    storage.set(STORAGE_KEY, JSON.stringify(list));
  } catch {
    // Best-effort — a failed write just means a crash before the next
    // successful flush may go unreported. Never throws.
  }
}

function restore(): void {
  if (restoredOnce) return;
  restoredOnce = true;
  try {
    const raw = storage.get(STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return;
    for (const c of parsed) {
      if (
        c &&
        typeof c.signature === "string" &&
        typeof c.errorName === "string" &&
        typeof c.redactedFrame === "string" &&
        (c.kind === "uncaught" ||
          c.kind === "unhandledRejection" ||
          c.kind === "render") &&
        typeof c.sessionTotal === "number" &&
        typeof c.unsent === "number"
      ) {
        if (pending.has(c.signature)) continue;
        pending.set(c.signature, {
          signature: c.signature,
          errorName: c.errorName,
          kind: c.kind,
          redactedFrame: c.redactedFrame,
          summary: typeof c.summary === "string" ? c.summary : undefined,
          frames: Array.isArray(c.frames)
            ? (c.frames as CrashFrameData[])
            : undefined,
          // The action the previous page placed this crash in, carried back
          // with it. A restore that dropped these would turn a crash that
          // knew its action into one that never did.
          ...(typeof c.traceId === "string" ? { traceId: c.traceId } : {}),
          ...(typeof c.spanId === "string" ? { spanId: c.spanId } : {}),
          // And WHERE it happened, for the same reason: the crash that takes
          // the page down with it is the one a developer most needs placed,
          // and it can only arrive through this restore. Re-checked against
          // the closed vocabulary on the way back in — the store is on the
          // visitor's own device and nothing reaches the wire on the strength
          // of a value something else could have written there.
          ...(typeof c.routeLabel === "string"
            ? { routeLabel: c.routeLabel }
            : isRestoredUnknownTerm(c.partUnknown)
              ? { partUnknown: c.partUnknown }
              : {}),
          sessionTotal: c.sessionTotal,
          unsent: c.unsent,
          lastSeen: typeof c.lastSeen === "number" ? c.lastSeen : Date.now(),
        });
      }
    }
    evictIfNeeded();
  } catch {
    // Corrupt/absent store — start clean.
  }
}

/* ─── Install / uninstall ────────────────────────────────────────── */

/**
 * Install the global crash handler. Called once from `enableTelemetry`.
 * Idempotent: calling again only updates `detailed` and never re-registers the
 * window listeners (which would otherwise double-report). No-op outside a
 * browser (SSR / tests / the Node-side MCP entrypoint).
 *
 * Guest-code invariant: the listeners are PASSIVE and never `preventDefault()`,
 * so the browser's default error/rejection handling (console logging, dev-tools
 * pause) is completely unchanged.
 */
export function installCrashHandlers(opts: { detailed: boolean }): void {
  try {
    detailedMode = opts.detailed === true;
    if (active) return;
    if (
      typeof window === "undefined" ||
      typeof window.addEventListener !== "function"
    ) {
      // Not a browser — nothing to hook. Leave `active` false so capture()
      // stays inert.
      return;
    }
    active = true;

    errorListener = (event: Event) => {
      // Never throws; a bug here must not break the page's error flow.
      try {
        const e = event as {
          error?: unknown;
          message?: unknown;
        };
        // Resource-load errors (broken <img>/<script>) also fire "error" but
        // carry no error object and no message — those are NOT exceptions.
        if (e.error !== undefined && e.error !== null) {
          capture(e.error, "uncaught");
          return;
        }
        const msg = typeof e.message === "string" ? e.message : "";
        if (msg) capture(msg, "uncaught");
      } catch {
        // swallow — never disturb the host's error handling
      }
    };

    rejectionListener = (event: Event) => {
      try {
        const e = event as { reason?: unknown };
        capture(e.reason, "unhandledRejection");
      } catch {
        // swallow
      }
    };

    // Passive listeners only — never preventDefault(), so host behavior is
    // unchanged.
    window.addEventListener("error", errorListener);
    window.addEventListener("unhandledrejection", rejectionListener);

    // Restore any crashes persisted from a previous session and flush them now
    // that a submitter is registered.
    try {
      restore();
      scheduleFlush();
    } catch {
      // Best-effort restore — a corrupt store just starts clean.
    }
  } catch {
    // installation must never throw into the host's enableTelemetry call.
  }
}

/**
 * Tear down the handlers and clear all pending crash state, including the
 * persisted store. Called by `telemetry.forget()` so nothing Boosthis-shaped is
 * left in the page or in storage. Removes ONLY the listeners we added.
 */
export async function uninstallCrashHandlers(): Promise<void> {
  active = false;
  detailedMode = false;
  try {
    if (
      typeof window !== "undefined" &&
      typeof window.removeEventListener === "function"
    ) {
      if (errorListener) window.removeEventListener("error", errorListener);
      if (rejectionListener) {
        window.removeEventListener("unhandledrejection", rejectionListener);
      }
    }
  } catch {
    // Best-effort.
  }
  errorListener = null;
  rejectionListener = null;
  pending.clear();
  crashTotal = 0;
  try {
    storage.remove(STORAGE_KEY);
  } catch {
    // Best-effort.
  }
}

/* ─── Test/debug helpers ─────────────────────────────────────────── */

/** Crashes captured since telemetry started (crashFree axis input). */
export function crashCount(): number {
  return crashTotal;
}

export const _crashInternals = {
  STORAGE_KEY,
  MAX_CRASHES,
  MAX_BATCH,
  redactError,
  parseStack,
  fileBasename,
  sanitizeErrorName,
  sanitizeSummary,
  bucketCount,
  capture,
  flush,
  buildBatch,
  isActive: () => active,
  isDetailed: () => detailedMode,
  pendingSize: () => pending.size,
  getPending: () => [...pending.values()].map((e) => ({ ...e })),
  getErrorListener: () => errorListener,
  getRejectionListener: () => rejectionListener,
  reset: () => {
    pending.clear();
    active = false;
    detailedMode = false;
    errorListener = null;
    rejectionListener = null;
    restoredOnce = false;
    flushing = false;
    flushQueued = false;
    activeSubmitter = null;
    crashTotal = 0;
  },
  setDetailedForTests: (d: boolean) => {
    detailedMode = d;
  },
  setActiveForTests: (a: boolean) => {
    active = a;
  },
  setCrashCountForTests: (n: number) => {
    crashTotal = n;
  },
};
