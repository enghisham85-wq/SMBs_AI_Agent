/** Feedback store — the AI → Boosthis learning loop (web port).
 *
 * Mirrors `lib/boosthis-runtime-node/src/feedback.ts` (which itself mirrors
 * `lib/boosthis-py/boosthis/feedback.py`). When an AI agent uses Boosthis to
 * diagnose a perf issue and then acts on the suggestion, the outcome flows
 * back here as a structured event so we can improve the rule book over time.
 *
 * This module runs Node-side only (the web MCP server is a stdio process) —
 * it is never bundled into browser code, which is why the fs/os/path imports
 * are safe here.
 *
 * Three event kinds:
 *   - fix_outcome           — AI applied a rule's fixTemplate; did it help?
 *   - unmatched_pattern     — AI saw a slow pattern with no matching rule.
 *   - rule_improvement      — AI proposes a refinement to an existing rule.
 *
 * Storage is append-only JSONL at ~/.boosthis/feedback/feedback.jsonl (capped
 * at FEEDBACK_MAX_LINES, oldest rotated out) — the SAME local corpus the Node
 * and Python runtimes use, so one machine keeps one learning file. Local-first;
 * nothing leaves the host unless the integrator wires up safeTransmit.
 *
 * Every payload is filtered through assertNoPII BEFORE it touches disk on
 * the same denylist the rest of the runtime uses. The AI can lie about
 * field names; the guard does not trust them.
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { assertNoPII, PIIDetectedError } from "./no-pii";

export const FEEDBACK_DIR = path.join(os.homedir(), ".boosthis", "feedback");
export const FEEDBACK_PATH = path.join(FEEDBACK_DIR, "feedback.jsonl");
export const FEEDBACK_MAX_LINES = 5000;
export const FEEDBACK_TRIM_TO = 4000;

export type EventKind = "fix_outcome" | "unmatched_pattern" | "rule_improvement";

const VALID_KINDS: ReadonlySet<EventKind> = new Set<EventKind>([
  "fix_outcome",
  "unmatched_pattern",
  "rule_improvement",
]);

export interface FeedbackEvent {
  kind: EventKind;
  timestamp_ms: number;
  app_id: string | null;
  payload: Record<string, unknown>;
}

export class FeedbackRejectedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FeedbackRejectedError";
  }
}

// Allow tests to override the storage path without touching the real ~/.boosthis/.
let _dir = FEEDBACK_DIR;
let _path = FEEDBACK_PATH;

/** Test-only: redirect feedback storage to a sandbox directory. */
export function _setStorageDirForTests(dir: string): void {
  _dir = dir;
  _path = path.join(dir, "feedback.jsonl");
}

/** Test-only: restore default ~/.boosthis/feedback path. */
export function _resetStorageDirForTests(): void {
  _dir = FEEDBACK_DIR;
  _path = FEEDBACK_PATH;
}

function ensureDir(): void {
  try {
    fs.mkdirSync(_dir, { recursive: true });
  } catch (e) {
    throw new FeedbackRejectedError(`cannot create ${_dir}: ${(e as Error).message}`);
  }
}

function rotateIfNeeded(): void {
  // Best-effort — never let rotation failure block a write.
  try {
    if (!fs.existsSync(_path)) return;
    const data = fs.readFileSync(_path, "utf8");
    const lines = data.split("\n");
    // split() on a trailing-newline file yields a final empty string; drop it.
    const real = lines[lines.length - 1] === "" ? lines.slice(0, -1) : lines;
    if (real.length <= FEEDBACK_MAX_LINES) return;
    const keep = real.slice(real.length - FEEDBACK_TRIM_TO);
    const tmp = _path + ".tmp";
    fs.writeFileSync(tmp, keep.join("\n") + "\n", "utf8");
    fs.renameSync(tmp, _path);
  } catch {
    // swallow — rotation is opportunistic
  }
}

/** Append a feedback event. PII-guarded; throws FeedbackRejectedError on rejection.
 *
 * The PII guard runs on the *whole* payload (keys + values) so an AI cannot
 * smuggle a user identifier through by stashing it in an attacker-chosen
 * field like `free_form_notes`.
 */
export function record(
  kind: EventKind,
  payload: Record<string, unknown>,
  appId: string | null = null,
): FeedbackEvent {
  if (!VALID_KINDS.has(kind)) {
    throw new FeedbackRejectedError(`unknown kind: ${String(kind)}`);
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw new FeedbackRejectedError("payload must be an object");
  }
  try {
    assertNoPII(payload);
  } catch (e) {
    if (e instanceof PIIDetectedError) {
      throw new FeedbackRejectedError(
        `PII guard rejected feedback (field=${JSON.stringify(e.fieldName)})`,
      );
    }
    throw e;
  }
  const event: FeedbackEvent = {
    kind,
    timestamp_ms: Date.now(),
    app_id: appId,
    payload,
  };
  ensureDir();
  fs.appendFileSync(_path, JSON.stringify(event) + "\n", "utf8");
  rotateIfNeeded();
  return event;
}

/** Return most-recent events (newest first). Tolerant of corrupt lines. */
export function recent(opts: { limit?: number; kind?: EventKind } = {}): FeedbackEvent[] {
  const limit = opts.limit ?? 100;
  const wantKind = opts.kind;
  if (!fs.existsSync(_path)) return [];
  let data: string;
  try {
    data = fs.readFileSync(_path, "utf8");
  } catch {
    return [];
  }
  const lines = data.split("\n");
  const out: FeedbackEvent[] = [];
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i]?.trim();
    if (!line) continue;
    try {
      const row = JSON.parse(line) as Partial<FeedbackEvent>;
      const k = row.kind;
      if (!k || !VALID_KINDS.has(k)) continue;
      if (wantKind && k !== wantKind) continue;
      const payload = row.payload;
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) continue;
      out.push({
        kind: k,
        timestamp_ms: typeof row.timestamp_ms === "number" ? row.timestamp_ms : 0,
        app_id: typeof row.app_id === "string" ? row.app_id : null,
        payload: payload as Record<string, unknown>,
      });
      if (out.length >= limit) break;
    } catch {
      continue;
    }
  }
  return out;
}

/** Aggregate counts by kind + per-rule helpful/unhelpful counts. */
export function summary(): {
  total: number;
  by_kind: Record<EventKind, number>;
  fix_outcomes: { helpful: number; unhelpful: number };
  by_rule: Record<string, { helpful: number; unhelpful: number }>;
} {
  const events = recent({ limit: FEEDBACK_MAX_LINES });
  const by_kind: Record<EventKind, number> = {
    fix_outcome: 0,
    unmatched_pattern: 0,
    rule_improvement: 0,
  };
  let helpful = 0;
  let unhelpful = 0;
  // Null-prototype map — MCP `rule_id` is attacker-controlled (any AI agent
  // can send it). A plain object would let `__proto__` / `constructor` /
  // `prototype` mutate Object.prototype across the host process. `Map`
  // with string keys gives us prototype-pollution-proof accumulation.
  const byRuleMap = new Map<string, { helpful: number; unhelpful: number }>();
  for (const e of events) {
    by_kind[e.kind] = (by_kind[e.kind] ?? 0) + 1;
    if (e.kind === "fix_outcome") {
      const ruleId = typeof e.payload["rule_id"] === "string" ? (e.payload["rule_id"] as string) : "";
      const was = e.payload["was_helpful"] === true;
      if (was) helpful++;
      else unhelpful++;
      if (ruleId) {
        const bucket = byRuleMap.get(ruleId) ?? { helpful: 0, unhelpful: 0 };
        bucket[was ? "helpful" : "unhelpful"]++;
        byRuleMap.set(ruleId, bucket);
      }
    }
  }
  const by_rule: Record<string, { helpful: number; unhelpful: number }> =
    Object.create(null);
  for (const [k, v] of byRuleMap) by_rule[k] = v;
  return { total: events.length, by_kind, fix_outcomes: { helpful, unhelpful }, by_rule };
}

/** Remove the feedback file (tests + `boosthis telemetry forget` parity). */
export function clear(): void {
  try {
    fs.unlinkSync(_path);
  } catch {
    // ENOENT is fine
  }
}
