/** BoosthisBubble (web) — a floating, draggable bubble for the browser kit.
 *
 * The web analogue of the RN kit's BoosthisLauncher + BoosthisDashboard: drop
 * the kit into any site and a small movable bubble appears in a corner showing
 * the page's live performance rating. Tapping it opens a panel — the same
 * experience the RN kit gives: a first-open GATE (connect + agree), then the
 * FULL dashboard with a hero score, the meter axes the web kit computes, the
 * live event stats, an account card, and a footer with the disclaimer, kit
 * version, and a Terms link.
 *
 * GATE-FIRST (port of the RN kit's BoosthisTermsGate): on first open the panel
 * shows a gate — the "Connect to your account" card on top, a Terms & Privacy
 * box, an acceptance checkbox, an "I Agree & Continue" button, and a "Decline"
 * button. Only after acceptance does the dashboard render. Acceptance is keyed
 * to the terms version AND the installId, and is CLEARED on sign-out — so a
 * `TERMS_VERSION` bump, a fresh install (new installId), or signing out of the
 * kit all restart the full gate flow (sign-in → telemetry → terms).
 * Acceptance is read/written via the kit's `terms.ts` helpers
 * (`hasAcceptedCurrentTerms` / `recordTermsAccepted` / `clearTermsAcceptance`);
 * the fire-and-
 * forget local marker now fires on the Agree tap, not merely on open. The gate
 * concerns ONLY the Boosthis panel — it never walls the host page.
 *
 * Visibility precedence (first match wins):
 *   1. `BOOSTHIS_DISABLED` kill-switch — bubble never renders.
 *   2. `BOOSTHIS_BUBBLE` env/global — "1"/"true"/"yes"/"on" forces on,
 *      "0"/"false"/"no"/"off" forces off; values are trimmed/case-insensitive.
 *   3. `BOOSTHIS_NO_BUBBLE` truthy hides, then `BOOSTHIS_FORCE_BUBBLE` truthy
 *      shows. These legacy aliases lose to `BOOSTHIS_BUBBLE`.
 *   4. Code option — `startWebVitals({ bubble: true | false })`.
 *   5. Default: VISIBLE. The bubble shows on every host — live/published
 *      domains included — so an install never looks broken in production. Opt
 *      out explicitly with `BOOSTHIS_BUBBLE=0` or
 *      `startWebVitals({ bubble: false })`.
 *
 * Guest-safety: this is guest code inside a host page and must NEVER throw into
 * the host or break the page. Every listener + async handler is wrapped; a
 * failure anywhere quietly removes the bubble and the host keeps running. All
 * dynamic values are inserted via `textContent` — never markup. Rendered inside
 * a CLOSED shadow root so host CSS and the bubble's styles can't leak.
 *
 * Meters + samples are read on-device only and NEVER transmitted by the bubble.
 * The account card DOES talk to the hosted auth endpoints (sign-in / claim) —
 * those carry the developer's OWN credentials on purpose and are the only
 * network traffic the bubble makes.
 */

import { isBoosthisDisabled, readFlagValue } from "./runtimeFlags";
import { summary } from "./samples";
import { capturePerfSnapshot } from "./snapshot";
import {
  linearScore,
  ratingFor,
  RUNTIME_VERSION,
  SCORE_THRESHOLDS,
  type Rating,
} from "./thresholds";
import {
  getCircuitStats,
  getDeadClickStats,
  getLongTaskStats,
  getRouteReachabilityStats,
  getRoutesMissingFromPageMapCount,
  getVitals,
  currentRouteLabel,
} from "./vitals";
import { getPageMapStats, type PageMapPersistence } from "./pageMap";
import { pageActivityFacts, pageActivityHeroText } from "./pageActivity";
import { pageViewScopeLine, readPageViewTally } from "./pageViewTally";
import {
  CONTROL_MAP_SCOPE_CAPTION,
  controlMapPanelText,
  controlMapRows,
  getControlMap,
  type ControlMapReport,
} from "./controlMap";
import {
  controlCensusPanelText,
  controlCensusReport,
} from "./controlCensus";

import {
  getActiveProjectKey,
  getActiveProjectKeyStatus,
  getRegistrationRefusalKind,
  getRegistrationRefusalSentence,
  getActiveTelemetryClient,
  isInstallIdRejected,
  getDroppedRowCount,
  getDroppedRowCauses,
  getDroppedUploadCount,
  getLastUploadFailure,
  isLastUploadAttemptFailed,
  isPageMapSendEnabled,
  type UploadFailReason,
} from "./telemetry";
import {
  checkRegistrationOnce,
  getRegistrationState,
  getRegistrationVerdict,
  subscribeRegistration,
} from "./registration";
import {
  resolveTermsUrl,
  recordTermsAccepted,
  hasAcceptedCurrentTerms,
  clearTermsAcceptance,
} from "./terms";
import {
  buildPanelRows,
  barWidth,
  ratingLabel,
  type PanelMeterRow,
} from "./bubblePanel";
import { mountAccountCard, type AccountCardHandle } from "./bubbleAccount";
import { BOOSTHIS_ICON_URI } from "./brandIcon";
import {
  getEntitlementGateKind,
  getEntitlementMessage,
  forceEntitlementCheck,
  subscribeEntitlement,
} from "./killSwitch";
import {
  getKitProject,
  projectDisplay,
  PROJECT_LABEL,
  SCORE_CAPTION,
} from "./projectIdentity";

/**
 * How many drawn control → page lines the panel holds. The panel is 300px
 * wide, so this is the heaviest handful; a fixed number of row slots is
 * built once and filled on refresh, so a live page never creates DOM to
 * show the map. One extra slot carries the "+N more" line.
 */
const CONTROL_MAP_PANEL_ROWS = 4;

/** One pre-built row of the panel's drawn map. */
export interface ControlRowSlot {
  /** The control's code-defined id, or "(unnamed)"; empty on a spare row. */
  control: string;
  /** "/orders → /orders/new ×3", or the "+N more" line. */
  detail: string;
  /** False for a slot with nothing to say, which stays hidden. */
  shown: boolean;
}

/**
 * What each pre-built panel row should say. Separated from the DOM so the
 * decision can be tested: which pairs are drawn, how an unnamed control is
 * drawn, and that the last slot admits how many are not being shown rather
 * than letting them disappear.
 */
export function computeControlRowSlots(
  report: ControlMapReport,
  slots: number = CONTROL_MAP_PANEL_ROWS + 1,
): ControlRowSlot[] {
  const out: ControlRowSlot[] = [];
  const drawn = controlMapRows(report, slots > 1 ? slots - 1 : 1);
  for (let i = 0; i < slots; i++) {
    const row = drawn.rows[i];
    if (row) {
      out.push({
        control: row.control,
        detail: row.count > 1 ? `${row.detail} ×${row.count}` : row.detail,
        shown: true,
      });
    } else if (i === drawn.rows.length && drawn.more > 0) {
      out.push({ control: "", detail: `+${drawn.more} more`, shown: true });
    } else {
      out.push({ control: "", detail: "", shown: false });
    }
  }
  return out;
}

const TRUE_VALUES = new Set(["1", "true", "yes", "on"]);
const FALSE_VALUES = new Set(["0", "false", "no", "off"]);

type BubbleFlag =
  | "BOOSTHIS_BUBBLE"
  | "BOOSTHIS_NO_BUBBLE"
  | "BOOSTHIS_FORCE_BUBBLE";

/** Read exactly the named bubble flag through the shared flag reader — env
 *  var (Node/SSR), then `globalThis.NAME`, then `globalThis.__NAME__`. A page
 *  cannot set an environment variable, so the global spellings are the only
 *  ones a real browser install can use. */
function readBubbleFlag(name: BubbleFlag): string | boolean | undefined {
  return readFlagValue(name);
}

function parseBubbleDirective(raw: string | boolean | undefined): boolean | undefined {
  if (raw === undefined) return undefined;
  if (typeof raw === "boolean") return raw;
  const value = raw.trim().toLowerCase();
  if (TRUE_VALUES.has(value)) return true;
  if (FALSE_VALUES.has(value)) return false;
  return undefined;
}

function isTruthyAlias(raw: string | boolean | undefined): boolean {
  if (typeof raw === "boolean") return raw;
  return typeof raw === "string" && TRUE_VALUES.has(raw.trim().toLowerCase());
}

/** Dev-looking host? True for localhost / loopback / *.local / *.replit.dev.
 *  NOTE: the bubble no longer keys its default visibility off this helper (the
 *  default is now visible everywhere); it stays part of the public API for
 *  callers that want the dev/prod host signal. */
export function isDevHost(hostname?: string): boolean {
  try {
    const h = (
      hostname ??
      (typeof location !== "undefined" ? location.hostname : "")
    ).toLowerCase();
    if (!h) return false;
    if (h === "localhost" || h === "::1" || h === "[::1]" || h === "0.0.0.0") {
      return true;
    }
    if (h.startsWith("127.")) return true;
    if (h.endsWith(".local")) return true;
    if (h.endsWith(".replit.dev")) return true;
    return false;
  } catch {
    return false;
  }
}

/** Resolve whether the bubble should render right now. */
export function resolveBubbleVisibility(option?: boolean): boolean {
  // The absolute stop has its own fail-closed guard.
  try {
    if (isBoosthisDisabled()) return false;
  } catch {
    return false;
  }

  // Ordinary read failures are fail-open: they cannot silently hide the badge.
  try {
    const directive = parseBubbleDirective(readBubbleFlag("BOOSTHIS_BUBBLE"));
    if (directive !== undefined) return directive;
    if (isTruthyAlias(readBubbleFlag("BOOSTHIS_NO_BUBBLE"))) return false;
    if (isTruthyAlias(readBubbleFlag("BOOSTHIS_FORCE_BUBBLE"))) return true;
    if (option !== undefined) return option;
  } catch {
    return true;
  }
  // Default TRUE — visible on live/published sites too. An install that shows
  // nothing in production reads as broken; hiding is an explicit opt-out.
  return true;
}

/** Compute the display-only composite: weighted linear scores of the rated
 *  duration metrics that have fired so far (same weights as the composite
 *  model). Returns null score until at least one metric is measured. */
export function computePagePulse(): {
  score: number | null;
  rating: Rating | null;
  sampleCount: number;
} {
  const v = getVitals();
  let weighted = 0;
  let weightSum = 0;
  const add = (value: number | null, metric: keyof typeof SCORE_THRESHOLDS) => {
    if (value == null || !Number.isFinite(value)) return;
    const t = SCORE_THRESHOLDS[metric];
    weighted += t.weight * linearScore(value, t.good, t.poor);
    weightSum += t.weight;
  };
  add(v.ttfbMs, "ttfb");
  add(v.lcpMs, "lcp");
  add(v.inpMs, "inp");
  let sampleCount = 0;
  try {
    sampleCount = summary().total;
  } catch {
    sampleCount = 0;
  }
  if (weightSum <= 0) return { score: null, rating: null, sampleCount };
  const score = Math.round(weighted / weightSum);
  return { score, rating: ratingFor(score), sampleCount };
}

/** Lock descriptor served alongside (or instead of) the panel body when the
 *  server-authority kill-switch has rendered the kit inert. Consumed by the
 *  bubble's paint loop:
 *   - `kind: "hidden"` → silently hide the whole bubble (env-kill / tampered /
 *     grace-expired / never-activated ACTIVATION LOCK — no readable state).
 *   - `kind: "revoked" | "unpaid" | "paused"` → render the BLOCKING overlay with
 *     the paired `title` + `body` copy (owner-facing, no action button, no
 *     in-kit payment). Copy is kept byte-equal to the RN/Node dashboard overlay.
 *
 *  Returns `null` when the gate is "none" (active / within grace) so the panel
 *  serves its normal meter body. Never throws — a failure fails OPEN (null) so
 *  a bug in the entitlement client can never brick a paying app. */
export interface PanelLock {
  kind: "hidden" | "revoked" | "unpaid" | "paused";
  title: string;
  body: string;
}

export function computePanelLock(): PanelLock | null {
  try {
    const gate = getEntitlementGateKind();
    if (gate === "none") return null;
    // Never checked in is NOT a lock: no overlay, so the ordinary panel renders
    // and computePanelNotice()'s "Not registered yet" is what the developer
    // reads. Returning a lock here is what used to blank the whole bubble.
    if (gate === "unregistered") return null;
    if (gate === "hidden") {
      return { kind: "hidden", title: "", body: "" };
    }
    const detail = getEntitlementMessage();
    if (gate === "revoked") {
      return {
        kind: "revoked",
        title: "Access revoked",
        body:
          "This project's Boosthis access was revoked by the account owner. " +
          "Contact the owner if you think this is a mistake.",
      };
    }
    if (gate === "unpaid") {
      return {
        kind: "unpaid",
        title: "Payment required",
        body:
          "The Boosthis subscription for this account is unpaid. Ask the account " +
          "owner to renew it at boosthis.com to restore access.",
      };
    }
    // paused — calmer, reversible-pause notice; prefer the server-supplied
    // message when present (parity with the RN/Node overlay's `detail ?? …`).
    return {
      kind: "paused",
      title: "Boosthis is paused",
      body:
        detail ??
        "The owner has paused this app from the Boosthis dashboard. " +
          "Nothing was deleted — press Reconnect there to resume.",
    };
  } catch {
    // Fail OPEN: never brick a paying app because the gate readout threw.
    return null;
  }
}

/** Developer-facing notice shown when the SERVER rejected this app's
 *  registration because its install ID is not a UUID (consent 400 +
 *  `invalid_install_id`). Every byte is code-defined — no server text, no
 *  install id, no developer-supplied string is ever rendered here, because the
 *  whole point of the fixed copy is that nothing attacker-controllable reaches
 *  the panel. Display-only. */
const INSTALL_ID_REJECTED_COPY = {
  title: "Registration rejected \u2014 install ID must be a UUID",
  body: "Mint a real UUID and restart.",
} as const;

/** The install-ID-rejected notice descriptor, or null when registration was
 *  never rejected for that reason. Pure + display-only, so parity tests can
 *  assert the copy without a DOM (the web test env is "node"). Never throws —
 *  a failure fails OPEN (null) so a bug here can never blank the panel. */
export function computeInstallIdRejectedNotice(): {
  title: string;
  body: string;
} | null {
  try {
    if (!isInstallIdRejected()) return null;
    return {
      title: INSTALL_ID_REJECTED_COPY.title,
      body: INSTALL_ID_REJECTED_COPY.body,
    };
  } catch {
    return null;
  }
}

/** Shown in place of the identity when the kit has not started (no install id
 *  to name). Never a blank line: an empty slot reads as "no identity", which is
 *  exactly the ambiguity this line exists to remove. */
const INSTALL_ID_UNKNOWN_TEXT = "Install ID: not started on this page";

/** The hero line while this page has measured nothing yet.
 *
 *  It carries a NUMBER on purpose. The pause before the first score is
 *  deliberate — the kit holds its first report back for a short warm-up so the
 *  first figure is measured rather than guessed — but a line that only says
 *  "measuring" reads exactly the same at ten seconds as at ten minutes, so a
 *  normal wait was indistinguishable from a dead kit. With the time named, the
 *  same screen reads as a countdown.
 *
 *  The figure is this kit's real one: the first upload goes about ten seconds
 *  into a session and page measurements batch every thirty, so "about a
 *  minute" covers a first score comfortably. It is a KIT-owned literal (never
 *  server prose), matching every other line this panel draws. Changing the
 *  first-upload ramp means changing this sentence. */
const MEASURING_HERO_TEXT =
  "measuring \u2014 browse a few pages; first score in about a minute";

/** The identity line the panel prints, so the developer (or an AI reading the
 *  panel) can compare it with the dashboard instead of guessing which install
 *  is being described. Pure + display-only; never throws. */
export function computePanelInstallLine(): string {
  try {
    const id = getActiveTelemetryClient()?.installId;
    return typeof id === "string" && id ? `Install ID: ${id}` : INSTALL_ID_UNKNOWN_TEXT;
  } catch {
    return INSTALL_ID_UNKNOWN_TEXT;
  }
}

/**
 * Does the consent gate require sign-in + a linked project before terms can be
 * accepted? True whenever this project really IS registered with Boosthis.
 *
 * The no-lockout rule stands: an app that genuinely CANNOT register must not be
 * trapped behind a sign-in wall with no way forward. But the escape hatch may
 * only open on a CONFIRMED inability —
 *   • the kit never started here (no endpoint / no install id),
 *   • Boosthis says this install is not on file, or refused the project key,
 *   • we asked and could not reach anyone, and hold no local credential either
 *     (signing in would fail for the same reason).
 * A merely-missing local delete token is NOT one of those: the runtime never
 * persists it, so reading it as "unregistered" let the owner of a live install
 * walk past sign-in AND the telemetry choice by ticking terms alone.
 *
 * While the question is still open (asked, no answer yet) the gate stays
 * closed — a registered app must never slip through mid-check.
 *
 * Pure + display-only so it can be asserted without a DOM. Never throws; a
 * failure returns false (never lock the developer out because of a bug here).
 */
export function computeGateSignInRequired(): boolean {
  try {
    const c = getActiveTelemetryClient();
    if (!c?.endpoint || !c?.installId) return false;
    const verdict = getRegistrationVerdict();
    if (verdict === "registered") return true;
    if (verdict === "unregistered") return false;
    if (getRegistrationState() === "unreachable") return !!c.deleteToken;
    return true;
  } catch {
    return false;
  }
}

/** The gate's live enable/hint state. `checked` is the terms checkbox, and
 *  `connected` is set only by a VERIFIED link to the signed-in account. Pure,
 *  so the "a registered app cannot enter by ticking terms alone" rule can be
 *  asserted without a DOM. */
export function computeGateState(opts: {
  checked: boolean;
  connected: boolean;
}): { registered: boolean; needsConnect: boolean; canAgree: boolean } {
  const registered = computeGateSignInRequired();
  const needsConnect = registered && !opts.connected;
  return { registered, needsConnect, canAgree: opts.checked && !needsConnect };
}

/** The coarse kind of panel notice, or null when there is nothing to say.
 *  Mirrors the Ruby kit's `compute_panel_notice`. */
export type PanelNoticeKind =
  | "install-id-not-uuid"
  /** No project key at all, and nobody asked for that. The quiet failure this
   *  whole notice family exists for: the page measures itself flawlessly and
   *  appears on no dashboard, so no alert about it can ever fire. */
  | "no-project-key"
  /** No project key, deliberately. Same behaviour, opposite meaning — and the
   *  panel must never present one as the other. */
  | "no-project-key-chosen"
  | "not-registered"
  | "registration-unknown"
  | "sharing-off"
  | "uploads-failing";

/** Fixed, code-defined copy for each notice kind. Every byte is authored here —
 *  no server text, no install id, no developer string ever reaches the panel,
 *  and the sharing-off body names this kit's REAL share opt-in
 *  (`shareMeterWithAI: true`) as one of the two ways to turn sharing on. */
export const PANEL_NOTICE_COPY: Record<
  PanelNoticeKind,
  { title: string; body: string }
> = {
  "install-id-not-uuid": {
    title: INSTALL_ID_REJECTED_COPY.title,
    body: INSTALL_ID_REJECTED_COPY.body,
  },
  "no-project-key": {
    title: "No project key",
    body:
      "Boosthis stayed inactive because no project key is wired into this app. " +
      "Without one this app measures itself and never appears on your Boosthis dashboard. " +
      "Copy a project key from your Boosthis Setup page into this app, then reload.",
  },
  "no-project-key-chosen": {
    title: "Running with no project key",
    body:
      "Boosthis stayed inactive because this app is set to run with no project key. " +
      "That is a deliberate setting: this app measures itself and never appears on your Boosthis dashboard. " +
      "Nothing is being sent.",
  },
  "not-registered": {
    title: "Not registered yet",
    body:
      "This app has not registered with Boosthis, so nothing is being sent. " +
      "Check the project key and this app's outbound network access, then restart.",
  },
  "registration-unknown": {
    title: "Can't check right now",
    body:
      "Boosthis could not be reached to confirm whether this app is registered, " +
      "so this panel cannot say either way yet. This is not a failed install and " +
      "it does not mean anything stopped — it settles by itself once the check " +
      "goes through. Do not change the install line's id while this is showing.",
  },
  "sharing-off": {
    title: "Nothing is being uploaded",
    body:
      "This app is registered, but sharing is off: these meters stay on this " +
      "screen and your Boosthis dashboard stays empty. Turn sharing on there, " +
      "or start the kit with shareMeterWithAI: true.",
  },
  "uploads-failing": {
    title: "Uploads are not getting through",
    body: "This app is registered and sharing is on, but the last batch of measurements did not reach Boosthis, so your dashboard is missing the most recent data.",
  },
};

/** The panel's notice kind, or null when there is nothing to say. Ordered by
 *  how completely each state blocks the developer, byte-parity with the Ruby
 *  kit's `compute_panel_notice`:
 *
 *    install-id-not-uuid   the server rejected the id outright
 *    not-registered        the kit never started, or the server says this
 *                          install is not on file / the key was refused
 *    registration-unknown  we could not get an answer and hold nothing local:
 *                          "cannot tell right now", NEVER a failed install
 *    sharing-off           registered, but nothing is being uploaded
 *
 *  The registration verdict is the SERVER's answer (see registration.ts), not
 *  a browser-local guess: the runtime never persists the delete token, so a
 *  second browser holding none is not evidence of anything.
 *
 *  The last one is the quiet failure this notice exists to end: an install with
 *  sharing off still runs, still renders live local meters, and still looks
 *  completely healthy — while the dashboard it is supposed to feed stays empty.
 *  Nothing anywhere told the developer that, so the panel does.
 *
 *  Fail-open by construction: any error returns null (render the panel with no
 *  notice) rather than blanking the panel. */
export function computePanelNotice(): PanelNoticeKind | null {
  try {
    if (isInstallIdRejected()) return "install-id-not-uuid";
    // A missing key outranks everything below it, because everything below it
    // is a consequence: with no key there is nothing to register, nothing to
    // upload, and no project on the dashboard to be silent about. Saying "not
    // registered yet" here would name the symptom and hide the cause.
    const refusal = getRegistrationRefusalKind();
    if (refusal === "no-key-missing") return "no-project-key";
    if (refusal === "no-key-chosen") return "no-project-key-chosen";
    const c = getActiveTelemetryClient();
    // No endpoint / no install id means the kit never started in this page —
    // a fact about THIS process, not a guess about the server's records, so it
    // is still a definite "not registered".
    if (!c?.endpoint || !c?.installId) return "not-registered";
    // Everything else is the SERVER's answer, never browser-local state. The
    // delete token is deliberately not persisted by the runtime, so a second
    // browser holds none — that must never be read as a failed install.
    const verdict = getRegistrationVerdict();
    if (verdict === "unregistered") return "not-registered";
    if (verdict === "unknown" && !c.deleteToken) {
      // Unanswered AND nothing local to fall back on: say we cannot tell.
      return "registration-unknown";
    }
    if (!c.meterSharing) return "sharing-off";
    if (isLastUploadAttemptFailed() && getLastUploadFailure() !== null) {
      return "uploads-failing";
    }
    return null;
  } catch {
    return null;
  }
}

/** The panel-notice descriptor for the current kind, or null when there is
 *  nothing to say. Pure + display-only so parity tests can assert the copy
 *  without a DOM. Never throws — a failure fails OPEN (null). */
export function computePanelNoticeDescriptor(): {
  kind: PanelNoticeKind;
  title: string;
  body: string;
} | null {
  try {
    const kind = computePanelNotice();
    if (kind === null) return null;
    const copy = PANEL_NOTICE_COPY[kind];
    // When the kit knows WHY the registration was turned away, say that
    // instead of the generic "check the project key and network access" —
    // which was true of a revoked key, a paused account and a typo alike, and
    // therefore useful for none of them. The sentence comes from the same
    // single source the console line uses, so the two can never disagree.
    if (kind === "not-registered") {
      const reason = getRegistrationRefusalSentence();
      if (reason) return { kind, title: copy.title, body: reason };
    }
    if (kind === "registration-unknown") {
      // "Could not be reached to confirm" is the wrong story when the kit
      // never asked. A locked page and a page whose registration was thrown
      // away both land here with an unanswered verdict — correctly, because
      // neither knows what the server holds — but the panel can still say
      // which of the two it is instead of implying a check is in flight.
      const refusal = getRegistrationRefusalKind();
      if (refusal === "not-sent" || refusal === "never-attempted") {
        const reason = getRegistrationRefusalSentence();
        if (reason) return { kind, title: copy.title, body: reason };
      }
    }
    if (kind === "uploads-failing") {
      const failure = getLastUploadFailure();
      if (!failure) return null;
      const lost = getDroppedUploadCount();
      return {
        kind,
        title: copy.title,
        body: `${copy.body} ${panelUploadFailText(failure.reason)}.${lost > 0 ? ` Uploads lost: ${lost}.` : ""}`,
      };
    }
    return { kind, title: copy.title, body: copy.body };
  } catch {
    return null;
  }
}

function panelUploadFailText(reason: UploadFailReason): string {
  if (reason === "unauthorized") return "Refused — credentials rejected";
  if (reason === "rejected") return "Refused — batch rejected";
  if (reason === "server-error") return "Boosthis failed to store it";
  return "No answer — timed out or unreachable";
}
/** Cause markers ordered exactly like {@link PANEL_NOTICE_COPY}: code-defined
 *  keys the telemetry client hands us, never server text. The words shown in
 *  the panel are literals here in the renderer — the same rule the notice
 *  banner follows, so no server-provided text can ever be rendered. */
const DROP_CAUSE_LABELS: Record<string, string> = {
  labelRejected: "route names the privacy guard refused",
  traceCapReached: "spans past the 20-span limit",
  snapshotEntryFiltered: "snapshot entries the privacy guard refused",
};

/** The panel's drops line, or null when the server has never reported a
 *  dropped row this page. `count` is CUMULATIVE (the size of the hole in the
 *  dashboard; a later clean upload does not erase it) and `causes` are ordered
 *  code-defined markers. Null when nothing has been dropped, so a healthy app
 *  and an older server both show NOTHING. Fail-safe: any error degrades to
 *  null. */
export function computePanelDrops(): { count: number; text: string } | null {
  try {
    const count = getDroppedRowCount();
    if (count <= 0) return null;
    // The words are literals HERE, keyed off code-defined markers — never any
    // server-provided string. An unknown marker is counted in the total but
    // named nowhere.
    const words = getDroppedRowCauses()
      .map((c) => DROP_CAUSE_LABELS[c])
      .filter((w): w is string => typeof w === "string");
    const text = words.length > 0 ? `${count} \u2014 ${words.join("; ")}` : String(count);
    return { count, text };
  } catch {
    return null;
  }
}

/**
 * How the accumulated page map is being kept, in the panel's words.
 *
 * EVERY state gets its own words. "Kept" and "nothing written yet" are
 * different answers about a map of the same size, and a map that could not be
 * saved must never be readable as one that was — so no state is left to be
 * inferred from silence. Exhaustive over the union: adding a state without a
 * word here is a compile error, not a blank.
 */
export function pageMapPersistenceWord(p: PageMapPersistence): string {
  switch (p) {
    case "stored":
      return "kept on this device";
    case "memory-only":
      return "this visit only";
    case "unavailable":
      return "could not be saved";
    case "not-saved-yet":
      return "not written yet";
  }
}
const T = {
  bg: "#0b0c10",
  card: "#15171c",
  br: "#262932",
  fg: "#e6e7eb",
  mut: "#8b8f99",
  pri: "#f97316",
} as const;
const ORANGE = T.pri;
const COLORS: Record<Rating, string> = {
  good: "#4ade80",
  "needs-work": "#fbbf24",
  poor: "#f87171",
};
// The two silences that are NOT "come back later" — a reading this host cannot
// take, and a real reading that is deliberately never graded. Grey is reserved
// for "still measuring", so neither may share it or a permanent answer reads as
// progress toward a score. Same two colours as the served kit page and the
// dashboard tiles.
const SILENCE_COLORS: Readonly<Record<string, string>> = {
  "not-available": "#a371f7",
  "not-scored": "#58a6ff",
};
const NEUTRAL = T.mut;

/** Core web axes — the ones the composite page score is built from (weighted
 *  TTFB/LCP/INP). These render in the "Score breakdown" card; every other axis
 *  is re-housed in "Runtime health". Mirrors the Node kit's CORE split. */
const CORE_WEB_AXES: Record<string, 1> = {
  responsiveness: 1,
  frustration: 1,
  paintReadiness: 1,
};

const SIZE = 48;
const MARGIN = 14;

/** Hero subtitle for this runtime's panel — mirrors the RN kit's
 *  "React Native Performance" hero subtitle. */
const RUNTIME_PANEL_SUBTITLE = "Web Performance";

/** Gate copy — the first-open connect + agree screen. Ported from the RN
 *  BoosthisTermsGate, with dev-facing "app" wording adapted to "project".
 *  Exposed via `_getGateCopyForTests` so parity tests can assert the strings
 *  without a DOM. Display-only. */
const GATE_COPY = {
  kicker: "Boosthis",
  title: "Terms of Service & Privacy",
  intro: "Please review and accept before using Boosthis.",
  body:
    "Everything \u2014 liability, what Boosthis collects, AI suggestions, " +
    "and project-key access \u2014 lives on the Terms & Conditions page. " +
    "Open it and read it, then tick the box below to agree. ",
  checkLabel:
    "I have read and agree to the Terms of Service & Privacy Policy.",
  hint:
    "Sign in above to connect this project to your dashboard before continuing.",
  agree: "I Agree & Continue",
  decline: "Decline",
} as const;

let host: HTMLElement | null = null;
let refreshTimer: ReturnType<typeof setInterval> | null = null;
let accountCard: AccountCardHandle | null = null;
/** Unsubscribe from entitlement-gate changes, so a live revoke/restore flips
 *  the panel to/from the blocking lock overlay without waiting for the poll. */
let entitlementUnsub: (() => void) | null = null;
/** Unsubscribe from the registration answer, so the panel and the consent gate
 *  re-render the moment the server says whether this install is on file. */
let registrationUnsub: (() => void) | null = null;

/** Remove the bubble and stop its refresh loop. Safe to call repeatedly. */
export function unmountBubble(): void {
  try {
    if (refreshTimer != null) clearInterval(refreshTimer);
  } catch {
    // ignore
  }
  refreshTimer = null;
  try {
    entitlementUnsub?.();
  } catch {
    // ignore
  }
  entitlementUnsub = null;
  try {
    registrationUnsub?.();
  } catch {
    // ignore
  }
  registrationUnsub = null;
  try {
    accountCard?.destroy();
  } catch {
    // ignore
  }
  accountCard = null;
  try {
    host?.remove();
  } catch {
    // ignore
  }
  host = null;
}

/** @internal test hook. */
export function _resetBubbleForTests(): void {
  unmountBubble();
}

/** @internal test hook — the RN DARK_THEME palette + core-axis split + panel
 *  CSS the injected panel uses, exposed so parity tests can assert the panel
 *  matches the RN dashboard / Node bubble without a DOM. Display-only. */
export function _getPanelParityForTests(): {
  theme: typeof T;
  colors: typeof COLORS;
  coreWebAxes: typeof CORE_WEB_AXES;
  css: string;
  subtitle: string;
  brandIconUri: string;
} {
  return {
    theme: T,
    colors: COLORS,
    coreWebAxes: CORE_WEB_AXES,
    css: PANEL_CSS,
    subtitle: RUNTIME_PANEL_SUBTITLE,
    brandIconUri: BOOSTHIS_ICON_URI,
  };
}

/** @internal test hook — the gate-first "connect + agree" copy, so parity
 *  tests can assert the strings match the RN BoosthisTermsGate (dev-facing
 *  "app" wording adapted to "project") without a DOM. Display-only. */
export function _getGateCopyForTests(): typeof GATE_COPY {
  return GATE_COPY;
}

/** @internal test hook — the fixed install-ID-rejected card copy, so tests can
 *  assert the exact bytes (and that nothing dynamic can reach them) without a
 *  DOM. Display-only. */
export function _getInstallIdRejectedCopyForTests(): typeof INSTALL_ID_REJECTED_COPY {
  return INSTALL_ID_REJECTED_COPY;
}

/** @internal test hook — the blocking lock-overlay wiring facts (VAULT
 *  contract), so lockOverlay.test.ts can assert them without a jsdom DOM
 *  (the web test env is "node"). Mirrors the Node kit's bubbleSnippet string
 *  assertions: the a11y role + label, the cheap CSS blur scrim, and the
 *  overlay class names paint()/refresh() drive. */
export function _getLockOverlayForTests(): {
  role: string;
  ariaLabel: string;
  css: string;
  fallbackTitle: string;
} {
  return {
    role: "alertdialog",
    ariaLabel: "Boosthis locked",
    css: PANEL_CSS,
    fallbackTitle: "Boosthis is locked",
  };
}

const PANEL_CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    .wrap {
      position: fixed; z-index: 2147483000;
      right: ${MARGIN}px; bottom: ${MARGIN}px;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    .bubble {
      width: ${SIZE}px; height: ${SIZE}px; border-radius: 50%;
      background: ${T.bg}; border: 2.5px solid ${NEUTRAL};
      display: flex; align-items: center; justify-content: center;
      cursor: pointer; touch-action: none; user-select: none;
      box-shadow: 0 4px 14px rgba(0,0,0,0.4);
    }
    .bubble img { width: 34px; height: 34px; border-radius: 50%; display: block; pointer-events: none; }
    /* A PHONE IS A HOST APP'S FIRST SCREEN TOO. Capped against the viewport,
       and what does not fit scrolls — and SAYS it scrolls: iOS and Android
       draw an overlay scrollbar that only appears once you are already
       swiping, so a scrollbar alone is not a cue. Two 'local' cover layers
       and two 'scroll' shadows do it instead, kept byte-equal in behaviour
       with the nine hand-rolled bubbles in the other kits. The plain
       background declaration above is the fallback: an engine that cannot
       parse the gradient list drops that whole declaration, and without the
       first one the panel would render transparent. */
    .panel {
      display: none; position: absolute; right: 0; bottom: ${SIZE + 8}px;
      width: 300px; max-width: calc(100vw - ${MARGIN * 2}px);
      max-height: 72vh; overflow-y: auto; overscroll-behavior: contain;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: thin; scrollbar-color: ${T.mut} transparent;
      background: ${T.bg};
      background: linear-gradient(${T.bg} 50%, rgba(0,0,0,0)) top/100% 20px no-repeat local,
        linear-gradient(rgba(0,0,0,0), ${T.bg} 50%) bottom/100% 20px no-repeat local,
        radial-gradient(farthest-side at 50% 0, rgba(0,0,0,.6), rgba(0,0,0,0)) top/100% 10px no-repeat scroll,
        radial-gradient(farthest-side at 50% 100%, rgba(0,0,0,.6), rgba(0,0,0,0)) bottom/100% 10px no-repeat scroll,
        ${T.bg};
      color: ${T.fg};
      border: 1px solid ${T.br}; border-radius: 20px;
      box-shadow: 0 10px 28px rgba(0,0,0,0.5);
      padding: 12px; font: 12px/1.5 system-ui, sans-serif; text-align: left;
    }
    .panel.open { display: block; }
    .card {
      background: ${T.card}; border: 1px solid ${T.br}; border-radius: 16px;
      padding: 12px 14px; margin-bottom: 10px;
    }
    .card-title { font-size: 13px; font-weight: 700; color: ${T.fg}; }
    .card-sub { font-size: 10.5px; color: ${T.mut}; margin-top: 1px; margin-bottom: 2px; }
    .h-name { font-size: 16px; font-weight: 800; color: ${T.fg}; }
    .h-sub { font-size: 11px; color: ${T.mut}; }
    .h-project { font-size: 11px; color: ${T.mut}; margin-top: 3px; word-break: break-word; }
    .h-install { font-size: 10px; color: ${T.mut}; margin-top: 2px; word-break: break-all; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .h-score { text-align: center; padding: 10px 0 2px; }
    .h-num { font-size: 46px; font-weight: 800; line-height: 1; letter-spacing: -1px; color: ${T.fg}; }
    .h-of { font-size: 11px; color: ${T.mut}; margin-top: 2px; }
    .h-caption { text-align: center; margin-top: 5px; font-size: 10px; line-height: 1.4; color: ${T.mut}; }
    .h-badgewrap { text-align: center; }
    .badge {
      display: inline-flex; align-items: center; gap: 5px; margin-top: 8px;
      padding: 3px 12px; border-radius: 999px; font-size: 10px; font-weight: 700;
      letter-spacing: 0.6px; background: ${T.br}; color: ${T.mut}; border: 1px solid ${T.br};
    }
    .badge-dot { width: 6px; height: 6px; border-radius: 50%; background: ${T.mut}; }
    .h-stat { text-align: center; margin-top: 7px; font-size: 10.5px; color: ${T.mut}; }
    .formula { font-size: 10px; color: ${T.mut}; margin-top: 8px; }
    .meter { padding: 7px 0; border-bottom: 1px solid ${T.br}; }
    .meter:last-child { border-bottom: none; }
    .meter-top {
      display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
    }
    .meter-label { color: ${T.fg}; font-weight: 600; font-size: 12px; }
    .meter-cap {
      font-variant-numeric: tabular-nums; font-size: 11px; text-align: right; color: ${T.mut};
    }
    .meter-bar { height: 4px; border-radius: 3px; background: ${T.br}; margin-top: 5px; overflow: hidden; }
    .meter-fill { height: 100%; border-radius: 3px; width: 0%; }
    .row { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid ${T.br}; }
    .row:last-child { border-bottom: none; }
    .k { color: ${T.fg}; font-weight: 600; font-size: 12px; }
    .v { font-variant-numeric: tabular-nums; font-size: 11px; color: ${T.mut}; }
    .legend { display: flex; flex-wrap: wrap; gap: 6px 12px; font-size: 10.5px; color: ${T.mut}; }
    .legend-item { display: inline-flex; align-items: center; gap: 5px; }
    .legend-dot { width: 7px; height: 7px; border-radius: 50%; }
    .note { margin: 2px 2px 0; color: ${T.mut}; font-size: 10.5px; line-height: 1.5; }
    .ver { margin: 4px 2px 0; font-size: 10px; color: ${T.mut}; }
    .terms {
      display: inline-block; margin: 8px 2px 0; font-size: 11px;
      color: ${ORANGE}; text-decoration: none; cursor: pointer;
    }
    .terms:hover { text-decoration: underline; }
    .gate-kicker {
      font-size: 10px; font-weight: 700; letter-spacing: 2px;
      color: ${T.mut}; margin: 2px 2px 10px; text-transform: uppercase;
    }
    .gate-title { font-size: 20px; font-weight: 800; color: ${T.fg}; margin: 4px 2px 2px; }
    .gate-intro { font-size: 11px; color: ${T.mut}; margin: 0 2px 8px; }
    .gate-body { font-size: 11.5px; line-height: 1.6; color: ${T.mut}; margin: 0 2px 10px; }
    .gate-check {
      display: flex; align-items: flex-start; gap: 9px; cursor: pointer;
      user-select: none; margin: 4px 2px 6px;
    }
    .gate-box {
      flex: 0 0 auto; width: 20px; height: 20px; border-radius: 5px;
      border: 2px solid ${T.br}; background: transparent;
      display: flex; align-items: center; justify-content: center;
      font-size: 12px; font-weight: 700; line-height: 1; color: ${T.bg};
    }
    .gate-box.on { border-color: ${ORANGE}; background: ${ORANGE}; }
    .gate-check-label { font-size: 11.5px; line-height: 1.5; color: ${T.fg}; }
    .gate-hint { font-size: 10.5px; line-height: 1.5; color: ${T.mut}; margin: 2px 2px 6px; }
    .gate-agree {
      width: 100%; padding: 11px 10px; font-size: 13px; font-weight: 700;
      border: none; border-radius: 12px; margin-top: 8px;
      background: ${ORANGE}; color: ${T.bg}; cursor: pointer;
    }
    .gate-agree.off { background: ${T.card}; color: ${T.mut}; cursor: default; }
    .gate-decline {
      width: 100%; padding: 8px 10px; font-size: 12px; font-weight: 600;
      background: transparent; color: ${T.mut}; border: none;
      cursor: pointer; margin-top: 4px;
    }
    /* Registration-rejected notice — the server refused to register this app
       because its install ID is not a UUID. Shown ABOVE both the gate and the
       dashboard so the panel can never read as connected/awaiting while the
       app is in fact unregistered. Fixed, code-defined copy only. */
    .reject {
      display: none; background: ${T.card}; border: 1px solid ${COLORS.poor};
      border-radius: 16px; padding: 12px 14px; margin-bottom: 10px;
    }
    .reject.on { display: block; }
    .reject-title { font-size: 12.5px; font-weight: 700; color: ${COLORS.poor}; }
    .reject-body { font-size: 11px; line-height: 1.5; color: ${T.mut}; margin-top: 3px; }
    /* Blocking lock overlay (VAULT contract, owner UX). A full-cover layer that
       dims + BLURS everything behind it (CSS backdrop-filter — the cheap web
       approximation, no deps). Being an opaque full-cover layer that absorbs
       pointer events (pointer-events:auto by default), nothing of the meters can
       be read, tapped, or scrolled through. Copy is keyed off the server-computed
       lock kind; no server internals, no action button, no in-kit payment.
       Kept byte-equal to the RN/Node dashboard overlay copy. */
    .lock {
      display: none; position: absolute; top: 0; left: 0; right: 0; bottom: 0;
      z-index: 10; box-sizing: border-box; border-radius: 20px; padding: 32px 22px;
      flex-direction: column; align-items: center; justify-content: center;
      text-align: center;
      background: rgba(11,12,16,0.92);
      -webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px);
    }
    .lock.on { display: flex; }
    .lock-title { font-size: 17px; font-weight: 700; color: ${T.fg}; margin-bottom: 8px; }
    .lock-body { font-size: 13px; line-height: 1.5; color: ${T.mut}; max-width: 240px; }
`;

interface PanelRefs {
  panel: HTMLElement;
  /** The registration-rejected notice card (fixed copy; toggled by refresh). */
  rejectCard: HTMLElement;
  rejectTitle: HTMLElement;
  rejectBody: HTMLElement;
  heroProject: HTMLElement;
  /** The install id this copy of the kit is using (comparable with the
   *  dashboard, so nobody has to guess which install the panel describes). */
  heroInstall: HTMLElement;
  heroNum: HTMLElement;
  heroBadge: HTMLElement;
  heroBadgeDot: HTMLElement;
  heroBadgeTxt: HTMLElement;
  heroStat: HTMLElement;
  meterRows: Map<string, { cap: HTMLElement; fill: HTMLElement; label: HTMLElement }>;
  vLong: HTMLElement;
  vDead: HTMLElement;
  vUnreached: HTMLElement;
  vPageMap: HTMLElement;
  vCircuit: HTMLElement;
  /** "Press this, it opens that" — arrows from a control to the page it
   *  opened, dead ends, and how many presses came from a control carrying no
   *  code-defined test id. On-device only (controlMap.ts). */
  vControls: HTMLElement;
  /** How many controls the page HAS, how many have been pressed, and the
   *  reason whenever there is no count at all (controlCensus.ts). Counts
   *  and structural handles only — never a label. */
  vCensus: HTMLElement;
  /** The drawn pairs themselves: control on the left, "from \u2192 to" on the
   *  right. Pre-built, hidden until there is something to draw. */
  controlRows: { row: HTMLElement; k: HTMLElement; v: HTMLElement }[];
  /** The caption stating what the drawn rows are true of. */
  controlScope: HTMLElement;
  vViews: HTMLElement;
  viewsScope: HTMLElement;
  /** "Measurements dropped" card — hidden until the server reports a dropped
   *  row, so a healthy app and an older server show nothing at all. */
  dropCard: HTMLElement;
  dropValue: HTMLElement;
  /** Masked project key (last four characters only) + where the value was read
   *  from. Always shown, including when there is no key at all — "not set" is
   *  the answer this panel most needs to be able to give. */
  keyValue: HTMLElement;
  keySourceValue: HTMLElement;
}

function buildAndAttach(): void {
  if (host) return; // idempotent
  const doc = document;
  host = doc.createElement("div");
  host.setAttribute("data-boosthis-bubble", "");
  const root = host.attachShadow({ mode: "closed" });

  const style = doc.createElement("style");
  style.textContent = PANEL_CSS;
  root.appendChild(style);

  const wrap = doc.createElement("div");
  wrap.className = "wrap";
  root.appendChild(wrap);

  const panel = doc.createElement("div");
  panel.className = "panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Boosthis performance dashboard");
  wrap.appendChild(panel);

  const el = (tag: string, cls?: string, text?: string): HTMLElement => {
    const e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };

  // Two mutually-exclusive containers inside the single 300px panel: the
  // first-open GATE (connect + agree) and the normal dashboard. syncGate()
  // toggles which is visible from the persisted acceptance state; the account
  // card DOM node lives in exactly one at a time and reparents on accept.
  // Registration-rejected notice — appended FIRST so it sits above both the
  // gate and the dashboard. Hidden unless the server refused this app's
  // registration because its install ID is not a UUID; the copy is fixed at
  // build time (never a server- or developer-supplied string).
  const rejectCard = el("div", "reject");
  const rejectTitle = el("div", "reject-title", INSTALL_ID_REJECTED_COPY.title);
  const rejectBody = el("div", "reject-body", INSTALL_ID_REJECTED_COPY.body);
  rejectCard.appendChild(rejectTitle);
  rejectCard.appendChild(rejectBody);
  panel.appendChild(rejectCard);

  const gateWrap = el("div");
  const dashWrap = el("div");
  panel.appendChild(gateWrap);
  panel.appendChild(dashWrap);

  // Blocking lock overlay — appended LAST so it covers both the gate and the
  // dashboard. Hidden by default; showLock() reveals it for a {kind,title,body}
  // descriptor and freezes scrolling behind it. It absorbs pointer events, so
  // no tap passes through to the meters. role=alertdialog + a11y label.
  const lockEl = el("div", "lock");
  lockEl.setAttribute("role", "alertdialog");
  lockEl.setAttribute("aria-label", "Boosthis locked");
  const lockTitle = el("div", "lock-title");
  const lockBody = el("div", "lock-body");
  lockEl.appendChild(lockTitle);
  lockEl.appendChild(lockBody);
  panel.appendChild(lockEl);

  /** Render the overlay for a {kind,title,body} descriptor and block the panel
   *  body behind it. Never throws into the host. */
  const showLock = (lock: PanelLock): void => {
    try {
      lockTitle.textContent = String(lock.title || "Boosthis is locked");
      lockBody.textContent = String(lock.body || "");
      lockEl.classList.add("on");
      // Freeze scrolling behind the cover so the meters cannot be scrolled to.
      panel.style.overflowY = "hidden";
    } catch {
      // ignore
    }
  };
  /** Restore the normal panel (overlay down, scrolling back). */
  const hideLock = (): void => {
    try {
      lockEl.classList.remove("on");
      panel.style.overflowY = "auto";
    } catch {
      // ignore
    }
  };

  // Hero card — name, runtime subtitle, project identity, big score + honest
  // local-only caption, rating pill, status line.
  const hero = el("div", "card");
  hero.appendChild(el("div", "h-name", "Boosthis"));
  hero.appendChild(el("div", "h-sub", RUNTIME_PANEL_SUBTITLE));
  const heroProject = el(
    "div",
    "h-project",
    `${PROJECT_LABEL}: ${projectDisplay(getKitProject())}`,
  );
  hero.appendChild(heroProject);
  // The identity this copy of the kit is actually using. Printed so it can be
  // compared with the dashboard without guessing: when the panel and the
  // dashboard disagree, the reader can SEE which install is being described
  // instead of inferring it (the fault that let a rotated identity hide).
  const heroInstall = el("div", "h-install", INSTALL_ID_UNKNOWN_TEXT);
  hero.appendChild(heroInstall);
  const gw = el("div", "h-score");
  const heroNum = el("div", "h-num", "\u2013");
  const heroOf = el("div", "h-of", "/ 100");
  gw.appendChild(heroNum);
  gw.appendChild(heroOf);
  hero.appendChild(gw);
  hero.appendChild(el("div", "h-caption", SCORE_CAPTION));
  const heroBadge = el("div", "badge");
  const heroBadgeDot = el("span", "badge-dot");
  const heroBadgeTxt = el("span", undefined, "MEASURING");
  heroBadge.appendChild(heroBadgeDot);
  heroBadge.appendChild(heroBadgeTxt);
  const badgeWrap = el("div", "h-badgewrap");
  badgeWrap.appendChild(heroBadge);
  hero.appendChild(badgeWrap);
  const heroStat = el("div", "h-stat", MEASURING_HERO_TEXT);
  hero.appendChild(heroStat);
  dashWrap.appendChild(hero);

  const meterRows: PanelRefs["meterRows"] = new Map();
  // Build one meter row and register its refs. `box` is the card the row lives
  // in. Same DOM shape used for core (Score breakdown) and rest (Runtime
  // health) axes so both look identical to the app kit.
  const addMeter = (box: HTMLElement, row: PanelMeterRow): void => {
    const meter = el("div", "meter");
    const top = el("div", "meter-top");
    const label = el("span", "meter-label", row.label);
    const cap = el("span", "meter-cap", row.caption);
    top.appendChild(label);
    top.appendChild(cap);
    const bar = el("div", "meter-bar");
    const fill = el("div", "meter-fill");
    bar.appendChild(fill);
    meter.appendChild(top);
    meter.appendChild(bar);
    box.appendChild(meter);
    meterRows.set(row.key, { cap, fill, label });
  };

  // Seed with a pending snapshot so the rows exist immediately; refresh fills.
  let seed: PanelMeterRow[] = [];
  try {
    seed = buildPanelRows(capturePerfSnapshot());
  } catch {
    seed = [];
  }

  // Score breakdown — the composite (weighted TTFB/LCP/INP) core web axes with
  // bars + an honest formula footer, exactly like the app kit's breakdown card.
  const breakdownCard = el("div", "card");
  breakdownCard.appendChild(el("div", "card-title", "Score breakdown"));
  breakdownCard.appendChild(el("div", "card-sub", "this page · lower is better"));
  for (const row of seed) {
    if (CORE_WEB_AXES[row.key]) addMeter(breakdownCard, row);
  }
  breakdownCard.appendChild(
    el(
      "div",
      "formula",
      "score = weighted TTFB \u00b7 LCP \u00b7 INP \u00b7 good\u226585 \u00b7 needs-work\u226560",
    ),
  );
  dashWrap.appendChild(breakdownCard);

  // Runtime health — every remaining axis, plus the web-only live event meters
  // (main-thread blocks, dead clicks, unreachable routes, loops/bursts). All
  // measured on-device; re-housed into the same card system as the app kit.
  const healthCard = el("div", "card");
  healthCard.appendChild(el("div", "card-title", "Runtime health"));
  healthCard.appendChild(
    el("div", "card-sub", "on-device meters · shown when measurable"),
  );
  for (const row of seed) {
    if (!CORE_WEB_AXES[row.key]) addMeter(healthCard, row);
  }
  const addRow = (labelText: string): HTMLElement => {
    const row = el("div", "row");
    row.appendChild(el("span", "k", labelText));
    const v = el("span", "v", "—");
    row.appendChild(v);
    healthCard.appendChild(row);
    return v;
  };
  const vLong = addRow("Main-thread blocks");
  const vDead = addRow("Dead clicks");
  const vUnreached = addRow("Unreachable routes");
  const vPageMap = addRow("Page map");
  const vCircuit = addRow("Loops / bursts");
  const vControls = addRow("Controls → pages");
  // How many controls the page HAS, not only the ones somebody pressed:
  // the row above can never speak about an untouched control, and an
  // untouched control is the thing this row exists to show.
  const vCensus = addRow("Controls on page");
  // The map itself, beneath its summary: WHICH control opened WHICH page.
  // A tally alone would tell a developer an arrow exists without telling
  // them what it points at, which is the whole question this answers.
  const controlRows: { row: HTMLElement; k: HTMLElement; v: HTMLElement }[] = [];
  for (let i = 0; i <= CONTROL_MAP_PANEL_ROWS; i++) {
    const row = el("div", "row");
    const k = el("span", "k", "");
    const v = el("span", "v", "");
    row.appendChild(k);
    row.appendChild(v);
    row.style.display = "none";
    healthCard.appendChild(row);
    controlRows.push({ row, k, v });
  }
  const controlScope = el("div", "formula", CONTROL_MAP_SCOPE_CAPTION);
  controlScope.style.display = "none";
  healthCard.appendChild(controlScope);
  dashWrap.appendChild(healthCard);

  // Connection card — WHICH project key this page is using, and where that
  // value came from.
  //
  // The kit has always worked both of these out and shown neither, which is
  // the one thing that would have made a missing key obvious on the one screen
  // where the failure actually lands. Every other Boosthis kit displays them;
  // this one now does too. Only the last four characters are ever shown — a
  // tail of a key the developer already holds — never the key, and never a
  // token, endpoint or server string.
  const connCard = el("div", "card");
  connCard.appendChild(el("div", "card-title", "Connection"));
  connCard.appendChild(
    el("div", "card-sub", "which key this page is using"),
  );
  const keyRow = el("div", "row");
  keyRow.appendChild(el("span", "k", "Project key"));
  const keyValue = el("span", "v", "\u2014");
  keyRow.appendChild(keyValue);
  connCard.appendChild(keyRow);
  const keySourceRow = el("div", "row");
  keySourceRow.appendChild(el("span", "k", "Read from"));
  const keySourceValue = el("span", "v", "\u2014");
  keySourceRow.appendChild(keySourceValue);
  connCard.appendChild(keySourceRow);
  dashWrap.appendChild(connCard);

  // "Measurements dropped" card — the server accepted the batch and then
  // refused individual rows (a route label the privacy guard threw away, a
  // span past the 20-span cap, a rejected snapshot entry). Hidden until that
  // actually happens, so a healthy app and an older server show nothing at all.
  // Fixed heading; the value carries a cumulative count + code-defined cause
  // words assembled in the renderer, never any server-provided string.
  const dropCard = el("div", "card");
  dropCard.style.display = "none";
  dropCard.appendChild(el("div", "card-title", "Measurements dropped"));
  const dropRow = el("div", "row");
  dropRow.appendChild(el("span", "k", "Dropped"));
  const dropValue = el("span", "v", "—");
  dropRow.appendChild(dropValue);
  dropCard.appendChild(dropRow);
  dashWrap.appendChild(dropCard);

  // Account card — mounted into its own container. Talks to hosted auth
  // endpoints (sign-in / claim); all handlers are wrapped inside the module.
  // The container starts on the GATE (the connect card is on the gate) and is
  // reparented to this dashboard slot on accept. It is created here and inserted
  // into the gate below; `dashAccountSlot` marks where it lands post-accept.
  const accountContainer = el("div");
  const dashAccountSlot = el("div");
  dashWrap.appendChild(dashAccountSlot);
  // LINKED flag — set only by a verified claim (see bubbleAccount.ts). Mere
  // sign-in never sets it; the gate reads it to compute `connected`.
  let connected = false;
  try {
    accountCard = mountAccountCard(doc, accountContainer, {
      onConnectedChange: (c) => {
        connected = c;
        try {
          syncGate();
        } catch {
          // never throw into the host
        }
      },
      onSignedOut: () => {
        try {
          handleSignedOut();
        } catch {
          // never throw into the host
        }
      },
      // A fresh install registers SECONDS after the page loaded. The card's
      // auto-refresh poll fires this when installId/deleteToken finally land,
      // so the gate re-resolves against the now-known install with no page
      // reload (registered flips true, the connect hint appears).
      onContextChange: () => {
        try {
          syncGate();
        } catch {
          // never throw into the host
        }
      },
    });
  } catch {
    accountCard = null;
  }

  // Legend — same rating legend strip as the app kit / Node bubble.
  const legend = el("div", "card legend");
  const lgItem = (color: string, tx: string): HTMLElement => {
    const item = el("span", "legend-item");
    const dot = el("span", "legend-dot");
    dot.style.background = color;
    item.appendChild(dot);
    item.appendChild(el("span", undefined, tx));
    return item;
  };
  legend.appendChild(lgItem(COLORS.good, "Good \u226585"));
  legend.appendChild(lgItem(COLORS["needs-work"], "Needs work \u226560"));
  legend.appendChild(lgItem(COLORS.poor, "Poor <60"));
  dashWrap.appendChild(legend);

  // About footer — page-views count, the privacy note, kit version, terms link.
  const viewsRow = el("div", "row");
  viewsRow.appendChild(el("span", "k", "Page views measured"));
  const vViews = el("span", "v", "0");
  viewsRow.appendChild(vViews);
  // What that count covers, in words. The count spans page loads on this
  // device, which is wider than the panel could ever show before — and where
  // this browser keeps nothing, it is narrower. Either way the scope is said
  // rather than left to be guessed at (pageViewTally.ts).
  const viewsScope = el("div", "card-sub", "");
  const foot = el("div", "card");
  foot.appendChild(el("div", "card-title", "About"));
  foot.appendChild(viewsRow);
  foot.appendChild(viewsScope);
  dashWrap.appendChild(foot);

  dashWrap.appendChild(
    el(
      "div",
      "note",
      "Local dev meters \u2014 measured on this device only, nothing on this panel leaves the page. Signing in above uses your own Boosthis account credentials.",
    ),
  );
  dashWrap.appendChild(el("div", "ver", `Boosthis web kit ${RUNTIME_VERSION}`));

  // Shared Terms & Privacy href — same logic used by the gate link and the
  // dashboard footer link (…/api → origin + /terms, public fallback).
  const resolveTerms = (): string => {
    try {
      const ep = getActiveTelemetryClient()?.endpoint;
      return resolveTermsUrl(ep ?? "https://www.boosthis.com/api");
    } catch {
      return "https://www.boosthis.com/terms";
    }
  };

  const terms = doc.createElement("a");
  terms.className = "terms";
  terms.textContent = "Terms & Privacy";
  terms.setAttribute("target", "_blank");
  terms.setAttribute("rel", "noopener noreferrer");
  terms.setAttribute("href", resolveTerms());
  dashWrap.appendChild(terms);

  // ---- GATE (first-open connect + agree) --------------------------------
  // Port of the RN BoosthisTermsGate. Top to bottom: kicker, the account
  // (connect) card, heading + intro, body copy with a Terms & Privacy link,
  // the acceptance checkbox, a needsConnect hint, the primary Agree button,
  // and a ghost Decline. Acceptance state (persisted via terms.ts, with an
  // in-memory session fallback) decides whether the gate or the dashboard is
  // visible; the account card node reparents from the gate to the dashboard on
  // accept.

  // In-memory accept for this page session, used only when localStorage is
  // unavailable so a developer is never trapped in a re-gate loop after
  // agreeing (the persisted helper simply can't remember it across reloads).
  let sessionAccepted = false;

  const gateKicker = el("div", "gate-kicker", GATE_COPY.kicker);
  gateWrap.appendChild(gateKicker);

  // The connect card lives on the gate first (reparented to dashAccountSlot on
  // accept). accountContainer was mounted above.
  gateWrap.appendChild(accountContainer);

  gateWrap.appendChild(el("div", "gate-title", GATE_COPY.title));
  gateWrap.appendChild(el("div", "gate-intro", GATE_COPY.intro));

  const gateBody = el("div", "gate-body");
  gateBody.appendChild(doc.createTextNode(GATE_COPY.body));
  const gateTermsLink = doc.createElement("a");
  gateTermsLink.className = "terms";
  gateTermsLink.textContent = "Terms & Privacy";
  gateTermsLink.setAttribute("target", "_blank");
  gateTermsLink.setAttribute("rel", "noopener noreferrer");
  gateTermsLink.setAttribute("href", resolveTerms());
  gateTermsLink.style.margin = "0";
  gateBody.appendChild(gateTermsLink);
  gateWrap.appendChild(gateBody);

  // Checkbox row.
  let checked = false;
  const checkRow = el("div", "gate-check");
  checkRow.setAttribute("role", "checkbox");
  checkRow.setAttribute("aria-checked", "false");
  const checkBox = el("span", "gate-box");
  const checkLabel = el("span", "gate-check-label", GATE_COPY.checkLabel);
  checkRow.appendChild(checkBox);
  checkRow.appendChild(checkLabel);
  gateWrap.appendChild(checkRow);

  // needsConnect hint (shown only when registered but not yet linked).
  const gateHint = el("div", "gate-hint", GATE_COPY.hint);
  gateHint.style.display = "none";
  gateWrap.appendChild(gateHint);

  const agreeBtn = el("button", "gate-agree", GATE_COPY.agree);
  agreeBtn.setAttribute("type", "button");
  gateWrap.appendChild(agreeBtn);

  const declineBtn = el("button", "gate-decline", GATE_COPY.decline);
  declineBtn.setAttribute("type", "button");
  gateWrap.appendChild(declineBtn);

  // registered := BOOSTHIS's answer about this install, not this browser's
  // storage (see computeGateSignInRequired for the whole rule). connected :=
  // a verified claim fired (LINKED flag). ONE implementation, shared with the
  // Agree handler below so the two can never drift apart.

  /** Ask the server whether this install is on file. Fire-and-forget, self-
   *  throttled, and never awaited by the UI: the gate and the panel re-render
   *  from the subscription below when the answer lands. */
  const askRegistration = (): void => {
    try {
      const c = getActiveTelemetryClient();
      void checkRegistrationOnce({
        endpoint: c?.endpoint ?? null,
        installId: c?.installId ?? null,
        projectKey: getActiveProjectKey().key,
      });
    } catch {
      // never block the panel
    }
  };

  // True once the current terms version is accepted (persisted OR this session).
  const isAccepted = (): boolean => {
    if (sessionAccepted) return true;
    try {
      // Keyed to the CURRENT installId: a fresh install (new installId) does
      // not inherit an old acceptance — the full gate flow runs again.
      return hasAcceptedCurrentTerms(
        getActiveTelemetryClient()?.installId ?? null,
      );
    } catch {
      return false;
    }
  };

  /** Toggle gate vs. dashboard + refresh the gate's live enable/hint state.
   *  Hoisted so the account card's onConnectedChange (defined above) can call
   *  it. Never throws into the host. */
  function syncGate(): void {
    try {
      const accepted = isAccepted();
      if (accepted) {
        // Move the connect card node into its dashboard slot (idempotent —
        // reparenting a node that is already there is a no-op) and show the
        // dashboard.
        if (accountContainer.parentNode !== dashAccountSlot) {
          dashAccountSlot.appendChild(accountContainer);
        }
        gateWrap.style.display = "none";
        dashWrap.style.display = "block";
        return;
      }
      // Gate is showing: keep the connect card on the gate.
      if (accountContainer.parentNode !== gateWrap) {
        // Re-insert before the title (right after the kicker).
        gateWrap.insertBefore(accountContainer, gateKicker.nextSibling);
      }
      gateWrap.style.display = "block";
      dashWrap.style.display = "none";

      const { registered, needsConnect, canAgree } = computeGateState({
        checked,
        connected,
      });
      gateHint.style.display = needsConnect ? "block" : "none";
      if (canAgree) {
        agreeBtn.classList.remove("off");
      } else {
        agreeBtn.classList.add("off");
      }
    } catch {
      // never throw into the host
    }
  }

  /** Sign-out restarts the WHOLE gate flow (owner requirement, Jul 2026):
   *  wipe the persisted terms acceptance, drop the session/connected flags,
   *  reset the checkbox, and swap back to the gate so the developer walks
   *  sign-in → telemetry → terms again. Hoisted so the account card's
   *  onSignedOut (wired above) can call it. Never throws into the host. */
  function handleSignedOut(): void {
    try {
      clearTermsAcceptance();
    } catch {
      // best-effort
    }
    sessionAccepted = false;
    connected = false;
    checked = false;
    try {
      checkBox.classList.remove("on");
      checkBox.textContent = "";
      checkRow.setAttribute("aria-checked", "false");
    } catch {
      // DOM reset is cosmetic
    }
    syncGate();
  }

  // Checkbox toggle.
  try {
    checkRow.addEventListener("click", () => {
      try {
        checked = !checked;
        checkBox.classList.toggle("on", checked);
        checkBox.textContent = checked ? "\u2713" : "";
        checkRow.setAttribute("aria-checked", checked ? "true" : "false");
        syncGate();
      } catch {
        // ignore
      }
    });
  } catch {
    // no listeners — the gate still renders
  }

  // Agree: write acceptance (guarded), keep an in-memory flag as a fallback,
  // then swap to the dashboard. The fire-and-forget local record fires HERE
  // (on the Agree tap), not merely on open.
  try {
    agreeBtn.addEventListener("click", () => {
      try {
        const { registered, canAgree } = computeGateState({
          checked,
          connected,
        });
        if (!canAgree) return; // disabled state is a no-op
        try {
          recordTermsAccepted(
            Date.now(),
            getActiveTelemetryClient()?.installId ?? null,
          );
        } catch {
          // storage may be unavailable — the session flag still unblocks us
        }
        sessionAccepted = true;
        syncGate();
        try {
          accountCard?.refresh();
        } catch {
          // ignore
        }
        refresh();
      } catch {
        // ignore
      }
    });
  } catch {
    // ignore
  }

  // Decline: close the panel; nothing recorded.
  try {
    declineBtn.addEventListener("click", () => {
      try {
        panel.classList.remove("open");
      } catch {
        // ignore
      }
    });
  } catch {
    // ignore
  }

  const refs: PanelRefs = {
    panel,
    rejectCard,
    rejectTitle,
    rejectBody,
    heroProject,
    heroInstall,
    heroNum,
    heroBadge,
    heroBadgeDot,
    heroBadgeTxt,
    heroStat,
    meterRows,
    vLong,
    vDead,
    vUnreached,
    vPageMap,
    vCircuit,
    vControls,
    vCensus,
    controlRows,
    controlScope,
    vViews,
    viewsScope,
    dropCard,
    dropValue,
    keyValue,
    keySourceValue,
  };

  // Bubble icon
  const bubble = el("div", "bubble");
  bubble.setAttribute("role", "button");
  bubble.setAttribute("aria-label", "Boosthis performance");
  const glyph = doc.createElement("img");
  glyph.setAttribute("src", BOOSTHIS_ICON_URI);
  glyph.setAttribute("alt", "");
  bubble.appendChild(glyph);
  wrap.appendChild(bubble);

  const refresh = () => {
    try {
      // VAULT contract: a locked verdict swaps the panel to the blocking
      // overlay and stops here — the meters are never painted while locked, so
      // there is nothing to read behind the cover. A "hidden" verdict silently
      // removes the whole bubble (env-kill / tampered / grace-expired). A page
      // that has never checked in is NOT locked and NOT hidden: it falls
      // through and paints its "Not registered yet" notice.
      // Fail-open: computePanelLock() returns null on error.
      const lock = computePanelLock();
      if (lock) {
        if (lock.kind === "hidden") {
          try {
            if (host) host.style.display = "none";
          } catch {
            // ignore
          }
          return;
        }
        showLock(lock);
        return;
      }
      // Not locked: ensure the overlay is down and the bubble is visible again
      // (a paused/revoked verdict can be reversed by the owner mid-session).
      try {
        if (host) host.style.display = "";
      } catch {
        // ignore
      }
      hideLock();
      refreshPanel(refs, bubble);
    } catch {
      // display refresh must never throw into the host page
    }
  };

  // Drag + click (pointer events; a small move threshold separates the two).
  let dragging = false;
  let moved = false;
  let startX = 0;
  let startY = 0;
  let baseRight = MARGIN;
  let baseBottom = MARGIN;
  try {
    bubble.addEventListener("pointerdown", (e) => {
      try {
        dragging = true;
        moved = false;
        startX = e.clientX;
        startY = e.clientY;
        bubble.setPointerCapture(e.pointerId);
      } catch {
        dragging = false;
      }
    });
    bubble.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      try {
        const dx = e.clientX - startX;
        const dy = e.clientY - startY;
        if (!moved && Math.hypot(dx, dy) < 6) return;
        moved = true;
        const right = Math.max(
          4,
          Math.min(window.innerWidth - SIZE - 4, baseRight - dx),
        );
        const bottom = Math.max(
          4,
          Math.min(window.innerHeight - SIZE - 4, baseBottom - dy),
        );
        wrap.style.right = `${right}px`;
        wrap.style.bottom = `${bottom}px`;
      } catch {
        // ignore
      }
    });
    bubble.addEventListener("pointerup", (e) => {
      if (!dragging) return;
      dragging = false;
      try {
        bubble.releasePointerCapture(e.pointerId);
      } catch {
        // ignore
      }
      try {
        baseRight = parseFloat(wrap.style.right || `${MARGIN}`) || MARGIN;
        baseBottom = parseFloat(wrap.style.bottom || `${MARGIN}`) || MARGIN;
        if (!moved) {
          panel.classList.toggle("open");
          if (panel.classList.contains("open")) {
            // VAULT contract on-open enforcement edge: reconfirm entitlement
            // with the server when the panel opens (throttled ≤1/60s inside
            // forceEntitlementCheck). A revoked/unpaid verdict landing while
            // the panel is open swaps it to the blocking overlay via the
            // subscription below.
            try {
              forceEntitlementCheck();
            } catch {
              // never block opening the panel
            }
            // Decide gate vs. dashboard from the persisted acceptance state,
            // then refresh the account card + live meters. acctLoad-equivalent
            // (accountCard.refresh) runs whether the gate OR the dashboard is
            // shown, since the connect card lives on the gate too.
            try {
              syncGate();
            } catch {
              // never block opening the panel
            }
            // Ask Boosthis whether this install is on file. The panel's
            // registration verdict is OUR answer, never this browser's
            // storage — the fault that let one live install read as
            // "registered" in the tab that first registered it and "not
            // registered" everywhere else.
            askRegistration();
            try {
              accountCard?.refresh();
            } catch {
              // ignore
            }
            refresh();
          }
        }
      } catch {
        // ignore
      }
    });
    bubble.addEventListener("pointercancel", () => {
      dragging = false;
    });
  } catch {
    // no pointer events — bubble still shows, just not draggable
  }

  // Initial gate/dashboard split from persisted acceptance (panel is hidden
  // until first tap; this just sets which container will show when it opens).
  syncGate();

  // Ask once at mount too, so the verdict is usually settled before the first
  // open (and so the gate's sign-in requirement is right on the first paint).
  askRegistration();

  refresh();
  refreshTimer = setInterval(refresh, 2000);

  // Re-render the instant the registration answer lands: the panel notice AND
  // the gate both change with it (a confirmed registration re-arms the
  // sign-in requirement; a confirmed inability opens the no-lockout hatch).
  try {
    registrationUnsub = subscribeRegistration(() => {
      try {
        syncGate();
      } catch {
        // never throw into the host
      }
      try {
        refresh();
      } catch {
        // never throw into the host
      }
    });
  } catch {
    registrationUnsub = null;
  }

  // Re-render the instant the entitlement gate flips (revoke/restore/pause), so
  // an open panel swaps to/from the blocking lock overlay without waiting for
  // the 2s poll. Never throws into the host.
  try {
    entitlementUnsub = subscribeEntitlement(() => {
      try {
        refresh();
      } catch {
        // never throw into the host
      }
    });
  } catch {
    entitlementUnsub = null;
  }

  doc.body.appendChild(host);
}

/** Update the panel's live values from on-device meters. Never throws. */
function refreshPanel(refs: PanelRefs, bubble: HTMLElement): void {
  // Project identity can arrive after the DOM is built, so refresh it with the
  // live module state. textContent is mandatory: the name is untrusted display
  // data and must never be interpreted as markup.
  try {
    refs.heroProject.textContent =
      `${PROJECT_LABEL}: ${projectDisplay(getKitProject())}`;
  } catch {
    refs.heroProject.textContent = `${PROJECT_LABEL}: Not received from Boosthis yet`;
  }

  // The identity this copy of the kit holds, printed every paint so it tracks a
  // late-arriving (or rotated) id. textContent only — never markup.
  try {
    refs.heroInstall.textContent = computePanelInstallLine();
  } catch {
    // display-only
  }

  // Panel notice. Toggled every paint so a state that lands after the panel
  // was built still surfaces, and so the panel never keeps claiming "awaiting
  // telemetry" while the app is in fact unregistered, id-rejected, or silent.
  // Ordered (install-id-not-uuid → not-registered → sharing-off → nothing);
  // fixed, code-defined copy only. The card carries whichever notice fires:
  //   - install-id-not-uuid: the server refused the id outright;
  //   - not-registered: the kit never registered (no key / no reach);
  //   - sharing-off: registered, but the dashboard stays empty because sharing
  //     is off — the quiet failure this notice exists to end.
  const notice = computePanelNoticeDescriptor();
  try {
    if (notice) {
      refs.rejectTitle.textContent = notice.title;
      refs.rejectBody.textContent = notice.body;
      refs.rejectCard.classList.add("on");
    } else {
      refs.rejectCard.classList.remove("on");
    }
  } catch {
    // the notice is display-only — never break the rest of the paint
  }

  // Which key this page is using, refreshed every paint because the browser is
  // the one runtime whose key can arrive AFTER measuring has already begun —
  // the documented install starts the meters first and hands over the key on
  // the following line. Only the masked tail ever reaches the DOM; textContent
  // only, never markup.
  try {
    const keyStatus = getActiveProjectKeyStatus();
    refs.keyValue.textContent = keyStatus.display ?? "not set";
    refs.keySourceValue.textContent = keyStatus.description;
  } catch {
    refs.keyValue.textContent = "not set";
    refs.keySourceValue.textContent = "no project key configured";
  }

  const pulse = computePagePulse();
  const color = pulse.rating ? COLORS[pulse.rating] : NEUTRAL;

  // Bubble border from the composite pulse.
  bubble.style.borderColor = color;

  // Hero — big score, rating pill (dot + label, color+"1a" bg / color+"55"
  // border like the app kit), and the status line.
  if (pulse.score != null && pulse.rating != null) {
    refs.heroNum.textContent = `${pulse.score}`;
    refs.heroNum.style.color = color;
    refs.heroBadgeTxt.textContent = ratingLabel(pulse.rating);
    refs.heroBadgeDot.style.background = color;
    refs.heroBadge.style.background = `${color}1a`;
    refs.heroBadge.style.borderColor = `${color}55`;
    refs.heroBadge.style.color = color;
    // The count of readings, and — on an app whose work the kit cannot see as
    // page views — what it saw instead. Alone, "1 page view measured" on a
    // storefront that took a hundred orders reads as a quiet app.
    //
    // The count comes from the device tally, NOT from this document's sample
    // buffer: that buffer is thrown away by every full navigation, so reading
    // it here is what made this number incapable of reaching 2. The About row
    // below renders the SAME value, so the two can never disagree.
    refs.heroStat.textContent = pageActivityHeroText(
      readPageViewTally(),
      pageActivityFacts(),
    );
  } else {
    refs.heroNum.textContent = "\u2013";
    refs.heroNum.style.color = T.fg;
    refs.heroBadgeTxt.textContent = "MEASURING";
    refs.heroBadgeDot.style.background = T.mut;
    refs.heroBadge.style.background = T.br;
    refs.heroBadge.style.borderColor = T.br;
    refs.heroBadge.style.color = T.mut;
    refs.heroStat.textContent = MEASURING_HERO_TEXT;
  }

  // Meter rows from the snapshot axes (honest, omit-as-pending).
  let rows: PanelMeterRow[] = [];
  try {
    rows = buildPanelRows(capturePerfSnapshot());
  } catch {
    rows = [];
  }
  for (const row of rows) {
    const cell = refs.meterRows.get(row.key);
    if (!cell) continue;
    cell.cap.textContent = row.caption;
    if (row.rating === "pending" || row.score == null) {
      // A row with no score still says WHICH silence it is: grey only while a
      // score is genuinely coming, a distinct colour for the two answers that
      // are already final. The bar stays empty either way — there is no
      // number to draw — but the colour stops a permanent observation from
      // looking like a meter that has not finished.
      cell.cap.style.color = SILENCE_COLORS[row.rating] ?? T.mut;
      cell.fill.style.width = "0%";
    } else {
      const rc = COLORS[row.rating as Rating] ?? NEUTRAL;
      cell.cap.style.color = rc;
      cell.fill.style.width = `${barWidth(row)}%`;
      cell.fill.style.background = rc;
    }
  }

  // Live event stats.
  try {
    refs.vLong.textContent = `${getLongTaskStats().longTaskCount}`;
  } catch {
    refs.vLong.textContent = "—";
  }
  try {
    refs.vDead.textContent = `${getDeadClickStats().deadClickCount}`;
  } catch {
    refs.vDead.textContent = "—";
  }
  try {
    const routes = getRouteReachabilityStats();
    // "—" (not "0") until routes are declared — never imply a check ran.
    refs.vUnreached.textContent =
      routes.registeredCount === 0 ? "—" : `${routes.unreachableCount}`;
  } catch {
    refs.vUnreached.textContent = "—";
  }
  // How complete the self-drawing map is: pages seen, links between them, and
  // — only when routes were declared — how many of them are still unreached.
  // "—" until a page has been recorded: an empty map is "nothing seen yet",
  // never a measured zero. A map trimmed by its caps says it is a subset, and
  // a map that cannot be kept says that too rather than implying it was saved.
  try {
    const map = getPageMapStats();
    if (map.pageCount === 0) {
      refs.vPageMap.textContent = "—";
    } else {
      const parts = [
        `${map.pageCount} ${map.pageCount === 1 ? "page" : "pages"}`,
        `${map.linkCount} ${map.linkCount === 1 ? "link" : "links"}`,
      ];
      try {
        const routes = getRouteReachabilityStats();
        if (routes.registeredCount > 0) {
          // The MAP's window, not the session's: a page opened on an earlier
          // visit is already on the map, so it is not "not yet reached" here.
          // The session answer keeps its own row above.
          parts.push(`${getRoutesMissingFromPageMapCount()} unreached`);
        }
      } catch {
        // reachability unavailable — the map still says what it saw
      }
      if (map.truncated) parts.push("subset");
      parts.push(pageMapPersistenceWord(map.persistence));
      // Where it goes, and only when it goes anywhere. Left off by default so
      // the row never implies an upload a site did not ask for; said out loud
      // when the site DID ask, so "kept on this device" — a sentence about
      // storage — can never be read as a promise that it stays here.
      if (isPageMapSendEnabled()) parts.push("sent to your project");
      refs.vPageMap.textContent = parts.join(" · ");
    }
  } catch {
    refs.vPageMap.textContent = "—";
  }
  try {
    const circuit = getCircuitStats();
    if (circuit.navLoopDetected && circuit.requestBurstDetected) {
      refs.vCircuit.textContent = `loop ×${circuit.navLoopMaxBounces} · burst ${circuit.requestBurstMax1s}/s`;
    } else if (circuit.navLoopDetected) {
      refs.vCircuit.textContent = `loop ×${circuit.navLoopMaxBounces}`;
    } else if (circuit.requestBurstDetected) {
      refs.vCircuit.textContent = `burst ${circuit.requestBurstMax1s}/s`;
    } else {
      refs.vCircuit.textContent = "—";
    }
  } catch {
    refs.vCircuit.textContent = "—";
  }
  // "Press this, it opens that". The row has four answers that cannot be
  // confused: the click watch is not installed here ("not observable here"),
  // nothing has been pressed yet ("—"), or what was actually found — with
  // unnamed presses and anything the cap refused counted out loud. The
  // wording is derived once in controlMap.ts, so this row and the status
  // readout can never disagree.
  try {
    const map = getControlMap();
    refs.vControls.textContent = controlMapPanelText(map);
    // The census’s own word for its own state — a count, or the reason
    // there is none. Never a zero standing in for "we did not look".
    try {
      // The page this panel is showing on. A census joins presses only to
      // its own route, so a readout that does not say which page it is
      // asking about would report every control as never pressed.
      refs.vCensus.textContent = controlCensusPanelText(
        controlCensusReport(currentRouteLabel()),
      );
    } catch {
      refs.vCensus.textContent = "page refused";
    }
    // ...and the drawn pairs beneath it. Unnamed controls are drawn as
    // unnamed rather than dropped, and anything past the last slot or past
    // the on-device cap is counted in the final row instead of vanishing.
    const want = computeControlRowSlots(map, refs.controlRows.length);
    let anyDrawn = false;
    for (let i = 0; i < refs.controlRows.length; i++) {
      const slot = refs.controlRows[i];
      const say = want[i];
      slot.k.textContent = say.control;
      slot.v.textContent = say.detail;
      slot.row.style.display = say.shown ? "block" : "none";
      if (say.shown) anyDrawn = true;
    }
    refs.controlScope.style.display = anyDrawn ? "block" : "none";
  } catch {
    refs.vControls.textContent = "—";
    refs.vCensus.textContent = "page refused";
    try {
      for (const slot of refs.controlRows) slot.row.style.display = "none";
      refs.controlScope.style.display = "none";
    } catch {
      // a panel that cannot hide a row must still not break the page
    }
  }

  // One value, both surfaces — and the scope it is true for, in words beneath
  // it (pageViewTally.ts).
  const tally = readPageViewTally();
  refs.vViews.textContent = `${tally.measured}`;
  refs.viewsScope.textContent = pageViewScopeLine(tally);

  // "Measurements dropped" — shown only once the server has reported a dropped
  // row this page. Hidden otherwise (no row, no heading, no "0 dropped"), so a
  // healthy app and an older server look exactly as they do today.
  try {
    const drops = computePanelDrops();
    if (drops) {
      refs.dropValue.textContent = drops.text;
      refs.dropCard.style.display = "block";
    } else {
      refs.dropCard.style.display = "none";
    }
  } catch {
    // display-only — never break the rest of the paint
  }
}

/** Mount the bubble if visibility resolves to true. Idempotent; a no-op
 *  outside a browser. Any internal failure removes the bubble quietly. */
export function mountBubbleIfEnabled(option?: boolean): void {
  try {
    if (typeof document === "undefined" || typeof window === "undefined") {
      return;
    }
    if (!resolveBubbleVisibility(option)) return;
    if (host) return;
    const attach = () => {
      try {
        buildAndAttach();
      } catch {
        unmountBubble();
      }
    };
    if (document.body) attach();
    else {
      document.addEventListener("DOMContentLoaded", attach, { once: true });
    }
  } catch {
    unmountBubble();
  }
}
