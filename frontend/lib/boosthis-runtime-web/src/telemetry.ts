/** Opt-in telemetry client for the web runtime.
 *
 * Faithful (reduced-scope) port of `lib/boosthis-runtime-node/src/telemetry.ts`.
 * `enableTelemetry` registers an invited web app with the central Boosthis
 * server so it appears in the maintainer's /admin dashboard, then reports:
 *
 *   - optional full per-page samples: only in full-telemetry mode, stopped by
 *     `disable()` / `issuesOnly`;
 *   - the PII-filtered perf SNAPSHOT mirror: off in issues-only mode unless
 *     the developer opted in via `shareMeterWithAI` OR the server directive
 *     fired ("Connect AI" pressed in the web dashboard);
 *   - privacy-safe trace SPANS: ride the exact same allow-gate as the snapshot
 *     mirror (code-defined route labels only), off on a private app until
 *     sharing is authorized;
 *   - always-on CRASH reports (for registered apps): a separate channel that
 *     installs once from `enableTelemetry` and reports on its own — only the
 *     kill-switch or `forget()` can stop it;
 *   - always-on recurring-PROBLEM reports and fix outcomes (candidates +
 *     resolutions): the same always-on gate as crashes, fed from the kit's own
 *     live readings by `./reporting` and accumulated by `./candidateRules`.
 *     Nothing on this channel carries a page label — only a hashed
 *     `<kind>:<severityBucket>:<countBucket>` signature and an occurrence
 *     count.
 *
 * The kill-switch (`BOOSTHIS_DISABLED` / `globalThis.__BOOSTHIS_DISABLED__`)
 * and `forget()` behave exactly like the other runtimes.
 */

import { resetPressToScreen } from "./pressToScreen";
import { unwatchedFetch } from "./callWatch";
import { noteOwnEndpoint } from "./edgeCache";
import { assertNoPII, checkNoPII, transmitLabelHasPII } from "./no-pii";
import {
  MAX_PART_NAME,
  normalizeRouteLabel,
  warnPartNameRefusal,
} from "./vitals";
import { isControlHandle } from "./controlHandle";
// The page map's SENDING boundary — labels and pairs of labels, nothing else,
// and only when the host opted in. `pageMap.ts` (the recorder) is not
// imported here on purpose: this file can only reach what that module is
// willing to hand out for the wire.
import { buildPageMapWire } from "./pageMapWire";
import {
  _safeTransmitInternal,
  _safeTransmitTrustedPayload,
  isNoSendResponse,
  type InternalPostOptions,
} from "./transmit";
import { RUNTIME_VERSION, type Rating } from "./thresholds";
import { isBoosthisDisabled } from "./runtimeFlags";
import { setSampleObserver, type Sample } from "./samples";
import {
  setSnapshotSubmitter,
  startSnapshotAutoUpload,
  stopSnapshotAutoUpload,
  uploadPerfSnapshotNow,
  type WebSnapshotPayload,
} from "./snapshot";
import {
  setSpanSubmitter,
  startSpanAutoFlush,
  stopSpanAutoFlush,
  clearBufferedSpans,
  MAX_SPAN_BATCH,
  type TraceSpan,
} from "./spanEmitter";
import { resetRouteOutcomes } from "./routeOutcomes";
import { uninstallLocalStoreTracking } from "./localStore";
import {
  installCrashHandlers,
  setCrashSubmitter,
  uninstallCrashHandlers,
  type CrashReportPayload,
} from "./crashReporter";
import {
  setCandidateSubmitter,
  setResolutionSubmitter,
  clearAllCandidates,
} from "./candidateRules";
import { startReporting, stopReporting } from "./reporting";
import { clearPageViewTally } from "./pageViewTally";
import { storage } from "./storage";
import {
  forgetRegistered,
  rememberRegistered,
  wasRegisteredBefore,
} from "./registrationMemory";
import {
  startEntitlementCheckin,
  stopEntitlementCheckin,
  clearEntitlementCache,
  chaseFirstActivation,
  ensureEntitlementChecked,
} from "./killSwitch";
import {
  describeProjectKeySource,
  resolveProjectKey,
  type ResolvedProjectKey,
} from "./projectKey";
import { announceProjectKey } from "./startAnnounce";
import {
  getKitProject,
  parseKitProject,
  serializeKitProject,
  setKitProject,
} from "./projectIdentity";
import {
  checkRegistrationOnce,
  markRegistrationConfirmed,
  markRegistrationRefused,
  markRegistrationUnreachable,
} from "./registration";
import {
  getRegistrationRefusalKind,
  getRegistrationRefusalSentence,
  registrationFailureHint,
  setRegistrationRefusalKind,
  warnStayedInactiveOnce,
  _resetInactiveNoticeForTests,
  type RegistrationFailure,
} from "./inactiveNotice";
import {
  setRegistrationProbe,
  noteRegistrationAttemptEnded,
  noteRegistrationAttemptStarted,
  noteRegistrationConfirmed,
  noteRegistrationExplained,
  noteRegistrationNotSent,
  noteRegistrationTrouble,
} from "./registrationWatch";

// The inactive-line wording and the refusal it describes now live in their own
// module, because the failures that produce the WORST silence happen outside
// any registration round trip (see registrationWatch.ts). They stay exported
// from here so every existing caller — the barrel, the panel, the status
// readout — keeps reading them from one place.
export {
  getRegistrationRefusalKind,
  getRegistrationRefusalSentence,
} from "./inactiveNotice";

export const DEFAULT_TELEMETRY_ENDPOINT = "https://www.boosthis.com/api";

/** The most recently created telemetry client for this page, or null before
 *  `enableTelemetry` has run. Held so display-only surfaces (the bubble's
 *  dashboard panel + its account card) can read the live endpoint / installId /
 *  read+delete tokens WITHOUT the host having to thread the client through.
 *  Mirror of the RN kit's `getActiveTelemetryClient()`. */
let activeClient: TelemetryClient | null = null;
let activeProjectKey: ResolvedProjectKey = {
  key: null,
  source: "none",
  overrodeShared: false,
  display: null,
  declined: false,
};

export function getActiveProjectKey(): ResolvedProjectKey {
  return activeProjectKey;
}

export function getActiveProjectKeyStatus(): {
  display: string | null;
  description: string;
} {
  return {
    display: activeProjectKey.display,
    description: describeProjectKeySource(activeProjectKey.source),
  };
}

/** The active telemetry client, or null if `enableTelemetry` has not run.
 *  Read-only accessor for display-only surfaces (never mutated by callers). */
export function getActiveTelemetryClient(): TelemetryClient | null {
  return activeClient;
}

/** Whether THIS page asked for its page map to be uploaded (`sendPageMap`).
 *  False until enableTelemetry() runs, and false for every site that never
 *  asked — the default. Read by the bubble so its page-map row says where the
 *  map goes; it reports the host's request, not that an upload happened
 *  (consent, the kill switch, entitlement and issues-only all still decide
 *  that). */
let pageMapSendRequested = false;
/**
 * Apply the dashboard "Full telemetry" directive to the active telemetry client
 * IN-SESSION, using the SAME directive-apply path the consent response uses
 * (`serverFullTelemetry` → sample firehose + snapshot mirror re-wire). The
 * bubble's account card calls this right after its own successful telemetry-
 * toggle POST so the new mode takes effect immediately with no relaunch and no
 * code change. A no-op (returns false) when telemetry was never enabled. Never
 * throws: it delegates to the client method, whose gates already honor
 * disable()/forget()/BOOSTHIS_DISABLED.
 */
export function applyServerFullTelemetry(on: boolean): boolean {
  try {
    const client = activeClient;
    if (!client) return false;
    client.applyServerFullTelemetry(on);
    return true;
  } catch {
    return false;
  }
}

/** Web analogue of RN's `resolveDashboardWebUrl` — the human-facing dashboard
 *  origin derived from the active endpoint (…/api → origin). Falls back to the
 *  default origin when no client is active. Never throws. */
export function resolveDashboardWebUrl(): string {
  try {
    const ep = activeClient?.endpoint ?? DEFAULT_TELEMETRY_ENDPOINT;
    return ep.replace(/\/api\/?$/, "").replace(/\/+$/, "");
  } catch {
    return DEFAULT_TELEMETRY_ENDPOINT.replace(/\/api\/?$/, "");
  }
}

/** @internal test hook — clear the active-client registry between runs. */
export function _resetActiveTelemetryClientForTests(): void {
  activeClient = null;
  pageMapSendRequested = false;
  activeProjectKey = {
    key: null,
    source: "none",
    overrodeShared: false,
    display: null,
    declined: false,
  };
  _resetInactiveNoticeForTests();
}

let warnedProjectKeyOverride = false;

/** Sticky "registration rejected — the install ID is not a UUID" state. Set
 *  ONLY by the server's fixed `invalid_install_id` marker on a 400 consent
 *  response (the server validates `install_id` as a UUID v4). Before this the
 *  case looked exactly like a healthy install to the developer — consent
 *  returned, nothing threw, and the bubble kept saying "awaiting telemetry"
 *  forever. Read by the bubble so the panel shows the rejected state instead
 *  of a normal/awaiting one. Module-level (like the warn guard) so display-only
 *  surfaces can read it without the host threading the client through. */
let installIdRejected = false;

/** Whether the server rejected this app's install ID for not being a UUID.
 *  Display-only read for the bubble; never carries any server text. */
export function isInstallIdRejected(): boolean {
  return installIdRejected;
}

/** One-shot guard so the invalid-install-id rejection prints exactly ONE line
 *  per page — never once per consent attempt. */
let warnedInstallIdRejected = false;

/** Handle a 400 consent response. Returns true when the body carried the
 *  server's FIXED `invalid_install_id` marker — in which case the kit records
 *  the rejected state and prints one code-defined line. It never echoes the
 *  server's text, the install id, or any request/response value, and it never
 *  schedules the hourly rejected-registration retry: re-sending an unchanged
 *  bad id can never succeed, so that loop stays reserved for 401/403 key
 *  rejection. Never throws into the host. */
async function handleInvalidInstallIdOnce(res: Response): Promise<boolean> {
  let invalid = false;
  try {
    const body = (await res.clone().json()) as { error?: unknown };
    invalid = body?.error === "invalid_install_id";
  } catch {
    // Non-JSON / unreadable body — treat as an ordinary 400, not this case.
    invalid = false;
  }
  if (!invalid) return false;
  installIdRejected = true;
  if (warnedInstallIdRejected) return true;
  warnedInstallIdRejected = true;
  try {
    console.warn(
      "[boosthis] Registration rejected: this app's Boosthis install ID is " +
        "not a valid UUID. Generate a real UUID (e.g. uuidgen / " +
        "crypto.randomUUID()), persist it as the install ID, then restart.",
    );
  } catch {
    // A host that replaced console must never crash because of a warning.
  }
  return true;
}

/** Test-only: reset the one-shot registration-warning guards + the sticky
 *  invalid-install-id rejected state. */
export function _resetRegistrationWarningForTests(): void {
  warnedInstallIdRejected = false;
  installIdRejected = false;
  warnedProjectKeyOverride = false;
  _resetInactiveNoticeForTests();
}

// ── "The server stored fewer rows than we sent" ─────────────────────────────
//
// WHY THIS EXISTS. The upload endpoints answer 202 and then discard individual
// rows they cannot store: a route label the privacy guard refuses, a span past
// the per-trace cap, a snapshot entry whose name is rejected. Until now the kit
// read that 202 as unqualified success, so a developer whose every route label
// was being thrown away saw a healthy page, no output, and a dashboard quietly
// missing data. This reads the reply's honesty fields and warns ONCE per cause,
// reusing the same latch style + stream + crash-safety as the "stayed inactive"
// line above — same reason it is ungated: it exists for the developer who does
// not yet suspect the kit.
//
// Contract (identical in every kit): the ABSENCE of the fields (an
// older server, a non-JSON body, an unreadable body, a beacon with no readable
// reply) means "nothing to report" — no warning, no crash, no guessing. Nothing
// is uploaded; the warning is local output only. A cause key the kit does not
// know is counted in the total and named nowhere — never invent an explanation.

/** Why the server threw a row away. Mirrors the server's `DropCause`. */
type DropCause = "labelRejected" | "traceCapReached" | "snapshotEntryFiltered";

const DROP_CAUSE_KEYS: readonly DropCause[] = [
  "labelRejected",
  "traceCapReached",
  "snapshotEntryFiltered",
];

/** Short cause text — the same words the project's web page uses, so the panel
 *  and the dashboard never tell two different stories. */
const DROP_CAUSE_TEXT: Record<DropCause, string> = {
  labelRejected: "route names the privacy guard refused",
  traceCapReached: "spans past the 20-span limit",
  snapshotEntryFiltered: "snapshot entries the privacy guard refused",
};

/** What the developer changes to stop it. Second sentence of the warning. */
const DROP_CAUSE_FIX: Record<DropCause, string> = {
  labelRejected: "Name routes in code, the way GET /orders is written.",
  traceCapReached:
    "A trace keeps its first 20 spans \u2014 measure fewer steps per request, or split a very long trace.",
  snapshotEntryFiltered:
    "Name screens in code, never from what a person typed or an id.",
};

function dropFiniteCount(v: unknown): number {
  if (typeof v !== "number" || !Number.isFinite(v) || v <= 0) return 0;
  return Math.floor(v);
}

// Cumulative for the life of the page: it is the size of the hole in the
// dashboard, so a later clean upload does not erase it.
let droppedRows = 0;
const seenDropCauses = new Set<DropCause>();
const warnedDropCauses = new Set<DropCause>();

/** How many rows the server has refused since this page loaded. */
export function getDroppedRowCount(): number {
  return droppedRows;
}

/** The causes seen so far, in the fixed cause order. Empty when nothing has
 *  been dropped. Display-only; carries code-defined markers, never server
 *  text. */
export function getDroppedRowCauses(): DropCause[] {
  return DROP_CAUSE_KEYS.filter((c) => seenDropCauses.has(c));
}

/** Emit the one-shot line for a cause on stderr via console.warn, ungated,
 *  never repeated — the same latch style as warnStayedInactiveOnce. Never
 *  throws (a host that replaced console must not crash on our warning). */
function warnDropCauseOnce(cause: DropCause, count: number): void {
  if (warnedDropCauses.has(cause)) return;
  warnedDropCauses.add(cause);
  try {
    console.warn(
      `[boosthis] Boosthis dropped ${count} of the measurements this app sent: ` +
        `${DROP_CAUSE_TEXT[cause]}. ${DROP_CAUSE_FIX[cause]}`,
    );
  } catch {
    // A broken console must never take the host down.
  }
}

/** Record what one upload reply said, and warn once per cause. Safe to call
 *  with anything: a reply without the fields, a string, null. Only a body that
 *  reports a positive `dropped` changes any state or prints anything. Never
 *  throws — telling someone about a dropped row must never break an upload. */
export function noteServerDrops(body: unknown): void {
  try {
    if (!body || typeof body !== "object") return;
    const rec = body as Record<string, unknown>;
    const total = dropFiniteCount(rec.dropped);
    if (total <= 0) return;
    droppedRows += total;
    const raw = rec.droppedByCause;
    if (raw && typeof raw === "object") {
      const causes = raw as Record<string, unknown>;
      for (const cause of DROP_CAUSE_KEYS) {
        const n = dropFiniteCount(causes[cause]);
        if (n > 0) {
          seenDropCauses.add(cause);
          warnDropCauseOnce(cause, n);
        }
      }
    }
  } catch {
    // Reading the reply must never fail an upload.
  }
}

/** Read an upload reply's body and record what it says. Fire-and-forget: the
 *  caller does not await it, so a slow or unreadable body can never slow an
 *  upload. A beacon (no readable reply) or a body without the fields reports
 *  nothing, by contract. */
async function readDropsFromResponse(res: unknown): Promise<void> {
  try {
    const r = res as { json?: () => Promise<unknown> } | null;
    if (!r || typeof r.json !== "function") return;
    noteServerDrops(await r.json());
  } catch {
    // No body, not JSON, already consumed — nothing to report, by contract.
  }
}

/** Called ONLY where the server answered 2xx to a MEASUREMENT upload — the one
 *  place the kit already treats a reply as "an upload was accepted". A 202 is
 *  not unqualified success: the server may have stored fewer rows than we sent.
 *  Read the honesty fields WITHOUT awaiting them, off the upload path, so a
 *  slow or unreadable body can never slow an upload. Never throws. */
function noteMeasurementUploadAccepted(res: unknown): void {
  try {
    noteUploadAccepted();
    void readDropsFromResponse(res);
  } catch {
    // reading the reply must never fail an upload
  }
}

export type UploadFailReason =
  | "unauthorized"
  | "rejected"
  | "server-error"
  | "unreachable";

let lastUploadFailAtMs: number | null = null;
let lastUploadFailReason: UploadFailReason | null = null;
let droppedUploads = 0;
let lastAttemptFailed = false;

export function noteUploadAccepted(): void {
  try {
    lastAttemptFailed = false;
  } catch {
    /* a status-mirror write must never fail an upload */
  }
}

export function noteUploadRejected(reason: UploadFailReason): void {
  try {
    lastUploadFailAtMs = Date.now();
    lastUploadFailReason = reason;
    droppedUploads += 1;
    lastAttemptFailed = true;
  } catch {
    /* a status-mirror write must never fail an upload */
  }
}

export function getLastUploadFailure(): {
  at: number;
  reason: UploadFailReason;
} | null {
  if (lastUploadFailAtMs === null || lastUploadFailReason === null) return null;
  return { at: lastUploadFailAtMs, reason: lastUploadFailReason };
}

export function getDroppedUploadCount(): number {
  return droppedUploads;
}

export function isLastUploadAttemptFailed(): boolean {
  return lastAttemptFailed;
}

export function _resetUploadFailureForTests(): void {
  lastUploadFailAtMs = null;
  lastUploadFailReason = null;
  droppedUploads = 0;
  lastAttemptFailed = false;
}

export function _noteUploadResponseForTests(res: Response): boolean {
  try {
    // NOTHING WAS SENT. Every measurement upload lands here to be judged, and
    // when the kill-switch or a killed install silences the POST the transmit
    // helper still has to return a Response, so it invents a 204 — which is
    // `ok`. Judged as a success it would clear a real earlier failure, read
    // "drops" out of a body Boosthis never wrote, and hand the caller a
    // delivered-row count for rows that never left the page: the same false
    // green the registration path had, on six more surfaces.
    //
    // A locally-invented answer is evidence about US, never about the server.
    // It delivers nothing, so the caller counts zero, and it clears nothing.
    // It is not recorded as a refused batch either — the batch was not
    // refused; the kit is switched off, which the kit already says in its own
    // words (the lock notice, the status page's own rows and the registration
    // verdict), and an upload-failure line on top of that would describe a
    // fault that never happened.
    if (isNoSendResponse(res)) return false;
    if (res.ok) {
      noteMeasurementUploadAccepted(res);
      return true;
    }
    noteUploadRejected(
      res.status === 401 || res.status === 403
        ? "unauthorized"
        : res.status >= 500
          ? "server-error"
          : "rejected",
    );
  } catch {
    /* recording the result must never fail an upload */
  }
  return false;
}

function noteUploadUnreachable(): void {
  try {
    noteUploadRejected("unreachable");
  } catch {
    /* recording the result must never fail an upload */
  }
}

/** The panel figure: `"6 \u2014 route names the privacy guard refused; spans
 *  past the 20-span limit"`. Empty string when nothing has been dropped, so a
 *  healthy app shows nothing at all. */
export function dropSummaryText(): string {
  if (droppedRows <= 0) return "";
  const causes = getDroppedRowCauses().map((c) => DROP_CAUSE_TEXT[c]);
  return causes.length > 0
    ? `${droppedRows} \u2014 ${causes.join("; ")}`
    : String(droppedRows);
}

/** Test-only: forget every drop and every drop warn-once latch. */
export function _resetDropReportForTests(): void {
  droppedRows = 0;
  seenDropCauses.clear();
  warnedDropCauses.clear();
}

/** S5 of the silent-failure audit: how long the kit waits before quietly
 *  retrying a REJECTED registration (401/403 — bad/revoked/rotated invite
 *  key). A long-lived tab/SPA used to try exactly once at load and then stay
 *  dark until a reload even after the key was fixed server-side (rekey,
 *  rotation); one quiet retry per hour lets it come back WITHOUT a reload.
 *  Never retries on network failure (traffic-driven consent already covers
 *  that) and never prints again (the warn stays one-shot). */
let consentRetryMs = 60 * 60 * 1000;

/** Test-only: shrink the consent retry interval. */
export function _setConsentRetryMsForTests(ms: number): void {
  consentRetryMs = ms;
}

/** Longest display name the server stores (matches the `.max(60)` on the
 *  consent schema). Clamp client-side so a long name is trimmed rather than
 *  rejected at ingest. */
const APP_NAME_MAX = 60;

/** Sample-upload batching: flush at most this often… */
const SAMPLE_FLUSH_MS = 30_000;
/** …and never ship more than the server's batch cap in one POST. */
const MAX_SAMPLE_BATCH = 100;

/**
 * Decide the display name sent on consent. Unlike Node (which auto-detects
 * from package.json), the web runtime uses ONLY an explicit `opts.appName`:
 * the obvious auto-source — `document.title` — is user-visible page state
 * that routinely embeds dynamic user content ("Inbox (3) — jane@…"), so
 * auto-detection here would be a PII footgun, not a convenience. The explicit
 * value is clamped and dropped (never thrown on) if it trips the PII guard.
 */
export function resolveAppName(
  explicit: string | undefined,
): string | undefined {
  if (typeof explicit !== "string" || !explicit.trim()) return undefined;
  const clamped = explicit.trim().slice(0, APP_NAME_MAX);
  return checkNoPII({ appName: clamped }) === null ? clamped : undefined;
}

export interface TelemetryOptions {
  installId: string;
  endpoint?: string;
  packageVersion?: string;
  /** Optional friendly display name for this app, sent on consent so you can
   *  tell your installs apart in your own Boosthis dashboard. Developer-
   *  authored metadata — never put a user's name, email, or any PII here; the
   *  guard screens the value and DROPS it (registration still succeeds) if it
   *  looks identifying. Capped at 60 chars. */
  appName?: string;
  fetchOptions?: InternalPostOptions;
  /** Previously-issued bearer token (e.g. restored from storage on boot).
   *  When absent the client calls /installs/consent to get one. */
  deleteToken?: string;
  /** Fired whenever the server issues a delete token whose VALUE differs from
   *  the one currently held — first issuance, or fresh credentials replacing a
   *  stale restored token. The runtime does NOT persist the delete token
   *  itself (only the read token + installId), so the host should persist its
   *  own copy here and seed it back via `deleteToken` on the next load. */
  onTokenIssued?: (token: string) => void;
  /** Previously-issued SELF-scoped read token. The runtime also persists it
   *  itself (localStorage, `boosthis:` namespaced), so this option is only
   *  needed to seed a token the host already has. */
  readToken?: string;
  /** Fired whenever the server issues a read token whose VALUE differs from
   *  the one currently held. */
  onReadTokenIssued?: (token: string) => void;
  /** Shared invite key authorizing this install to register. Sent as
   *  `Authorization: Bearer <inviteKey>` on /installs/consent. */
  inviteKey?: string;
  /** Issues-only mode. When true the client NEVER ships per-page samples:
   *  `transmit()` becomes a no-op and samples are never queued. The snapshot
   *  mirror stays off too unless `shareMeterWithAI` (explicit or server
   *  directive) turns it on. */
  issuesOnly?: boolean;
  /** Explicit opt-in to mirror the PII-filtered perf SNAPSHOT (per-page rows +
   *  axes) to the server so the developer's OWN AI can read the live picture
   *  over the hosted MCP live-read tools. Default false. In full mode the
   *  snapshot ships anyway; this flag only matters in issues-only mode. It
   *  never enables the raw per-page sample firehose. */
  shareMeterWithAI?: boolean;
  /** Explicit opt-in to send this site's PAGE MAP — the pages the kit has
   *  seen opened, and the paths between them — alongside the snapshot it
   *  already uploads. Default false: nothing about navigation leaves the
   *  device unless you turn this on.
   *
   *  It carries page LABELS ONLY: the same normalized route labels already on
   *  every sample and span, plus pairs of them with counts. There is no field
   *  on the wire for a control, a gesture, a coordinate or anything read from
   *  the DOM, and no option adds one (see `pageMapWire.ts`).
   *
   *  It rides the snapshot's gates exactly — consent, kill-switch,
   *  entitlement and issues-only all apply, so in issues-only mode this sends
   *  nothing unless `shareMeterWithAI` is also on. */
  sendPageMap?: boolean;
  /** Opt in to the crash reporter's DETAILED mode. Default false. When false,
   *  a crash report carries only the always-on closed schema (error name, a
   *  code-derived hashed signature, a redacted top frame, a bucketed count).
   *  When true it also adds a PII-scrubbed first message line + sanitized
   *  frames (function + file basename + line/col). Both modes pass the shared
   *  PII guard before upload. */
  crashDetails?: boolean;
}

export interface TelemetrySample {
  routeLabel: string;
  durationMs: number;
  rating: Rating;
  ruleId?: string;
}

/** One recurring-problem signature. Everything here is built by the kit from
 *  the shared vocabulary plus two buckets — there is no route label, no URL, no
 *  value and no code on this channel. Shape is identical in every kit. */
export interface CandidateSignaturePayload {
  signature: string;
  kind: string;
  severityBucket: "low" | "med" | "high";
  countBucket: string;
  occurrences: number;
}

/** Privacy-safe fix-resolution signal. NEVER carries code, diffs, page names,
 *  or values — only the kind + before→after rating, plus the bucketed
 *  circumstances the problem held BEFORE the fix. */
export interface ResolutionPayload {
  ruleId: string;
  kind: string;
  beforeRating: "good" | "needs-work" | "poor";
  afterRating: "good" | "needs-work" | "poor";
  occurrences: number;
  severityBucket?: "low" | "med" | "high";
  countBucket?: string;
}

export interface TelemetryClient {
  readonly installId: string;
  readonly endpoint: string;
  readonly enabled: boolean;
  readonly deleteToken: string | null;
  /** SELF-scoped read token for this install, or null until consent has
   *  issued one (or it was restored from storage / `opts.readToken`).
   *  Read-only: it can read this install's own uploaded perf data over the
   *  hosted MCP live-read path, but can never delete or ingest. */
  readonly readToken: string | null;
  /** Effective project key decision, including its safe display and source. */
  readonly projectKey: ResolvedProjectKey;
  /** The EFFECTIVE full-telemetry mode right now: code config (`issuesOnly:
   *  false`) OR the dashboard "Full telemetry" directive. The bubble's
   *  signed-in account card reads this so its telemetry toggle can fall back to
   *  the kit's current mode when the claim response omits `fullTelemetry`. */
  readonly fullTelemetryEffective: boolean;
  /** Whether ANY meter is actually being shared to the server dashboard right
   *  now: full mode (code config OR the server "Full telemetry" grant) OR the
   *  `shareMeterWithAI` opt-in (explicit OR the server directive). Display-only,
   *  read by the bubble's panel so a registered-but-silent install can SAY it
   *  is silent instead of looking healthy while the dashboard stays empty. It
   *  reflects the sharing CHOICE, not the transient kill-switch/disable state. */
  readonly meterSharing: boolean;
  /** Apply the dashboard "Full telemetry" directive IN-SESSION — the SAME code
   *  path postConsent() runs when the consent response carries `fullTelemetry`.
   *  The account card calls this after its own successful toggle POST so the
   *  new mode takes effect immediately (no relaunch): ON forces full mode for
   *  the session (sample firehose + snapshot mirror); OFF reverts to the
   *  code-configured mode. Never overrides disable()/forget()/BOOSTHIS_DISABLED.
   */
  applyServerFullTelemetry(on: boolean): void;
  consent(): Promise<number>;
  /** Drop the install credential this page is holding and ask Boosthis for a
   *  working one, returning the fresh credential (or null when none was
   *  issued).
   *
   *  WHY: a second browser or tab that re-consents for this same install is
   *  handed the install's credential back, and that re-issue ROTATES — the
   *  copy still holding the previous one silently loses the ability to prove
   *  itself. Uploads are not the visible casualty; the panel's own actions
   *  are (linking the project, changing the telemetry grant), and they come
   *  back refused with nothing in the kit reacting. The ordinary consent path
   *  — same install id, same project key — hands this page a working
   *  credential back, so the panel calls this ONCE on a refusal and retries.
   *
   *  Deliberately narrow. It does nothing unless a credential is actually
   *  held (a page holding none was not overtaken by anything, so a refusal
   *  there has another cause), and it can NEVER mint a new identity: a
   *  re-consent that finds nothing to restore leaves this page exactly as it
   *  was rather than registering a second install under the same project. */
  renewCredential(): Promise<string | null>;
  transmit(
    samples: readonly TelemetrySample[],
    opts?: { keepalive?: boolean },
  ): Promise<number>;
  /** Upload one PII-filtered perf snapshot. Gated off in issues-only mode
   *  unless `shareMeterWithAI` (explicit or server directive) is set; always
   *  gated off when disabled or killed. Pass `keepalive: true` from a
   *  pagehide handler so the request outlives the page. Returns 1 on
   *  success. */
  transmitSnapshot(
    snapshot: WebSnapshotPayload,
    opts?: { keepalive?: boolean },
  ): Promise<number>;
  /** Upload a batch of privacy-safe crash reports. ALWAYS-ON for a registered
   *  app (not gated by `disable()`/`issuesOnly`): only the kill-switch or
   *  `forget()` can stop it. Returns the number of reports accepted. */
  transmitCrashes(crashes: readonly CrashReportPayload[]): Promise<number>;
  /** Upload a batch of recurring-problem signatures. ALWAYS-ON for a registered
   *  app, exactly like `transmitCrashes`: not gated by `disable()`/`issuesOnly`,
   *  stopped only by the kill-switch or `forget()`. Pass `keepalive: true` from
   *  a pagehide handler so a closing page still delivers its report. Returns
   *  the number of signatures accepted. */
  transmitCandidates(
    signatures: readonly CandidateSignaturePayload[],
    opts?: { keepalive?: boolean },
  ): Promise<number>;
  /** Upload a batch of fix outcomes. Same always-on gate as
   *  `transmitCandidates`. Returns the number of resolutions accepted. */
  transmitResolutions(
    resolutions: readonly ResolutionPayload[],
    opts?: { keepalive?: boolean },
  ): Promise<number>;
  /** Upload a batch of privacy-safe trace spans. Rides the same allow-gate as
   *  the snapshot mirror (off on a private app until sharing is authorized).
   *  Returns the number of spans accepted. */
  transmitSpans(spans: readonly TraceSpan[]): Promise<number>;
  disable(): void;
  enable(): void;
  forget(): Promise<number>;
}

/** Mirrors the server's consent-time installId validation (zod `.uuid()` — any
 *  8-4-4-4-12 hex layout, case-insensitive). Consent is fire-safe, so a
 *  wrong-shaped id would otherwise fail SILENTLY server-side (the app runs but
 *  never registers). Validating here fails loud at the one moment the
 *  developer is looking. */
const INSTALL_ID_SHAPE =
  /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/**
 * UUIDv4 generator for the self-healing reinstall recovery. Prefers the
 * platform's crypto.randomUUID (modern browsers/Node) and falls back to a
 * Math.random v4 — acceptable HERE because the rotated install id is an
 * IDENTIFIER, not an authenticator ("install ids are identifiers, not
 * authenticators" is a server-side invariant): every capability still rides
 * the server-minted delete/read tokens. Exported for tests.
 */
export function generateRotationInstallId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c && typeof c.randomUUID === "function") {
    try {
      return c.randomUUID();
    } catch {
      /* fall through to the dependency-free path */
    }
  }
  let out = "";
  for (const ch of "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx") {
    if (ch === "x") out += ((Math.random() * 16) | 0).toString(16);
    else if (ch === "y") out += (((Math.random() * 4) | 0) + 8).toString(16);
    else out += ch;
  }
  return out;
}

/**
 * The widening ladder a registration that never completed is chased on.
 *
 * At module scope, and exported, for one reason: it is the HORIZON the kit
 * promises a developer when it says a failed registration is not final. A
 * ladder buried inside `enableTelemetry` could be lengthened without anyone
 * noticing that the sentence still says ten minutes, which is the same class
 * of mistake as the 36-second verdict this wording replaced — a promise made
 * on one surface and kept somewhere else entirely. A test holds the two
 * together.
 *
 * Cumulative: 15s, 1m, 3m, 8m. Quick enough to catch the common brief outage,
 * slow enough that a page left open all day on a dead network costs almost
 * nothing.
 */
export const CONSENT_CHASE_LADDER_MS = [15_000, 45_000, 120_000, 300_000];

/**
 * WHAT THIS KIT IS READY TO SEND BEFORE ANYONE HAS DECIDED — the sharing
 * posture of a brand-new install in the window between this process starting
 * and the server's first answer.
 *
 * Declared ONCE for the whole product in lib/telemetry-mode-contract.json.
 * This is the copy this kit reads, and a guard reads the literal back out of
 * this file and fails when the two diverge without a written reason. A NAMED
 * constant rather than a bare literal beside `opts.issuesOnly`, so the
 * pre-contact answer is a stated position somebody can find rather than the
 * same word hardcoded once per kit with nothing holding the copies together.
 *
 * Nothing can actually leave the device in that window regardless — every
 * upload path also needs the delete token the consent answer mints — so this
 * decides what the kit is READY to send, not what it has sent.
 */
export const PRE_CONTACT_FULL_TELEMETRY_DEFAULT = true;

export function enableTelemetry(opts: TelemetryOptions): TelemetryClient {
  if (!opts.installId || typeof opts.installId !== "string") {
    throw new Error("enableTelemetry requires opts.installId (UUIDv4 string)");
  }
  if (!INSTALL_ID_SHAPE.test(opts.installId)) {
    throw new Error(
      "enableTelemetry: opts.installId must be a UUID (8-4-4-4-12 hex, e.g. crypto.randomUUID()) — the server rejects any other shape at registration, so the install would silently never appear",
    );
  }
  const endpoint = opts.endpoint ?? DEFAULT_TELEMETRY_ENDPOINT;
  // Our own sends go out through the page's fetch, which the call watcher
  // wraps. Registering the endpoint keeps OUR replies out of the app's
  // hosting-cache reading.
  try {
    noteOwnEndpoint(endpoint);
  } catch {
    /* observing must never disturb the page */
  }
  const projectKey = resolveProjectKey(opts.inviteKey);
  activeProjectKey = projectKey;
  // The module install supplies the key HERE, one statement after the kit's
  // start call. Hand it to the startup line before it gives up waiting, so the
  // anchor line names the key instead of claiming there is none.
  try {
    announceProjectKey(projectKey.key);
  } catch {
    /* evidence never blocks reporting */
  }
  // NO KEY IS A STATE, NOT A SILENCE.
  //
  // A page with no project key measures itself perfectly and appears nowhere:
  // no project row on the owner's dashboard, so no chart and no alert about
  // this app can ever exist. That is a supported mode — but it is IDENTICAL,
  // from the outside, to a key that simply failed to arrive, which is how a
  // real customer's site stayed invisible. So the kit records which of the two
  // this is the moment it knows, and the panel and status readout show it from
  // here on. (The console line waits for the first round trip to settle, so a
  // sharper fault can claim the page's one line — see postConsent.)
  if (!projectKey.key) {
    setRegistrationRefusalKind(
      projectKey.declined ? "no-key-chosen" : "no-key-missing",
    );
    // Backstop for the page that never gets as far as a round trip (no gate
    // accepted, consent never attempted, the tab closed early). Without it the
    // console line would depend on a call that may never happen — and silence
    // is the exact failure this whole change exists to end. Long enough that a
    // real round trip wins the line; the latch makes it a no-op if it did.
    try {
      const t = setTimeout(() => {
        warnNoProjectKeyIfStillUnexplained();
      }, NO_KEY_ANNOUNCE_DELAY_MS);
      // Never hold a Node-side test process (or an SSR render) open for this.
      const handle = t as unknown as { unref?: () => void };
      if (typeof handle.unref === "function") handle.unref();
    } catch {
      /* a host without timers must not fail to start */
    }
  }
  if (projectKey.overrodeShared && !warnedProjectKeyOverride) {
    warnedProjectKeyOverride = true;
    try {
      console.warn(
        `[boosthis] Web project key overridden by __BOOSTHIS_PROJECT_KEY_WEB__ (${projectKey.display}).`,
      );
    } catch {
      /* a host that replaced console must not break registration */
    }
  }
  const packageVersion = opts.packageVersion ?? RUNTIME_VERSION;
  // OUR OWN UPLOADS ARE NOT THE APP'S CALLS.
  //
  // The call watcher wraps the page's `fetch` so a hanging backend call is
  // visible. Everything below goes out through that same `fetch`, so left
  // alone the kit would file its own registration, snapshots and spans as one
  // of the app's backend destinations — and in the app this reading exists
  // for, a front end with no backend of its own, ours could be the only
  // destination reported. Send under our own wrapper instead, resolved per
  // call so it is right whichever order the host starts things in. A host's
  // own `fetchImpl` always wins, and when nothing is wrapped this is the
  // page's ordinary `fetch`.
  const fetchOptions: InternalPostOptions = {
    ...(opts.fetchOptions ?? {}),
    fetchImpl:
      opts.fetchOptions?.fetchImpl ??
      (((input: unknown, init?: unknown) =>
        (unwatchedFetch() ?? fetch)(
          input as RequestInfo,
          init as RequestInit,
        )) as unknown as typeof fetch),
  };
  const issuesOnly = opts.issuesOnly ?? !PRE_CONTACT_FULL_TELEMETRY_DEFAULT;
  const explicitShareMeterWithAI = opts.shareMeterWithAI ?? false;
  // Off unless the host says otherwise. There is no server directive that
  // turns this on: the page map is the host's decision alone.
  const sendPageMap = opts.sendPageMap ?? false;
  // Remembered for the bubble, whose page-map row must not read as a purely
  // on-device record once the host has asked for it to be sent.
  pageMapSendRequested = sendPageMap;
  let serverShareMeterWithAI = false;

  // SECOND server directive — the dashboard "Full telemetry" switch, learned
  // from the consent response OR applied in-session by the bubble's account
  // card (applyServerFullTelemetry). A THREE-STATE value that follows BOTH
  // edges:
  //   • null  = the server has not told us yet — honour the code-configured
  //             mode (`issuesOnly`) until first contact.
  //   • true  = ON  — force full mode for the session (per-page sample firehose
  //             + snapshot mirror), even when the host code asked for
  //             issues-only (preserves the documented contract).
  //   • false = OFF — force issues-only for the session, even when the host
  //             code asked for full mode (fixes the defect where a dashboard
  //             "turn it off" was silently ignored).
  // It can never override disable()/forget()/the kill-switch — every upload
  // path re-checks `enabled` + the kill-switch. Initialised to null (NOT false)
  // so it never fabricates an OFF the server never sent.
  let serverFullTelemetry: boolean | null = null;

  /** The mode uploads actually run under: before the server has spoken
   *  (`serverFullTelemetry === null`) the code-configured `issuesOnly` wins;
   *  after first contact the server directive wins in BOTH directions. Inverse
   *  of effective full mode
   *  (`serverFullTelemetry === null ? !issuesOnly : serverFullTelemetry`).
   *  Mirrors Node/RN. */
  function effectiveIssuesOnly(): boolean {
    return serverFullTelemetry === null ? issuesOnly : !serverFullTelemetry;
  }

  /** SAY IT ONCE when this project's dashboard directive contradicts what the
   *  installed code asked for.
   *
   *  The server wins in both directions and always has; that is deliberate
   *  and this warning does not dispute it. What it ends is a developer
   *  reading their own source, seeing the mode they set, and believing it
   *  while the switch on their project's page quietly says otherwise. BOTH
   *  values are named, so the line is actionable without first opening the
   *  dashboard to find out what it currently says.
   *
   *  One wording in every kit that warns — see
   *  lib/telemetry-mode-contract.json, which is also where a kit that does
   *  NOT warn has to write down why. */
  let telemetryModeContradictionAnnounced = false;
  function noteTelemetryModeContradiction(): void {
    if (telemetryModeContradictionAnnounced) return;
    // Nothing to contradict until the server has actually answered: before
    // first contact the code's setting is the only one there is.
    if (serverFullTelemetry === null) return;
    const requested = issuesOnly ? "reduced" : "full";
    const effective = effectiveIssuesOnly() ? "reduced" : "full";
    if (requested === effective) return;
    telemetryModeContradictionAnnounced = true;
    try {
      // NAMES THE ANSWER, NEVER THE SWITCH'S POSITION. A disconnected or
      // removed project is answered with the sharing directives withdrawn
      // while its switch may still be ON, so a line quoting that switch
      // would be false exactly where a developer most needs it. The kit
      // knows what it was told; it does not know why.
      console.warn(
        `[boosthis] telemetry mode: your code asked for ${requested} telemetry, but this project's server answer is ${effective}, so ${effective} is what is in force. The server answer wins in both directions: the "Full telemetry" switch on the project's page sets it, and a disconnected or removed project is answered with reduced until it is reconnected. Change it there, or drop the code setting so the two agree.`,
      );
    } catch {
      /* a host that replaced console must not break telemetry */
    }
  }

  let enabled = true;
  let deleteToken: string | null = opts.deleteToken ?? null;

  // SELF-scoped read token, persisted under a per-install key so multiple
  // installs sharing one origin never collide. Web storage is synchronous, so
  // the restore happens before the first consent round-trip — we never
  // overwrite a token already provided via opts.
  const readTokenStoreKey = `readToken:${opts.installId}`;
  let readToken: string | null = opts.readToken ?? null;
  if (!readToken) {
    const restored = storage.get(readTokenStoreKey);
    if (restored) {
      readToken = restored;
    }
  }
  // WHICH PROJECT this install feeds. Restore it beside the read token so the
  // panel can still name the project before this page's consent reply lands.
  const projectStoreKey = `project:${opts.installId}`;
  try {
    const storedProject = parseKitProject(storage.get(projectStoreKey));
    if (storedProject) setKitProject(storedProject.name, storedProject.code);
  } catch {
    // Best-effort: unreadable browser storage only delays the project name.
  }

  // ─── HOURLY CONSENT RETRY AFTER A REJECTED REGISTRATION (S5) ─────────────
  // One pending timer at most; re-armed only by another rejection. Guarded
  // `.unref()` for the Node-side entrypoints (a browser timer id has none).
  // Stopped on success, disable(), and forget(); the callback re-checks
  // enabled + kill-switch at fire time so a late tick can never resurrect a
  // stopped runtime.
  let consentRetryTimer: ReturnType<typeof setTimeout> | null = null;

  function stopConsentRetry(): void {
    consentChaseStep = 0;
    if (consentRetryTimer !== null) {
      clearTimeout(consentRetryTimer);
      consentRetryTimer = null;
    }
  }

  function scheduleConsentRetry(afterMs: number = consentRetryMs): void {
    if (consentRetryTimer !== null) return;
    consentRetryTimer = setTimeout(() => {
      consentRetryTimer = null;
      if (!enabled || isBoosthisDisabled()) return;
      void postConsent().catch(() => {
        // postConsent only throws on PII-guard bugs; never crash the host.
      });
    }, afterMs);
    const t = consentRetryTimer as unknown as { unref?: () => void };
    if (typeof t.unref === "function") t.unref();
  }

  // ─── KEEP TRYING AFTER A REGISTRATION THAT NEVER COMPLETED ───────────────
  // The hourly loop above is for a key the SERVER refused: nothing changes
  // until somebody fixes it there, so asking more often is just noise. A
  // registration that never completed is the opposite case — a tab opened in a
  // tunnel, a proxy down for a minute, a 5xx mid-deploy — and it usually clears
  // by itself within seconds. Giving up after one attempt is what left a real
  // install unregistered for its whole session.
  //
  // A widening ladder, not a fixed interval: quick enough to catch the common
  // brief outage, slow enough that a page left open all day on a dead network
  // costs almost nothing.
  let consentChaseStep = 0;

  function scheduleConsentChase(): void {
    const idx = Math.min(consentChaseStep, CONSENT_CHASE_LADDER_MS.length - 1);
    const step = CONSENT_CHASE_LADDER_MS[idx] ?? 300_000;
    consentChaseStep += 1;
    // Clamped by the hourly rhythm so ONE test seam compresses both loops —
    // otherwise a test proving the chase would have to sleep for real seconds.
    scheduleConsentRetry(Math.min(step, consentRetryMs));
  }

  // ─── SELF-HEALING REINSTALL RECOVERY (identity rotation) ──────────────────
  // A redeployed app (or one whose storage was cleared) keeps its host-baked
  // installId but loses its stored tokens, so its tokenless re-consent gets
  // the idempotent orphan response ("already_registered_no_token") forever.
  // Because a FRESH registration with a valid invite key is already permitted
  // (that is exactly what a brand-new install does), the kit recovers on its
  // own: mint a fresh random installId, persist the mapping in kit-owned
  // storage (keyed by the baked id so relaunches that re-pass the baked id
  // keep resolving to the rotated identity), and re-register under the new
  // identity. Server capability is UNCHANGED — no new endpoint, no weakened
  // proof rule; the old row's credentials stay dead and the row goes dormant.
  //
  // Guard rails (parity with the RN/Node kits):
  //   • fires ONLY on the orphan status, with an invite key in hand, and with
  //     no tokens (a Repair-window response carries tokens and takes the
  //     normal adoption path — rotation can never race an owner's Repair);
  //   • at most ONE rotation per session (no retry loops);
  //   • a persisted mapping is adopted only if it is UUID-shaped;
  //   • forget() erases the mapping (leave nothing Boosthis-shaped behind).
  // Web storage is synchronous, so the persisted mapping is adopted inline —
  // before the first network call, with no async gate needed.
  let activeInstallId = opts.installId;
  let rotatedThisSession = false;
  const rotationStoreKey = `rotatedInstallId:${opts.installId}`;
  {
    const stored = storage.get(rotationStoreKey);
    if (
      typeof stored === "string" &&
      INSTALL_ID_SHAPE.test(stored) &&
      stored !== opts.installId
    ) {
      activeInstallId = stored;
    }
  }

  /** (Re)install the server-authority kill-switch heartbeat for the CURRENT
   *  identity. startEntitlementCheckin captures installId by value, so every
   *  identity change (in-session rotation) must repoint it — otherwise the
   *  heartbeat knocks with the old id + the new token and the activation lock
   *  never releases. `getToken` is lazy so the token minted by consent (read
   *  OR delete) is picked up on the next check. This is the VAULT contract's
   *  on-launch enforcement edge: the first check runs a FORCED launch check-in
   *  (bypassing any cache-satisfied fast path). Web storage is synchronous, so
   *  the persisted-rotation mapping is already adopted above — no async gate. */
  function installCheckinConfig(): void {
    startEntitlementCheckin({
      endpoint,
      installId: activeInstallId,
      getToken: () => deleteToken ?? readToken,
      kitVersion: packageVersion,
      integrity: null,
      fetchImpl: fetchOptions.fetchImpl,
    });
  }

  async function postConsent(consentOpts?: {
    /** Whether the orphan answer may mint a FRESH identity (see the
     *  self-healing block below). True everywhere except a deliberate
     *  credential renewal, which is repairing ONE known install and must
     *  never turn a refused credential into a second install. */
    allowRotation?: boolean;
  }): Promise<number> {
    let res: Response;
    const displayName = resolveAppName(opts.appName);
    // An attempt is now on the record BEFORE it is made, so the watch that
    // reconciles the startup line can tell "we asked and heard nothing" from
    // "we never asked" — the two states this bug used to blur into one.
    noteRegistrationAttemptStarted();
    try {
      res = await _safeTransmitInternal(
        `${endpoint}/installs/consent`,
        {
          installId: activeInstallId,
          runtime: "web",
          packageVersion,
          // WHAT THIS BUILD'S CODE ASKED FOR — never what is in effect. The
          // server's own switch still wins in both directions and nothing here
          // changes that; sending this is what lets the owner's page show the
          // two answers side by side, instead of describing a code setting it
          // has never been told. See lib/telemetry-mode-contract.json.
          telemetryMode: issuesOnly ? "reduced" : "full",
          ...(displayName ? { appName: displayName } : {}),
        },
        // Consent is the step that LEADS to activation, so it must be allowed
        // to send while the kit is merely LOCKED (never handshaken). A KILLED
        // install (revoked / unpaid / grace-expired) is still silenced.
        { ...fetchOptions, allowWhenLocked: true },
        projectKey.key ? `Bearer ${projectKey.key}` : undefined,
        // Proof of control for the one-time read-token backfill: the
        // Authorization header carries the invite key, so the delete token
        // rides in this dedicated internal header (applied AFTER the PII
        // guard). Omitted on first consent — the fresh-register path mints
        // the read token unconditionally.
        deleteToken ? { "X-Boosthis-Install-Token": deleteToken } : undefined,
      );
    } catch {
      noteRegistrationAttemptEnded();
      // Network/transport failures return 0 instead of propagating. PII
      // failures still propagate — they indicate a programming bug. A thrown
      // transport (DNS, TLS, a broken certificate store, a blocking proxy)
      // means the kit could not reach the API at all: say so ONCE. This is the
      // silent-failure case that cost a real install ~19 minutes. Traffic-
      // driven consent will keep retrying; the one-shot guard keeps it quiet.
      warnStayedInactiveOnce("network");
      // We could not ask, so we do not know. "Cannot tell right now" — never a
      // failed install (the panel used to render this as "not registered").
      markRegistrationUnreachable();
      noteRegistrationTrouble();
      // …and come back to it. A tab that opened offline must register when it
      // can, rather than staying dark until something else happens to trigger
      // a consent.
      scheduleConsentChase();
      return 0;
    }
    noteRegistrationAttemptEnded();
    // NOTHING WAS SENT.
    //
    // When the kill-switch or the activation lock silences this POST, the
    // transmit helper still has to return a Response, so it invents a 204 — and
    // a 204 is `ok`. Every branch below therefore used to read a request that
    // never left the browser as a registration the SERVER had accepted, and
    // `markRegistrationConfirmed()` published that as fact to the panel, the
    // status readout and the kit's own claim about itself. A false green is
    // worse than the silence it hid: it cannot even be re-tested honestly.
    //
    // A locally-invented answer says nothing about the server, so it gets its
    // own honest state and no confirmation at all.
    if (isNoSendResponse(res)) {
      noteRegistrationNotSent({ silent: isBoosthisDisabled() });
      return 0;
    }
    if (res.ok) {
      // Registration accepted — cancel any pending rejected-registration
      // retry so the timer never fires a redundant consent.
      stopConsentRetry();
      // A registration the server ACCEPTED proves the install id is valid, so
      // discard any earlier invalid-install-id rejected state (a developer who
      // saw the rejection, minted a real UUID, and re-registered on the same
      // page must get the normal dashboard back, not a stale rejection).
      installIdRejected = false;
      // An accepted consent is the server telling us this install IS on file —
      // the strongest possible answer to the panel's registration question, and
      // one that outlives whatever this browser happens to have stored.
      markRegistrationConfirmed();
      // …and write that down where it outlives the page. Registration is
      // durable server-side; every local surface described ONE page load, so
      // a reload of a healthy install started again from "announced, never
      // asked" and presented a days-old success as a fresh failure.
      rememberRegistered(activeInstallId);
      // The startup line promised this. Say so if the promise had already been
      // broken once — a developer who read the failure line must be told when
      // it clears, or a tab that started offline looks broken forever.
      noteRegistrationConfirmed();
      try {
        const body = (await res.json()) as {
          deleteToken?: unknown;
          readToken?: unknown;
          shareMeterWithAI?: unknown;
          fullTelemetry?: unknown;
          status?: unknown;
          projectName?: unknown;
          projectCode?: unknown;
        };
        // WHICH PROJECT this key belongs to. The developer-authored name is
        // re-sanitised by projectIdentity, persisted, and rendered as text only.
        // Older servers omit both fields, which must leave known state intact.
        if (body?.projectName != null || body?.projectCode != null) {
          setKitProject(body.projectName, body.projectCode);
          try {
            storage.set(
              projectStoreKey,
              serializeKitProject(getKitProject()),
            );
          } catch {
            // Best-effort: the next consent reply can restore display state.
          }
        }
        // Adopt any server-issued delete token and — critically — notify the
        // host whenever the VALUE changes, not just the first time one is
        // issued. A page can come back holding a STALE persisted token
        // (storage restored from an older copy, or a regenerated installId
        // with old storage kept); the server then mints fresh credentials
        // (fresh registration, or an owner-approved repair re-issue). If the
        // host is only told on first issuance, it keeps re-persisting the
        // stale token and every reload is broken again — so fire on change.
        const issued =
          typeof body?.deleteToken === "string" ? body.deleteToken : null;
        if (issued) {
          const changed = issued !== deleteToken;
          deleteToken = issued;
          if (changed) opts.onTokenIssued?.(issued);
        }
        const readIssued =
          typeof body?.readToken === "string" ? body.readToken : null;
        if (readIssued) {
          const changed = readIssued !== readToken;
          readToken = readIssued;
          storage.set(readTokenStoreKey, readIssued);
          if (changed) opts.onReadTokenIssued?.(readIssued);
        }
        // Server directive: the developer connected an AI from the web
        // dashboard, so auto-enable the PII-filtered snapshot mirror (even in
        // issues-only mode). Rising-edge only — it never turns the mirror off
        // and never touches the sample firehose.
        if (body?.shareMeterWithAI === true && !serverShareMeterWithAI) {
          serverShareMeterWithAI = true;
          syncSnapshotUpload();
        }
        // Dashboard "Full telemetry" directive — BOTH edges (parity with
        // Node/RN). ON forces full mode for the session; OFF forces issues-only
        // for the session. The first boolean the server sends flips the
        // three-state `serverFullTelemetry` off its initial null, so from then
        // on the server wins in BOTH directions. Only wires the snapshot mirror
        // on/off here; the per-page sample firehose is gated live via
        // effectiveIssuesOnly() in onRecordedSample()/transmit(). Never
        // resurrects a disabled runtime — syncSnapshotUpload() self-guards on
        // enabled + kill-switch.
        if (
          typeof body?.fullTelemetry === "boolean" &&
          body.fullTelemetry !== serverFullTelemetry
        ) {
          serverFullTelemetry = body.fullTelemetry;
          syncSampleObserver();
          syncSnapshotUpload();
        }
        // Both answers are known here for the first time — outside the block
        // above, so a response that merely REPEATS a directive we already hold
        // still gets the developer told. The helper self-guards on the
        // pre-contact state and says it once.
        noteTelemetryModeContradiction();
        // SELF-HEALING: the server says this install id is registered but we
        // hold no proof (orphaned identity — a redeploy/storage wipe lost the
        // stored tokens). If we have an invite key and this response carried
        // NO tokens (a Repair-window response carries tokens and was adopted
        // above — never rotate over a Repair), mint a FRESH identity and
        // re-register under it. Once per session, mapping persisted so every
        // later launch resolves the baked id to the rotated identity.
        if (
          body?.status === "already_registered_no_token" &&
          !deleteToken &&
          !readToken &&
          projectKey.key &&
          !rotatedThisSession &&
          consentOpts?.allowRotation !== false
        ) {
          rotatedThisSession = true;
          const rotated = generateRotationInstallId();
          // Best-effort persistence — storage.set degrades to memory itself,
          // so a blocked store means a session-only rotation, never a throw.
          storage.set(rotationStoreKey, rotated);
          activeInstallId = rotated;
          // Keep the persisted "installId" (used by the Node-side MCP
          // entrypoint) pointing at the identity that actually owns the data.
          storage.set("installId", rotated);
          // Repoint the server-authority kill-switch heartbeat at the rotated
          // identity so it knocks with the id that actually owns the data (and
          // the new token) — otherwise the activation lock would never release.
          installCheckinConfig();
          // Re-register under the fresh identity — this is a normal fresh
          // registration and mints fresh credentials via the standard path.
          return postConsent();
        }
        if (body?.status === "already_registered_no_token" && !deleteToken) {
          // The orphan marker rode in on an HTTP 200, so the success branch
          // would otherwise walk straight past it: the install looks connected,
          // uploads nothing, and says nothing. Self-heal already tried (or
          // could not — no invite key, or already rotated once this page) and
          // the answer is still the orphan marker, so this is NOT a working
          // registration. Say it once and stop; never loop.
          setRegistrationRefusalKind("orphan");
          warnStayedInactiveOnce("orphan");
        }
      } catch {
        // ignore malformed body — server health is the source of truth
      }
      // Consent succeeded and (typically) minted a token, so the kit can now
      // learn its true entitlement — a revoked / unpaid verdict must land
      // immediately (and release the activation lock for an active project)
      // without waiting for the 6h heartbeat.
      //
      // This used to force a check unconditionally, which meant a returning
      // visitor asked the SAME question twice on every launch: once when the
      // heartbeat started with the credential already in storage, and again
      // here. Now it asks only when the question is new — which is exactly the
      // first-ever visitor, whose launch check had no credential to ask with,
      // and an identity rotation, which mints a fresh pair.
      ensureEntitlementChecked();
      // …and if that first answer does not confirm the install (the server can
      // legitimately still be finishing the registration behind it), keep
      // asking on a short ladder. Without it a correct install stayed dark,
      // and invisible, until the next launch — 22 minutes on the project that
      // reported it.
      chaseFirstActivation();
    } else if (res.status === 400) {
      // The server validates `install_id` as a UUID v4 and answers 400 with a
      // fixed `invalid_install_id` marker when it is not. Record the rejected
      // state + print one code-defined line; do NOT arm the hourly retry (the
      // id cannot change by itself, so retrying can never succeed). Any other
      // 400 falls through untouched.
      await handleInvalidInstallIdOnce(res);
      // A refused id is a definite negative: nothing is on file under it.
      markRegistrationRefused();
      // Explained already, in that path's own fixed sentence — so the watch
      // must close the promise quietly rather than add a second, vaguer line.
      noteRegistrationExplained();
    } else if (res.status === 401 || res.status === 403) {
      // Registration was rejected (bad/revoked invite key). The Web runtime
      // has no locked-screen UI, so surface a one-time plain-English hint —
      // then keep quietly knocking once an hour so a key fixed server-side
      // (rekey / rotation) brings this app back without a page reload. An
      // `invite_key_revoked` marker in the body sharpens the wording.
      //
      // The marker splits three situations that used to be one shrug, and that
      // need opposite actions: a revoked key is dead forever, a paused one
      // needs no code change at all, and an unrecognised one usually means the
      // key the developer meant is still fine and simply mistyped. Only the
      // fixed marker is read — never the server's prose.
      let kind: RegistrationFailure = "key";
      try {
        const body = (await res.clone().json()) as { error?: unknown };
        const marker = body?.error;
        kind =
          marker === "invite_key_revoked"
            ? "revoked"
            : marker === "plan_required" ||
                marker === "account_closure_pending"
              ? "paused"
              : marker === "invite_key_unknown"
                ? "unknown"
                : "key";
      } catch {
        // Non-JSON body — fall back to the generic "not accepted" wording.
      }
      setRegistrationRefusalKind(kind);
      warnStayedInactiveOnce(kind);
      // The key was refused, so this app genuinely cannot register under it —
      // a definite negative, and the one case where the gate's no-lockout
      // escape hatch is meant to open.
      markRegistrationRefused();
      noteRegistrationTrouble();
      scheduleConsentRetry();
    } else {
      // Any other non-ok answer (5xx, an unexpected 4xx) is still a
      // registration that did not go through — a kit that says nothing here is
      // indistinguishable from a healthy one. Name the coarse reason once.
      warnStayedInactiveOnce("other", res.status);
      // A server error is not an answer about our records: cannot tell.
      markRegistrationUnreachable();
      noteRegistrationTrouble();
      // A 5xx clears on its own once the server recovers — but only for a page
      // that asks again. (A fresh 400 for a non-UUID id is handled above and
      // deliberately never reaches this ladder: it can never succeed.)
      scheduleConsentChase();
    }
    // NO KEY, SAID LAST — but always said.
    //
    // The keyless state is known the instant enableTelemetry runs (and the
    // panel and status readout show it from that moment). The CONSOLE line
    // waits until the round trip has settled, for one reason: a specific fault
    // beats a general one. A keyless page that also cannot reach us, or whose
    // install id belongs to another copy, has a sharper problem worth the one
    // line it gets. The latch inside warnStayedInactiveOnce makes this a no-op
    // whenever anything above already spoke — so exactly one line is printed
    // either way, and a keyless page is never silent.
    warnNoProjectKeyIfStillUnexplained();
    return res.status;
  }

  /** Say the keyless state on the console, unless a sharper reason already
   *  claimed this page's one line. Never throws. */
  function warnNoProjectKeyIfStillUnexplained(): void {
    try {
      if (projectKey.key) return;
      warnStayedInactiveOnce(
        projectKey.declined ? "no-key-chosen" : "no-key-missing",
      );
    } catch {
      /* evidence never blocks reporting */
    }
  }

  /** Whether the PII-filtered snapshot mirror may upload right now. True in
   *  full mode; in issues-only mode only with the explicit `shareMeterWithAI`
   *  opt-in OR the server directive. Always off when disabled or killed. */
  function effectiveSnapshotUploadAllowed(): boolean {
    if (!enabled || isBoosthisDisabled()) return false;
    return (
      !effectiveIssuesOnly() ||
      explicitShareMeterWithAI ||
      serverShareMeterWithAI
    );
  }

  /** Wire (or unwire) the per-page sample firehose to the CURRENT effective
   *  mode. Wired only in full mode (code config OR the server "Full telemetry"
   *  grant), never when disabled/killed. Idempotent: re-wiring the same
   *  observer is a no-op. Lets the dashboard directive flip the firehose on/off
   *  in-session without a relaunch. Mirrors Node's setSampleQueueIssuesOnly. */
  function syncSampleObserver(): void {
    if (!effectiveIssuesOnly() && enabled && !isBoosthisDisabled()) {
      setSampleObserver(onRecordedSample);
      wirePagehideFlush();
    } else {
      setSampleObserver(null);
      unwirePagehideFlush();
      sampleQueue = [];
      if (sampleTimer !== null) {
        clearTimeout(sampleTimer);
        sampleTimer = null;
      }
    }
  }

  /** Single source of truth for wiring the snapshot submitter + auto-upload
   *  timer. Clearing the submitter (not just stopping the timer) matters
   *  because a manual flush calls the submitter directly. */
  function syncSnapshotUpload(): void {
    if (effectiveSnapshotUploadAllowed()) {
      setSnapshotSubmitter((snap, o) => client.transmitSnapshot(snap, o));
      startSnapshotAutoUpload();
      // The 60s cadence alone loses every visit shorter than a minute — the
      // common case on a plain multi-page site — so the snapshot also
      // exit-flushes on pagehide with keepalive, exactly like the sample
      // queue. Rides the same allow-gate: wired only while sharing is on.
      wireSnapshotPagehideFlush();
      // Trace spans carry code-defined route labels too, so they share the
      // exact same allow-gate as the snapshot mirror: nothing span-shaped is
      // retained or shipped on a private app until sharing is authorized.
      setSpanSubmitter((spans) => client.transmitSpans(spans));
      startSpanAutoFlush();
    } else {
      setSnapshotSubmitter(null);
      stopSnapshotAutoUpload();
      unwireSnapshotPagehideFlush();
      setSpanSubmitter(null);
      stopSpanAutoFlush();
    }
  }

  // Exit flush for the snapshot mirror. uploadPerfSnapshotNow() re-checks the
  // submitter (cleared by syncSnapshotUpload when sharing turns off) and skips
  // data-free pages, so a late pagehide can never leak a snapshot or ship an
  // empty one. keepalive lets the request outlive the dying document.
  const snapshotPagehideFlush = (): void => {
    void uploadPerfSnapshotNow({ keepalive: true });
  };
  function wireSnapshotPagehideFlush(): void {
    try {
      if (typeof addEventListener === "function") {
        // Idempotent: re-adding the same function reference is a no-op per
        // the DOM addEventListener contract, so repeated sync calls are safe.
        addEventListener("pagehide", snapshotPagehideFlush);
      }
    } catch {
      // non-browser context — the cadence timer still flushes
    }
  }
  function unwireSnapshotPagehideFlush(): void {
    try {
      if (typeof removeEventListener === "function") {
        removeEventListener("pagehide", snapshotPagehideFlush);
      }
    } catch {
      // ignore
    }
  }

  // ---- full-sample upload queue (full mode only) ---------------------------
  // The vitals engine records one sample per page view; the observer batches
  // them and flushes on a cadence + on pagehide so a tab close doesn't drop
  // the last page. Issues-only mode never registers the queue.
  let sampleQueue: TelemetrySample[] = [];
  let sampleTimer: ReturnType<typeof setTimeout> | null = null;

  function flushSampleQueue(keepalive = false): void {
    if (sampleTimer !== null) {
      clearTimeout(sampleTimer);
      sampleTimer = null;
    }
    if (sampleQueue.length === 0) return;
    const batch = sampleQueue;
    sampleQueue = [];
    void client.transmit(batch, keepalive ? { keepalive: true } : undefined);
  }

  function scheduleSampleFlush(): void {
    if (sampleTimer !== null) return;
    sampleTimer = setTimeout(() => {
      sampleTimer = null;
      flushSampleQueue();
    }, SAMPLE_FLUSH_MS);
    // In a Node context (tests, SSR) never keep the process alive.
    const t = sampleTimer as { unref?: () => void };
    if (typeof t.unref === "function") t.unref();
  }

  function onRecordedSample(s: Sample): void {
    if (effectiveIssuesOnly() || !enabled || isBoosthisDisabled()) return;
    sampleQueue.push({
      routeLabel: s.name,
      durationMs: s.duration_ms,
      rating: s.rating,
    });
    if (sampleQueue.length >= MAX_SAMPLE_BATCH) {
      flushSampleQueue();
      return;
    }
    scheduleSampleFlush();
  }

  const pagehideFlush = (): void => {
    flushSampleQueue(true);
  };
  function wirePagehideFlush(): void {
    try {
      if (typeof addEventListener === "function") {
        addEventListener("pagehide", pagehideFlush);
      }
    } catch {
      // non-browser context — the cadence timer still flushes
    }
  }
  function unwirePagehideFlush(): void {
    try {
      if (typeof removeEventListener === "function") {
        removeEventListener("pagehide", pagehideFlush);
      }
    } catch {
      // ignore
    }
  }

  const client: TelemetryClient = {
    get installId() {
      return activeInstallId;
    },
    get endpoint() {
      return endpoint;
    },
    get enabled() {
      return enabled && !isBoosthisDisabled();
    },
    get deleteToken() {
      return deleteToken;
    },
    get readToken() {
      return readToken;
    },
    get projectKey() {
      return projectKey;
    },
    get fullTelemetryEffective() {
      // The mode uploads actually run under right now: !effectiveIssuesOnly()
      // is full mode (code config OR the server "Full telemetry" grant).
      return !effectiveIssuesOnly();
    },
    get meterSharing() {
      // The sharing CHOICE, independent of the transient kill/disable state:
      // full mode OR the shareMeterWithAI opt-in (explicit or server).
      return (
        !effectiveIssuesOnly() ||
        explicitShareMeterWithAI ||
        serverShareMeterWithAI
      );
    },

    async consent() {
      if (!enabled || isBoosthisDisabled()) {
        // Returning zero here — after the startup line has already promised a
        // registration — is one of the paths that produced the reported
        // silence. Nothing is sent, so nothing may be claimed: record the
        // honest state. The env kill-switch stays SILENT on the console
        // because the startup line's own badge clause already said the kit is
        // switched off; contradicting it would be worse than saying nothing.
        noteRegistrationNotSent({ silent: isBoosthisDisabled() });
        return 0;
      }
      return postConsent();
    },

    async renewCredential() {
      if (!enabled || isBoosthisDisabled()) return null;
      // Nothing held = nothing was replaced. Re-consenting here would only add
      // a round trip to a refusal whose cause lies elsewhere.
      const stale = deleteToken;
      if (!stale) return null;
      // Drop it FIRST: the server hands a known install its credential back
      // only on a consent that presents none. Sending the dead one would be
      // read as a proof attempt and answered as a normal re-consent instead.
      deleteToken = null;
      await postConsent({ allowRotation: false });
      if (!deleteToken) {
        // No credential came back — the project may have been cut off, or a
        // sibling copy registered moments ago and the server is holding the
        // credential it just handed out. Put the page back exactly as it was
        // (a dead credential is no worse than none) and let the caller say so.
        deleteToken = stale;
        return null;
      }
      // A consent that returned the SAME credential proves nothing was
      // renewed, so the caller must not retry against it.
      return deleteToken === stale ? null : deleteToken;
    },

    async transmit(samples, transmitOpts) {
      // Issues-only mode never ships per-page samples — unless the server's
      // dashboard "Full telemetry" grant is on (effectiveIssuesOnly()).
      if (effectiveIssuesOnly()) return 0;
      if (!enabled || isBoosthisDisabled() || samples.length === 0) return 0;
      // Lazy consent — retry if we never captured a delete token.
      if (!deleteToken) {
        await postConsent();
      }
      // Race re-check: disable() may have flipped while awaiting consent.
      if (!enabled || isBoosthisDisabled()) return 0;
      if (!deleteToken) return 0;
      // Route-label hardening before assertNoPII, mirroring Node: refuse over
      // the shared cap and silently drop any sample whose label
      // carries a PII pattern (UUID, long numeric id, email, JWT, phone).
      // The vitals engine already normalizes dynamic path segments to ":id",
      // so this is defence-in-depth for direct transmit() callers.
      const raw =
        samples.length > MAX_SAMPLE_BATCH
          ? samples.slice(0, MAX_SAMPLE_BATCH)
          : samples;
      const batch = raw
        .filter((s) => {
          if (normalizeRouteLabel(s.routeLabel) === null) {
            warnPartNameRefusal(
              s.routeLabel.length > MAX_PART_NAME ? "too-long" : "invalid",
            );
            return false;
          }
          if (transmitLabelHasPII(s.routeLabel) !== null) {
            warnPartNameRefusal("invalid");
            return false;
          }
          return true;
        })
        .map((s) => ({
          routeLabel: s.routeLabel,
          durationMs: s.durationMs,
          rating: s.rating,
          ...(s.ruleId !== undefined ? { ruleId: s.ruleId } : {}),
        }))
        ;
      if (batch.length === 0) return 0;
      const payload = {
        installId: activeInstallId,
        packageVersion,
        samples: batch,
      };
      assertNoPII(payload);
      try {
        const res = await _safeTransmitInternal(
          `${endpoint}/samples`,
          payload,
          // On the way out of the page a normal fetch is killed by the
          // navigation, so the last page view's measurement is lost — the
          // common case on a plain multi-page site, where nearly every visit
          // ends before the 30s cadence fires.
          transmitOpts?.keepalive
            ? { ...fetchOptions, keepalive: true }
            : fetchOptions,
          `Bearer ${deleteToken}`,
        );
        return _noteUploadResponseForTests(res) ? batch.length : 0;
      } catch {
        noteUploadUnreachable();
        return 0;
      }
    },

    async transmitSnapshot(snapshot, transmitOpts) {
      // The snapshot carries page labels, so it rides the optional
      // full-detail channel. Mirrors Node/RN transmitSnapshot.
      if (!effectiveSnapshotUploadAllowed()) return 0;
      if (!enabled || isBoosthisDisabled()) return 0;
      if (!deleteToken) {
        await postConsent();
      }
      // Race re-check: disable()/forget() may have flipped while awaiting.
      if (!effectiveSnapshotUploadAllowed()) return 0;
      if (!deleteToken) return 0;
      // Filter every label field before the PII guard: page-row keys are
      // code-derived normalized paths, but drop any entry whose label still
      // carries a PII pattern.
      const safeRows = snapshot.rows.filter(
        (r) => transmitLabelHasPII(r.key) === null,
      );
      // The route list is a NEW way for labels to reach the wire, so it is
      // re-audited here rather than trusted from where it was built. An entry
      // that fails is DROPPED, never redacted into something that would read
      // as a route. `total` is left alone deliberately: it states how many
      // the merge held, so a shortened list still reads as "at least".
      const safeRouteList = snapshot.routeList
        ? {
            ...snapshot.routeList,
            entries: snapshot.routeList.entries.filter(
              (e) => transmitLabelHasPII(e.label) === null,
            ),
          }
        : undefined;
      // The control census is the OTHER new way a string reaches the wire, so
      // it is re-audited here too rather than trusted from where it was
      // built. A handle is not screened for PII — it is required to BE a
      // handle, matching the closed shape `controlHandle.ts` produces, which
      // no label, name or value can satisfy. `to` is a route label and goes
      // through the same guard every other label does. An entry failing
      // either is DROPPED whole; `total` is left alone so a shortened list
      // still reads as "at least".
      const safeControlCensus = snapshot.controlCensus
        ? {
            ...snapshot.controlCensus,
            entries: snapshot.controlCensus.entries.filter(
              (e) =>
                isControlHandle(e.handle) &&
                (e.to === undefined || transmitLabelHasPII(e.to) === null),
            ),
          }
        : undefined;
      const sanitizedSnapshot = {
        ...snapshot,
        rows: safeRows,
        ...(safeRouteList ? { routeList: safeRouteList } : {}),
        ...(safeControlCensus ? { controlCensus: safeControlCensus } : {}),
      };
      // The page map rides THIS upload — same gates, same back-off, same
      // batching, no round trip of its own — and only when the host asked
      // for it. A sibling of `snapshot` rather than a field inside it,
      // because the snapshot is stored as sent and this is screened and
      // stored separately.
      const pageMap = sendPageMap ? buildPageMapWire() : null;
      const payload = {
        installId: activeInstallId,
        packageVersion,
        capturedAt: sanitizedSnapshot.capturedAt,
        snapshot: sanitizedSnapshot,
        ...(pageMap ? { pageMap } : {}),
      };
      assertNoPII(payload);
      try {
        const res = await _safeTransmitInternal(
          `${endpoint}/snapshots`,
          payload,
          // On the way out of the page a normal fetch is killed by the
          // navigation. Short visits never survive the 60s snapshot cadence,
          // so without the keepalive exit flush a real page's snapshot (with
          // its axes) would never land server-side at all.
          transmitOpts?.keepalive
            ? { ...fetchOptions, keepalive: true }
            : fetchOptions,
          `Bearer ${deleteToken}`,
        );
        return _noteUploadResponseForTests(res) ? 1 : 0;
      } catch {
        noteUploadUnreachable();
        return 0;
      }
    },

    async transmitCrashes(crashes) {
      // Always-on for registered apps: NOT gated by the enable/disable toggle
      // or issues-only mode. Only the kill-switch (BOOSTHIS_DISABLED) or
      // forget() can stop it — mirrors Node transmitCrashes, NOT the snapshot.
      if (isBoosthisDisabled() || crashes.length === 0) return 0;
      // Lazy consent — retry if the host never captured a delete token.
      if (!deleteToken) {
        await postConsent();
      }
      if (isBoosthisDisabled()) return 0;
      if (!deleteToken) return 0;
      // Server caps a batch at 50 reports (CrashBatch.maxItems).
      const batch = crashes.length > 50 ? crashes.slice(0, 50) : crashes;
      const payload = {
        installId: activeInstallId,
        packageVersion,
        crashes: batch,
      };
      // Defense in depth — every crash fingerprint is code-derived on-device
      // (error name + redacted frame + bucketed count, never the raw message),
      // but the shared PII guard still runs before upload.
      assertNoPII(payload);
      try {
        const res = await _safeTransmitInternal(
          `${endpoint}/crashes`,
          payload,
          fetchOptions,
          `Bearer ${deleteToken}`,
        );
        // /crashes returns 202 Accepted; res.ok covers 2xx.
        return _noteUploadResponseForTests(res) ? batch.length : 0;
      } catch {
        noteUploadUnreachable();
        return 0;
      }
    },

    async transmitCandidates(signatures, transmitOpts) {
      // Always-on for registered apps: NOT gated by the enable/disable toggle
      // or issues-only mode. Only the kill-switch (BOOSTHIS_DISABLED) or
      // forget() can stop it — mirrors transmitCrashes, NOT the snapshot.
      if (isBoosthisDisabled() || signatures.length === 0) return 0;
      // Lazy consent — retry if the host never captured a delete token.
      if (!deleteToken) {
        await postConsent();
      }
      // Race re-check: forget()/the kill-switch may have flipped while awaiting.
      if (isBoosthisDisabled()) return 0;
      if (!deleteToken) return 0;
      // Server caps a batch at 50 signatures.
      const batch =
        signatures.length > 50 ? signatures.slice(0, 50) : signatures;
      const payload = {
        installId: activeInstallId,
        packageVersion,
        signatures: batch,
      };
      // Defense in depth — a signature is built by Boosthis from
      // kind + severity bucket + count bucket and carries no page label at all,
      // but the shared PII guard still runs before it leaves.
      assertNoPII(payload);
      try {
        const res = await _safeTransmitInternal(
          `${endpoint}/candidates`,
          payload,
          // A closing page is the common ending in a browser, so the exit
          // flush must outlive the document exactly like the sample and
          // snapshot flushes do.
          transmitOpts?.keepalive
            ? { ...fetchOptions, keepalive: true }
            : fetchOptions,
          `Bearer ${deleteToken}`,
        );
        return _noteUploadResponseForTests(res) ? batch.length : 0;
      } catch {
        noteUploadUnreachable();
        return 0;
      }
    },

    async transmitResolutions(resolutions, transmitOpts) {
      // Always-on for registered apps, exactly like transmitCandidates.
      if (isBoosthisDisabled() || resolutions.length === 0) return 0;
      if (!deleteToken) {
        await postConsent();
      }
      if (isBoosthisDisabled()) return 0;
      if (!deleteToken) return 0;
      const batch =
        resolutions.length > 50 ? resolutions.slice(0, 50) : resolutions;
      const payload = {
        installId: activeInstallId,
        packageVersion,
        resolutions: batch,
      };
      assertNoPII(payload);
      try {
        const res = await _safeTransmitInternal(
          `${endpoint}/resolutions`,
          payload,
          transmitOpts?.keepalive
            ? { ...fetchOptions, keepalive: true }
            : fetchOptions,
          `Bearer ${deleteToken}`,
        );
        return _noteUploadResponseForTests(res) ? batch.length : 0;
      } catch {
        noteUploadUnreachable();
        return 0;
      }
    },

    async transmitSpans(spans) {
      // Trace spans carry code-defined route labels, so they ride the SAME
      // gate as the snapshot mirror: off in issues-only mode unless the
      // developer opted in via `shareMeterWithAI` OR the server directive
      // fired. Always gated off when disabled or killed. Mirrors Node/RN.
      if (!effectiveSnapshotUploadAllowed()) return 0;
      if (!enabled || isBoosthisDisabled() || spans.length === 0) return 0;
      // Lazy consent — retry if the host never captured a delete token.
      if (!deleteToken) {
        await postConsent();
      }
      // Race re-check: disable()/forget() may have flipped while awaiting.
      if (!effectiveSnapshotUploadAllowed()) return 0;
      if (!deleteToken) return 0;
      // Drop any span whose label carries a PII pattern. The labels are
      // code-defined ("GET /users/:id", ids redacted by spanLabel), but a
      // caller could pass a user-derived string. Use transmitLabelHasPII —
      // the same guard as sample uploads — which enforces UUID, long numeric
      // id, and whitespace checks while still allowing the structural
      // "HTTP_METHOD /path" space that span labels legitimately carry.
      const safe = spans.filter((s) => transmitLabelHasPII(s.routeLabel) === null);
      const batch =
        safe.length > MAX_SPAN_BATCH ? safe.slice(0, MAX_SPAN_BATCH) : safe;
      if (batch.length === 0) return 0;
      const payload = {
        installId: activeInstallId,
        packageVersion,
        spans: batch,
      };
      // Defense in depth — the per-label filter already dropped any PII-bearing
      // span, but run the whole-payload guard before it leaves.
      assertNoPII(payload);
      try {
        const res = await _safeTransmitInternal(
          `${endpoint}/spans`,
          payload,
          fetchOptions,
          `Bearer ${deleteToken}`,
        );
        return _noteUploadResponseForTests(res) ? batch.length : 0;
      } catch {
        noteUploadUnreachable();
        return 0;
      }
    },

    disable() {
      // disable() stops the optional full-detail channels: samples + the
      // screen-bearing snapshot mirror. syncSnapshotUpload() clears the
      // submitter (not just the timer) so a manual flush cannot leak a
      // snapshot after disable().
      enabled = false;
      sampleQueue = [];
      if (sampleTimer !== null) {
        clearTimeout(sampleTimer);
        sampleTimer = null;
      }
      // Cancel any pending rejected-registration retry — the fire-time guard
      // would no-op it anyway, but don't leave a dead timer pending.
      stopConsentRetry();
      syncSampleObserver();
      syncSnapshotUpload();
    },
    enable() {
      enabled = true;
      syncSampleObserver();
      syncSnapshotUpload();
    },

    applyServerFullTelemetry(on: boolean) {
      // Mirror the consent-response directive-apply (see postConsent above): a
      // no-op when the value is unchanged; otherwise flip the session directive
      // and re-wire the sample firehose + snapshot mirror to the new mode. The
      // bubble's signed-in account card calls this after its own successful
      // toggle POST so the new mode takes effect immediately (no relaunch). It
      // never overrides disable()/forget()/BOOSTHIS_DISABLED — the sync helpers
      // both re-check enabled + the kill-switch.
      if (typeof on !== "boolean" || on === serverFullTelemetry) return;
      serverFullTelemetry = on;
      syncSampleObserver();
      syncSnapshotUpload();
      // Flipping the switch from the bubble is the other way into a
      // contradiction, and it takes effect without a relaunch — so the same
      // sentence has to be available from here too.
      noteTelemetryModeContradiction();
    },

    async forget() {
      // Local state is wiped unconditionally — even without a delete token,
      // "forget" must leave nothing Boosthis-shaped behind. Best-effort,
      // never throws.
      setSampleObserver(null);
      unwirePagehideFlush();
      sampleQueue = [];
      if (sampleTimer !== null) {
        clearTimeout(sampleTimer);
        sampleTimer = null;
      }
      // Cancel any pending rejected-registration retry — forget() must leave
      // nothing Boosthis-shaped running.
      stopConsentRetry();
      // Stop the server-authority heartbeat and RE-LOCK the kit: erasure must
      // leave nothing Boosthis-shaped behind and a copy without a proven
      // handshake must not run (the ACTIVATION LOCK re-engages exactly as on a
      // never-connected install). Best-effort, never throws.
      stopEntitlementCheckin();
      try {
        clearEntitlementCache();
      } catch {
        // best-effort — the in-memory lock is already engaged
      }
      setSnapshotSubmitter(null);
      stopSnapshotAutoUpload();
      unwireSnapshotPagehideFlush();
      // Tear down the span mirror too: stop the flusher, unwire the submitter
      // (making enqueueSpan inert), and drop any buffered spans.
      setSpanSubmitter(null);
      stopSpanAutoFlush();
      clearBufferedSpans();
      // Crash reporting is ALWAYS-ON for a registered app; forget() (and the
      // kill-switch) are the only things that stop it. Clear the submitter and
      // uninstall the window error/unhandledrejection hooks, wiping the
      // in-memory buffer AND the persisted crash key so nothing Boosthis-shaped
      // is left behind.
      setCrashSubmitter(null);
      await uninstallCrashHandlers();
      // Problem reporting, same treatment: stop the loop, unwire the exit
      // flush, drop both submitters (making any later tick inert), and erase
      // the accumulated candidate + baseline keys from storage. Nothing may
      // survive as leaked state into the next page load.
      stopReporting();
      setCandidateSubmitter(null);
      setResolutionSubmitter(null);
      clearAllCandidates();
      // Drop the per-call observed/failed counts. They hold nothing but two
      // numbers against a label that never leaves this page, but `forget()`
      // leaves NOTHING Boosthis-shaped behind, and a count that survives
      // erasure is a count the next snapshot would upload about a period this
      // install has been told to forget.
      resetRouteOutcomes();
      // Put the page's own Web Storage methods back the way we found them and
      // drop the two counters behind them. The local-store readings observe
      // the HOST's data path, so erasure has to hand that path back
      // untouched: leaving `Storage.prototype` wrapped would keep this page
      // running Boosthis code after it was told to forget, and leaving the
      // counts would let operations from before the erasure be reported by a
      // later start as if they had just been measured.
      uninstallLocalStoreTracking();
      // Erase persisted state (privacy parity — forget leaves nothing
      // Boosthis-shaped in storage).
      readToken = null;
      storage.remove(readTokenStoreKey);
      storage.remove("installId");
      // Including the panel's own count of page views measured on this
      // device. It never leaves the page and it is only two integers, but it
      // is Boosthis-shaped state that outlives a page load, and a count that
      // survives erasure would let this browser keep reporting a period the
      // install has been told to forget.
      clearPageViewTally();
      // And the press→screen join, including its counts of presses that
      // reached no timed screen: an erased install keeps no tally of what it
      // watched, not even the parts it could not measure.
      resetPressToScreen();
      // Including the memory that this install was ever registered: an
      // erased install must not leave a record of its own registration
      // behind to soften the next page's verdict.
      forgetRegistered(activeInstallId);
      forgetRegistered(opts.installId);
      // Erase the self-healing rotation mapping too — forget() must leave
      // nothing Boosthis-shaped behind, and the forget below (sent under the
      // ROTATED identity when one is active) erases the identity that
      // actually owns the uploaded data.
      storage.remove(rotationStoreKey);
      if (!deleteToken) {
        enabled = false;
        return 0;
      }
      // Kill-switch honors silence even on erasure: never touch the network,
      // but still clear local state.
      if (isBoosthisDisabled()) {
        deleteToken = null;
        enabled = false;
        return 204;
      }
      // Right-to-erasure: the fixed payload legitimately contains the
      // server-issued `deleteToken` field (which the PII guard would
      // otherwise reject by field name). `_safeTransmitTrustedPayload` skips
      // ONLY the payload-body check while still scanning caller-supplied
      // headers + URL and honoring the kill-switch.
      const token = deleteToken;
      try {
        const res = await _safeTransmitTrustedPayload(
          `${endpoint}/installs/forget`,
          {
            installId: activeInstallId,
            deleteToken: token,
          },
          fetchOptions,
        );
        deleteToken = null;
        enabled = false;
        return res.status;
      } catch {
        return 0;
      }
    },
  };

  // Wire the full-sample queue the moment telemetry is enabled (full mode
  // only — issues-only apps never queue a sample until the dashboard "Full
  // telemetry" directive flips them into full mode) and the snapshot mirror
  // per the initial share policy.
  syncSampleObserver();
  syncSnapshotUpload();

  // Crash reporting is ALWAYS-ON for a registered app (independent of the
  // enable/disable toggle and issues-only mode) — the window error /
  // unhandledrejection listeners are OBSERVE-ONLY and never change the host's
  // crash behavior. Only the kill-switch (`BOOSTHIS_DISABLED`) or `forget()`
  // stops it. Wire the submitter first, then install the listeners.
  setCrashSubmitter((crashes) => client.transmitCrashes(crashes));
  installCrashHandlers({ detailed: opts.crashDetails ?? false });

  // Problem reporting is ALWAYS-ON for a registered app too — same gate as
  // crash reporting, and for the same reason: a recurring problem a real
  // visitor hit has to be able to teach us something whether or not the
  // developer ever connected an AI. Wire both submitters first, then start the
  // (slow) accumulation loop, which also flushes on `pagehide` so a page closed
  // after ten seconds still delivers what it saw.
  setCandidateSubmitter((signatures, submitOpts) =>
    client.transmitCandidates(signatures, submitOpts),
  );
  setResolutionSubmitter((resolutions, submitOpts) =>
    client.transmitResolutions(resolutions, submitOpts),
  );
  startReporting();

  // Persist the install id so a Node-side MCP entrypoint (or a later session)
  // can fall back to it together with the persisted read token. Best-effort.
  // Uses the ACTIVE identity (post-rotation when a persisted mapping was
  // adopted) so external readers always see the id that owns the data.
  storage.set("installId", activeInstallId);

  // Register as the active client so display-only surfaces (the bubble's
  // dashboard panel + account card) can read the live endpoint / installId /
  // tokens. Last-wins: a host that re-enables replaces the previous client.
  activeClient = client;

  // A RETURNING VISITOR IS NOT A FAULT.
  //
  // The startup line promises a registration on every load, but a page that
  // already holds a credential for this install deliberately does not register
  // again — so "announced and never asked" describes a perfectly healthy
  // repeat visit as well as the failure this watch exists to catch. Only the
  // server can tell those apart (the credential may equally be stale, or have
  // been copied from another install), so when the deadline arrives with no
  // attempt made, this asks it. A page holding nothing at all is not worth a
  // request: it cannot be the healthy case.
  setRegistrationProbe(async () => {
    // Either this page holds a credential, or this browser has watched
    // Boosthis accept this very install id before. The second is the reloaded
    // healthy page: registration is durable, so a page that cannot produce a
    // token is "cannot tell", never "it never registered".
    if (!deleteToken && !readToken && !wasRegisteredBefore(activeInstallId)) {
      return false;
    }
    try {
      await checkRegistrationOnce({
        endpoint,
        installId: activeInstallId,
        projectKey: projectKey.key,
        ...(fetchOptions.fetchImpl ? { fetchImpl: fetchOptions.fetchImpl } : {}),
      });
    } catch {
      // An unanswered check is "cannot tell" — never evidence of a fault.
    }
    return true;
  });

  // Install the server-authority kill-switch heartbeat + run the FORCED launch
  // check-in (VAULT contract's on-launch enforcement edge). Web storage is
  // synchronous so the rotation mapping was already adopted above, but we still
  // defer the kick to a microtask to match the Node runtime's post-rotation
  // timing — enableTelemetry() must return without a synchronous network call,
  // and the disabled/kill state is re-read at kick time. Fail-open, never
  // throws into the host.
  void Promise.resolve()
    .then(() => {
      if (!enabled || isBoosthisDisabled()) return;
      installCheckinConfig();
    })
    .catch(() => {
      // never throw into the host
    });

  return client;
}

/** How long a keyless page waits before saying so on the console, when no
 *  registration round trip has settled to claim the line first. */
const NO_KEY_ANNOUNCE_DELAY_MS = 15_000;

export function isPageMapSendEnabled(): boolean {
  return pageMapSendRequested;
}
