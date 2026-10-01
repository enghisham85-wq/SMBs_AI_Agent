/** Learning-loop WRITE client (dev-only, runs Node-side under the MCP server).
 *
 * Web parity of `lib/boosthis-runtime-node/src/proposeRule.ts` — see that file
 * for the full contract. When the developer's own AI reports a slow pattern that
 * no shipped rule covers (`boosthis.report_unmatched_pattern`), this module
 * forwards a privacy-safe PROPOSAL to the maintainer's review queue
 * (`POST /rules/propose`). The maintainer approves or rejects it by hand —
 * nothing here ever ships a rule.
 *
 * This module uses `node:crypto` and is therefore imported ONLY by the Node-side
 * MCP entrypoints (`mcp.ts` / `mcp-stdio.ts`), never re-exported from the
 * browser barrel (`index.ts`).
 *
 * TWO privacy tiers, decided by the SERVER:
 *   - TIER A (always): a demand signal only — runtime, a coarse category,
 *     severity + timing buckets, a NON-reversible sha256 of the pattern, and an
 *     occurrence count. No free text, no code, no identifiers.
 *   - TIER B (only when the app proves it has connected its AI): additionally
 *     the raw pattern text and any AI-drafted rule (id/title/whenToApply/fix).
 *     Proof is the install's read/web-read/delete token in the
 *     `X-Boosthis-Install-*` headers; the invite key alone never unlocks it.
 *
 * The payload rides the shared PII guard chokepoint (`_safeTransmitInternal`),
 * `BOOSTHIS_DISABLED` silences it (the chokepoint returns a synthetic 204), and
 * it is entirely fail-open — any error is swallowed so a best-effort proposal
 * can never break the tool the developer's AI called.
 */

import { createHash } from "node:crypto";
import { unwatchedFetch } from "./callWatch";
import { _safeTransmitInternal } from "./transmit";
import { checkNoPII } from "./no-pii";
import { SCORE_THRESHOLDS } from "./thresholds";

export type ProposalRuntime = "rn" | "node" | "py" | "web";
export type ProposalCategory =
  | "startup"
  | "navigation"
  | "interaction"
  | "rendering"
  | "network"
  | "data"
  | "memory"
  | "other";
type SeverityBucket = "low" | "med" | "high";
type TimingBucket = "good" | "warn" | "poor";

const CATEGORIES: ReadonlySet<string> = new Set<ProposalCategory>([
  "startup",
  "navigation",
  "interaction",
  "rendering",
  "network",
  "data",
  "memory",
  "other",
]);

function env(): Record<string, string | undefined> {
  return process.env;
}

function endpointBase(): string {
  const e = env();
  // `||` (not `??`) so a blank var never shadows the fallback — mirrors community.ts.
  const raw =
    e.BOOSTHIS_ENDPOINT ||
    e.BOOSTEN_ENDPOINT ||
    "https://www.boosthis.com/api";
  return raw.replace(/\/+$/, "");
}

function inviteKey(): string | undefined {
  const e = env();
  const k = (e.BOOSTHIS_INVITE_KEY || e.BOOSTEN_INVITE_KEY || "").trim();
  return k || undefined;
}

/** Read/web-read/delete token that proves control of a connected install.
 *  Same env vars the stdio server uses to wire live-data reads. */
function installCreds(): { installId: string; token: string } | null {
  const e = env();
  const installId = (e.BOOSTHIS_INSTALL_ID || e.BOOSTEN_INSTALL_ID || "").trim();
  const token = (e.BOOSTHIS_READ_TOKEN || e.BOOSTEN_READ_TOKEN || "").trim();
  if (!installId || !token) return null;
  return { installId, token };
}

/** Map the tool's `language` enum to the wire `runtime` enum. */
export function runtimeFromLanguage(language: string): ProposalRuntime {
  switch (language) {
    case "node":
      return "node";
    case "python":
      return "py";
    case "react-native":
      return "rn";
    case "web":
      return "web";
    default:
      return "web";
  }
}

/** Non-reversible dedup + distinct-project counting key: sha256 of the
 *  whitespace-normalized, lower-cased pattern. The raw pattern is NEVER part
 *  of the tier-A signal — only this hash is. */
export function patternSignature(pattern: string): string {
  const norm = pattern.toLowerCase().replace(/\s+/g, " ").trim();
  return createHash("sha256").update(norm).digest("hex");
}

/** Bucket the observed latency against the shared LCP thresholds (the web
 *  runtime's primary "how slow is this page" band — it has no TTI axis).
 *  Absent or non-finite input degrades to "warn" (an unmatched pattern is,
 *  by nature, a suspected problem). */
export function timingBucketFor(observedMs: number | undefined): TimingBucket {
  if (typeof observedMs !== "number" || !Number.isFinite(observedMs)) {
    return "warn";
  }
  const t = SCORE_THRESHOLDS.lcp;
  if (observedMs <= t.good) return "good";
  if (observedMs >= t.poor) return "poor";
  return "warn";
}

function severityFor(timing: TimingBucket): SeverityBucket {
  if (timing === "poor") return "high";
  if (timing === "warn") return "med";
  return "low";
}

function normalizeCategory(category: string | undefined): ProposalCategory {
  return category && CATEGORIES.has(category)
    ? (category as ProposalCategory)
    : "other";
}

/** Include a tier-B free-text field ONLY if it is present and passes the shared
 *  PII guard. Dropping a PII-tripping field (rather than aborting) keeps the
 *  tier-A demand signal alive while never sending PII-shaped free text — the
 *  server re-runs the same guard as the authority. */
function cleanText(
  value: string | undefined,
  maxLen: number,
): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.slice(0, maxLen);
  if (!trimmed) return undefined;
  return checkNoPII(trimmed) === null ? trimmed : undefined;
}

export interface UnmatchedPatternReport {
  pattern: string;
  language: string;
  observedMs?: number;
  category?: string;
  /** TIER B ONLY — an AI-drafted candidate rule. Sent only when install creds
   *  resolve; each field is independently PII-scanned and dropped if it trips. */
  draft?: {
    proposedRuleId?: string;
    title?: string;
    whenToApply?: string;
    fixTemplate?: string;
  };
}

/**
 * Fire-and-forget a rule PROPOSAL to the maintainer's review queue.
 *
 * Fully fail-open: returns silently (never throws) when there is no invite key,
 * when `BOOSTHIS_DISABLED` is set, or on any network/PII/transport error. The
 * caller (`report_unmatched_pattern`) treats it as best-effort and never awaits
 * its outcome for the tool result.
 */
export async function reportUnmatchedPattern(
  report: UnmatchedPatternReport,
): Promise<void> {
  try {
    const key = inviteKey();
    // Gate: a proposal requires an invite key (the distributable `fix` scope).
    // Without one, this is an unregistered install — stay fully on-device.
    if (!key) return;
    // Defensive kill-switch check; _safeTransmitInternal also honors it.
    if (env().BOOSTHIS_DISABLED === "1") return;

    const timingBucket = timingBucketFor(report.observedMs);
    const body: Record<string, unknown> = {
      runtime: runtimeFromLanguage(report.language),
      category: normalizeCategory(report.category),
      severityBucket: severityFor(timingBucket),
      timingBucket,
      signature: patternSignature(report.pattern),
      occurrences: 1,
    };

    // TIER B: only when we can prove control of a connected install. The
    // server still decides the tier — we just attach the proof + drafted text.
    let internalHeaders: Record<string, string> | undefined;
    const creds = installCreds();
    if (creds) {
      internalHeaders = {
        "X-Boosthis-Install-Id": creds.installId,
        "X-Boosthis-Install-Token": creds.token,
      };
      const rawPattern = cleanText(report.pattern, 2000);
      if (rawPattern) body.pattern = rawPattern;
      const d = report.draft ?? {};
      if (d.proposedRuleId) {
        const id = d.proposedRuleId.slice(0, 80);
        if (/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(id)) body.proposedRuleId = id;
      }
      const title = cleanText(d.title, 160);
      if (title) body.title = title;
      const whenToApply = cleanText(d.whenToApply, 2000);
      if (whenToApply) body.whenToApply = whenToApply;
      const fixTemplate = cleanText(d.fixTemplate, 8000);
      if (fixTemplate) body.fixTemplate = fixTemplate;
    }

    await _safeTransmitInternal(
      `${endpointBase()}/rules/propose`,
      body,
      // Ours, not the app's: go under the call watcher's wrapper when it is on.
      { fetchImpl: unwatchedFetch() ?? undefined },
      `Bearer ${key}`,
      internalHeaders,
    );
  } catch {
    // Best-effort learning loop: never let a proposal break the tool call.
  }
}
