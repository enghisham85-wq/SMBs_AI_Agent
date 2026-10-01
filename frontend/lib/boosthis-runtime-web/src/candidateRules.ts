/* ─── Boosthis: candidate rules (browser) ────────────────────────────
 *
 * A "candidate rule" is a recurring problem signature the kit has observed but
 * which does NOT map to any rule in the existing rule book. When the same kind
 * of finding fires repeatedly, that is a strong signal there is a real,
 * generalizable pattern worth turning into a named rule everybody gets.
 *
 * This is the browser copy of the contract every kit follows
 * (`docs/kit-problem-reporting-contract.md`). It is a straight port of the
 * React Native accumulator with one difference: the web store
 * (`./storage`) is SYNCHRONOUS, so the read/write helpers here are too. The
 * thresholds, the buckets, the signature shape and the wire payload keys are
 * byte-identical to the Node, React Native and Python siblings, because a
 * signature that spells itself differently can never group with the same
 * problem found in another language.
 *
 * The browser kit matters more than most here: it is the only kit a customer's
 * END USERS run, so what it finds is what real visitors actually hit.
 *
 * PRIVACY. A finding is reduced to `<kind>:<severityBucket>:<countBucket>` and
 * nothing else. The finding's own `name` — a page label, a host — is used
 * LOCALLY only, for dedupe and a generic hint. It never enters the signature
 * and it never leaves the page: page labels routinely carry customer data.
 * There is no URL, no query string, no value and no code on this channel.
 *
 * Storage: one `localStorage` key per store (candidates, baselines), bounded to
 * MAX_CANDIDATES so a busy site cannot grow the visitor's storage without
 * limit. Both keys are wiped by `forget()`.
 */

import { storage } from "./storage";
import { findingPart, type FailurePartUnknown } from "./failurePart";
import { isProblemKind } from "./problemKinds";

const STORAGE_KEY = "candidate-rules:v1";
const MAX_CANDIDATES = 100;
const MIN_OCCURRENCES_TO_SURFACE = 2;

/** A cross-cutting problem one of the kit's live detectors found.
 *
 *  Deliberately structural, not a union of the vocabulary: `ingestFindings`
 *  filters on {@link isProblemKind}, so an off-list kind is DROPPED rather than
 *  throwing inside a visitor's page. */
export interface CrossCuttingFinding {
  /** A name from the shared vocabulary (`./problemKinds`). */
  kind: string;
  /** Local-only label (page label, bare host, or a fixed non-route word). */
  name: string;
  /** Honest measurement in ms — never a synthetic severity number. */
  p95: number;
  /** Honest occurrence count for this finding. */
  count: number;
  /** Generic, developer-facing sentence. Local only. */
  hint: string;
}

export type CandidateStatus = "new" | "promoted-local" | "submitted";

export interface CandidateRule {
  /** Stable local id derived from the signature. */
  id: string;
  /** Privacy-safe fingerprint: `<kind>:<bucket>:<countBucket>`. No values. */
  signature: string;
  /** Detector kind that produced this finding. */
  kind: string;
  /** Bucketed severity ("low" | "med" | "high") based on p95. */
  severityBucket: "low" | "med" | "high";
  /** First wall-clock time this signature was seen. */
  firstSeenAt: number;
  /** Most recent wall-clock time. */
  lastSeenAt: number;
  /** How many times this exact signature has fired. */
  occurrences: number;
  /** Safe, generic hint pulled from the originating finding. */
  exampleHint: string;
  /** Lifecycle state. Server upload sets `submitted`. */
  status: CandidateStatus;
  /** WHERE the most recent sighting of this signature was: the screen, as the
   *  sample path already labels it, when the detector filed the finding under
   *  one. Exactly one of these two is ever set — see `failurePart.ts`. The
   *  part travels; it is still never part of the signature, so grouping is
   *  unchanged and one problem does not split into one row per screen. */
  routeLabel?: string;
  partUnknown?: FailurePartUnknown;
}

function bucketSeverity(p95: number): "low" | "med" | "high" {
  if (p95 <= 100) return "low";
  if (p95 <= 500) return "med";
  return "high";
}

function bucketCount(count: number): string {
  if (count < 3) return "<3";
  if (count < 10) return "<10";
  if (count < 50) return "<50";
  return "50+";
}

/** Build the privacy-safe signature. The `name` field of the finding is
 *  intentionally dropped (it may carry customer content) and only the kind +
 *  severity bucket + count bucket are kept. The signature is what is persisted
 *  and what is uploaded. */
export function signatureFor(finding: CrossCuttingFinding): string {
  const sev = bucketSeverity(finding.p95);
  const ct = bucketCount(finding.count);
  return `${finding.kind}:${sev}:${ct}`;
}

function safeHashId(input: string): string {
  // Cheap stable hash — no crypto dep, no PII risk because the input is already
  // the signature (no user values). Used purely as a stable storage key.
  let h = 5381;
  for (let i = 0; i < input.length; i++) h = (h * 33) ^ input.charCodeAt(i);
  return "cr_" + (h >>> 0).toString(36);
}

/** Replace the persisted candidate set with the given list. Bounded. */
function writeAll(list: CandidateRule[]): void {
  try {
    const trimmed = list.slice(0, MAX_CANDIDATES);
    storage.set(STORAGE_KEY, JSON.stringify(trimmed));
  } catch {
    // Storage is best-effort (quota, private mode, sandboxed iframe). A failed
    // write just means the next ingest rebuilds from whatever persisted last.
  }
}

/** Read the persisted candidate set. Safe — returns [] on any error. */
export function listCandidateRules(): CandidateRule[] {
  try {
    const raw = storage.get(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (c): c is CandidateRule =>
        c &&
        typeof c.id === "string" &&
        typeof c.signature === "string" &&
        typeof c.occurrences === "number",
    );
  } catch {
    return [];
  }
}

/** Network submitter registered by the telemetry client. Cleared by
 *  `forget()`, so nothing leaves the page after erasure. */
export type CandidateSubmitter = (
  signatures: readonly {
    signature: string;
    kind: string;
    severityBucket: "low" | "med" | "high";
    countBucket: string;
    occurrences: number;
  }[],
  opts?: { keepalive?: boolean },
) => Promise<number>;

let activeSubmitter: CandidateSubmitter | null = null;

/** Register the auto-submitter. Pass `null` to clear (erasure / kill switch). */
export function setCandidateSubmitter(
  submitter: CandidateSubmitter | null,
): void {
  activeSubmitter = submitter;
}

/* ─── Fix-resolution detection ──────────────────────────────────────
 *
 * Boosthis reports not just problems but fixes: when a kind that was firing at
 * a worse severity is later observed only at a better one, a fix landed. What
 * is emitted is the kind plus the bucketed before→after rating — never the
 * code, the diff, a page label or a raw value.
 */

const BASELINE_STORAGE_KEY = "rule-baselines:v1";

type SeverityBucket = "low" | "med" | "high";
const SEV_RANK: Record<SeverityBucket, number> = { low: 0, med: 1, high: 2 };

type SampleRating = "good" | "needs-work" | "poor";
function severityToRating(sev: SeverityBucket): SampleRating {
  return sev === "high" ? "poor" : sev === "med" ? "needs-work" : "good";
}

/** Network submitter for fix-resolution signals. Wired and cleared exactly like
 *  {@link CandidateSubmitter}. Payload carries only the kind + bucketed
 *  before→after rating + the circumstances the problem held BEFORE the fix. */
export type ResolutionSubmitter = (
  resolutions: readonly {
    ruleId: string;
    kind: string;
    beforeRating: SampleRating;
    afterRating: SampleRating;
    occurrences: number;
    severityBucket?: SeverityBucket;
    countBucket?: string;
  }[],
  opts?: { keepalive?: boolean },
) => Promise<number>;

let activeResolutionSubmitter: ResolutionSubmitter | null = null;

/** Register the resolution auto-submitter. Pass `null` to clear. */
export function setResolutionSubmitter(
  submitter: ResolutionSubmitter | null,
): void {
  activeResolutionSubmitter = submitter;
}

/** Per-kind worst severity ever observed. Used only to detect when a kind's
 *  severity later improves. Contains no user data. */
function readBaselines(): Record<string, SeverityBucket> {
  try {
    const raw = storage.get(BASELINE_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    const out: Record<string, SeverityBucket> = {};
    for (const [k, v] of Object.entries(parsed)) {
      if (v === "low" || v === "med" || v === "high") out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

function writeBaselines(baselines: Record<string, SeverityBucket>): void {
  try {
    storage.set(BASELINE_STORAGE_KEY, JSON.stringify(baselines));
  } catch {
    // Best-effort, same policy as the candidate store.
  }
}

/** Module-level dedupe: the reporting loop runs on a cadence, and an identical
 *  finding set seen twice in a row is ONE observation. Without this,
 *  "recurrence" would measure how often the kit looks, not how often the
 *  problem happens. */
let lastIngestKey: string | null = null;

/** Test-only: clear the dedupe key so successive ingests in a single test
 *  process behave as independent observations. */
export function _resetIngestDedupeForTests(): void {
  lastIngestKey = null;
}

function fingerprintFindings(findings: CrossCuttingFinding[]): string {
  return findings
    .map((f) => `${f.kind}|${f.name}|${Math.round(f.p95)}|${f.count}`)
    .sort()
    .join("\n");
}

/**
 * Ingest the latest detector findings: increment counts for known signatures,
 * create new candidates for unseen ones, persist, then upload anything that
 * just crossed the local recurrence threshold.
 *
 * Polling-safe: consecutive calls with an identical finding set (same kinds +
 * names + p95 + counts) are treated as the same observation.
 *
 * `keepalive` is passed straight through to the submitters so the exit flush on
 * `pagehide` can outlive the dying document, exactly like the sample and
 * snapshot flushes.
 */
export async function ingestFindings(
  observed: CrossCuttingFinding[],
  opts?: { keepalive?: boolean },
): Promise<CandidateRule[]> {
  // Only the shared vocabulary travels. A kind nobody else spells the same way
  // could never group with the same problem found by another language, so it is
  // dropped here rather than accumulated and uploaded. Dropping (not throwing)
  // is deliberate: bookkeeping must never break a visitor's page.
  const findings = observed.filter((f) => isProblemKind(f.kind));
  if (findings.length === 0) return listSurfaceable(listCandidateRules());
  const key = fingerprintFindings(findings);
  if (key === lastIngestKey) return listSurfaceable(listCandidateRules());
  lastIngestKey = key;
  const now = Date.now();
  const existing = listCandidateRules();
  const byId = new Map(existing.map((c) => [c.id, c]));

  for (const f of findings) {
    const sig = signatureFor(f);
    const id = safeHashId(sig);
    const prev = byId.get(id);
    // WHERE this sighting was. Read from the name the detector already filed
    // the finding under, never from the address bar as it reads while this
    // bookkeeping pass happens to run.
    const where = findingPart(f.name);
    if (prev) {
      byId.set(id, {
        ...prev,
        lastSeenAt: now,
        occurrences: prev.occurrences + 1,
        // The LATEST sighting's answer, whichever of the two it is. Keeping a
        // part named by an earlier sighting beside a newer "could not tell"
        // would leave one record asserting both, and every reader prefers the
        // name — so the store would keep showing a screen this problem is no
        // longer being seen on. One answer, always the freshest.
        routeLabel: where.routeLabel,
        partUnknown: where.partUnknown,
      });
    } else {
      byId.set(id, {
        id,
        signature: sig,
        kind: f.kind,
        severityBucket: bucketSeverity(f.p95),
        firstSeenAt: now,
        lastSeenAt: now,
        occurrences: 1,
        exampleHint: f.hint,
        status: "new",
        ...where,
      });
    }
  }

  // Most-recently-seen first, capped.
  const merged = [...byId.values()].sort((a, b) => b.lastSeenAt - a.lastSeenAt);
  writeAll(merged);

  // ── Fix-resolution detection ──────────────────────────────────────
  // Compare each kind's current worst severity against the worst severity ever
  // recorded. A strict improvement means a fix landed. The stored baseline is
  // lowered ONLY after a successful submit, so a transient network failure
  // retries on the next pass instead of losing the improvement.
  const baselines = readBaselines();
  let baselinesChanged = false;
  const currentWorst: Record<string, SeverityBucket> = {};
  const currentCount: Record<string, number> = {};
  for (const f of findings) {
    const sev = bucketSeverity(f.p95);
    const prev = currentWorst[f.kind];
    if (!prev || SEV_RANK[sev] > SEV_RANK[prev]) currentWorst[f.kind] = sev;
    currentCount[f.kind] = Math.max(currentCount[f.kind] ?? 0, f.count);
  }
  const resolved: {
    ruleId: string;
    kind: string;
    beforeRating: SampleRating;
    afterRating: SampleRating;
    occurrences: number;
    severityBucket: SeverityBucket;
    countBucket: string;
  }[] = [];
  for (const [kind, sev] of Object.entries(currentWorst)) {
    const worst = baselines[kind];
    if (worst && SEV_RANK[sev] < SEV_RANK[worst]) {
      resolved.push({
        ruleId: kind,
        kind,
        beforeRating: severityToRating(worst),
        afterRating: severityToRating(sev),
        occurrences: 1,
        // Privacy-safe "circumstances" so the community matcher can weight this
        // proven fix toward similar pages: the severity the problem held BEFORE
        // the fix, plus the bucketed occurrence count. No raw values, no code.
        severityBucket: worst,
        countBucket: bucketCount(currentCount[kind] ?? 0),
      });
    } else if (!worst || SEV_RANK[sev] > SEV_RANK[worst]) {
      // New or worsened — record/raise the worst-ever baseline now.
      baselines[kind] = sev;
      baselinesChanged = true;
    }
  }
  if (resolved.length > 0 && activeResolutionSubmitter) {
    try {
      const accepted = await activeResolutionSubmitter(resolved, opts);
      if (accepted > 0) {
        for (const r of resolved.slice(0, accepted)) {
          baselines[r.ruleId] = currentWorst[r.ruleId]!;
          baselinesChanged = true;
        }
      }
    } catch {
      // Best-effort. The baseline is left untouched so the improvement is
      // retried on the next observation once connectivity returns.
    }
  }
  if (baselinesChanged) writeBaselines(baselines);

  // Auto-submit: any surfaceable candidate still `new` is a signature that just
  // crossed the local threshold. The submitter is registered for every
  // REGISTERED install (this channel is not gated on the detail-share
  // directive) and cleared by the kill switch or erasure.
  if (activeSubmitter) {
    const toSubmit = listSurfaceable(merged).filter((c) => c.status === "new");
    if (toSubmit.length > 0) {
      const payload = toSubmit.map((c) => ({
        signature: c.signature,
        kind: c.kind,
        severityBucket: c.severityBucket,
        countBucket: c.signature.split(":")[2] ?? "<3",
        occurrences: c.occurrences,
        // WHERE, beside the signature and never folded into it. One of the
        // two, never both: a named part and a term saying we could not name
        // one are answers to the same question and the name is the better one.
        ...(c.routeLabel !== undefined
          ? { routeLabel: c.routeLabel }
          : c.partUnknown !== undefined
            ? { partUnknown: c.partUnknown }
            : {}),
      }));
      try {
        const accepted = await activeSubmitter(payload, opts);
        if (accepted > 0) {
          const submittedIds = new Set(
            toSubmit.slice(0, accepted).map((c) => c.id),
          );
          // Re-read: a concurrent flush (a cadence tick racing a pagehide
          // flush) may have written since `merged` was built, and clobbering
          // that write would resurrect an already-submitted candidate.
          const latest = listCandidateRules();
          const after = (latest.length > 0 ? latest : merged).map((c) =>
            submittedIds.has(c.id) ? { ...c, status: "submitted" as const } : c,
          );
          writeAll(after);
          return listSurfaceable(after);
        }
      } catch {
        // Best-effort. The candidate stays `new` so the next observation
        // retries automatically.
      }
    }
  }

  return listSurfaceable(merged);
}

/** Filter to candidates that have hit the minimum recurrence threshold — one
 *  occurrence is an anecdote, and the claim being made is "recurring". */
export function listSurfaceable(all: CandidateRule[]): CandidateRule[] {
  return all.filter((c) => c.occurrences >= MIN_OCCURRENCES_TO_SURFACE);
}

/** Mark a candidate as promoted locally. Does not edit the shipped rule book —
 *  that is a maintainer release step. */
export function promoteCandidateLocal(id: string): void {
  const next = listCandidateRules().map((c) =>
    c.id === id ? { ...c, status: "promoted-local" as const } : c,
  );
  writeAll(next);
}

/** Mark a candidate as submitted (after a successful server POST). */
export function markCandidateSubmitted(id: string): void {
  const next = listCandidateRules().map((c) =>
    c.id === id ? { ...c, status: "submitted" as const } : c,
  );
  writeAll(next);
}

/** Wipe all candidates AND the severity baselines, and drop the in-memory
 *  dedupe key. Wired into `telemetry.forget()` so nothing Boosthis-shaped is
 *  left behind in the visitor's browser. */
export function clearAllCandidates(): void {
  try {
    storage.remove(STORAGE_KEY);
  } catch {
    // Best-effort.
  }
  try {
    storage.remove(BASELINE_STORAGE_KEY);
  } catch {
    // Best-effort.
  }
  lastIngestKey = null;
}

/** Test/debug helper. */
export const _candidateInternals = {
  STORAGE_KEY,
  BASELINE_STORAGE_KEY,
  MAX_CANDIDATES,
  MIN_OCCURRENCES_TO_SURFACE,
  bucketSeverity,
  bucketCount,
  safeHashId,
};
