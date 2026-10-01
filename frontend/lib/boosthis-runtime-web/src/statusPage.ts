/**
 * The browser kit's status readout — its third surface.
 *
 * WHY THIS EXISTS
 * ---------------
 * Every other Boosthis kit answers "what is this install actually doing?" in
 * three places: one console line, the in-app panel, and a status page. The
 * browser kit had only the first two, and the second showed neither the key it
 * was using nor where that value came from — even though it had worked both out
 * internally all along. So on the ONE surface where a missing key actually
 * lands, the answer existed and was never shown.
 *
 * A browser kit has no server of its own to serve a page from, so this is the
 * page's equivalent: a plain, code-defined block of lines the host can print to
 * the console, drop into a debug view, or read from a test. Same facts, same
 * sentences, same single source of wording as the console line and the panel —
 * so the three can never drift apart.
 *
 * WHAT IT NEVER CONTAINS
 * ----------------------
 * The project key (only its last four characters), any token, any server text,
 * or anything the developer did not already have.
 */

import {
  getActiveProjectKeyStatus,
  getActiveTelemetryClient,
  getRegistrationRefusalSentence,
  getDroppedUploadCount,
  getLastUploadFailure,
  isLastUploadAttemptFailed,
  type UploadFailReason,
} from "./telemetry";
import { currentRouteLabel } from "./vitals";
import { getRegistrationVerdict } from "./registration";
import { getRegistrationPromiseState } from "./registrationWatch";
import {
  getRegistrationRefusalKind,
  registrationKeepsTrying,
} from "./inactiveNotice";
import { readRegisteredAt } from "./registrationMemory";
import {
  pageActivityFacts,
  pageActivityStatusLine,
  type PageActivityFacts,
} from "./pageActivity";
import { RUNTIME_VERSION } from "./thresholds";
import {
  controlMapStatusLine,
  controlMapRows,
  getControlMap,
  type ControlMapReport,
} from "./controlMap";
import {
  controlCensusReport,
  controlCensusStatusLine,
  type ControlCensusReport,
} from "./controlCensus";

/** The three-outcome wording every kit owns, byte-identical. A readout may say
 *  "this readout" where a panel says "this panel"; nothing else may vary. Kept
 *  as literals HERE, not imported, because the cross-kit honesty guard greps
 *  each surface's own file for the contiguous sentence — an import would leave
 *  this surface unchecked, which is how a surface drifts. */
const UNKNOWN_TITLE = "Can't check right now";
const UNKNOWN_BODY =
  "Boosthis could not be reached to confirm whether this app is registered, " +
  "so this readout cannot say either way yet. This is not a failed install " +
  "and it does not mean anything stopped \u2014 it settles by itself once the " +
  "check goes through. Do not change the install line's id while this is " +
  "showing.";
/** The Registered row when the answer is missing. Yes / No is two answers to a
 *  three-answer question. */
const STATUS_ROW_UNKNOWN = "Can't tell right now";
/** …and a bare "no" is two answers to a two-answer question.
 *
 *  A live install read "Registered with Boosthis: no" while the server had the
 *  same install id registered three days earlier and still measuring. The
 *  qualifier ("on this page") sat on the row below, and the unqualified
 *  verdict is what the reader took away. So the "no" now carries WHOSE no it
 *  is: Boosthis's answer about the install, or this page's answer about
 *  itself. */
const STATUS_ROW_NOT_ON_FILE =
  "no \u2014 Boosthis does not have this install on file";
const STATUS_ROW_NO_KIT_HERE = "no \u2014 Boosthis never started on this page";
/** This browser's own record of an earlier success, shown only where the
 *  server has not answered. Local state may fill an unanswered gap; it may
 *  never become a verdict, so it says plainly whose record it is and where the
 *  real answer lives. */
const REGISTERED_BEFORE_LABEL = "Registered before in this browser";
const REGISTERED_BEFORE_NOTE =
  "this browser's own record, not Boosthis's answer \u2014 your Boosthis " +
  "dashboard's connection check for this project settles it";
/** What the identity line is called, everywhere. */
const INSTALL_ID_LABEL = "Install ID";
/** Never a blank identity: an empty slot reads as "no identity", which is
 *  exactly the ambiguity this line exists to remove. */
const INSTALL_ID_UNKNOWN_TEXT = "not started on this page";

/** What became of the startup line's promise, for the developer who never
 *  opens a console.
 *
 *  "Registered: no" was previously the same answer for a page where Boosthis
 *  never ran, a page still waiting for its first reply, and a page that
 *  announced a registration and then silently failed to make one. The third is
 *  a fault and the other two are not, so it gets said in its own words. */
const STARTUP_ROW_LABEL = "Announced registering";
const STARTUP_NOT_ANNOUNCED = "no \u2014 Boosthis never started on this page";
const STARTUP_WAITING = "yes \u2014 waiting for the answer";
const STARTUP_NEVER_REGISTERED =
  "yes \u2014 but it never registered on this page";
/** "Not registered yet, still trying" is a different state from "not
 *  registered, and here is what stopped it" — and it was being read as the
 *  second. The kit's deadline is ~36 seconds; its retry ladder runs for
 *  minutes, and a real install recovered on its own about three minutes in
 *  with this row already transcribed as a failure. Where the kit is still
 *  chasing the registration, the row says so and the sentence below carries
 *  the horizon. */
const STARTUP_STILL_TRYING =
  "yes \u2014 not registered on this page yet, still trying";
const STARTUP_REGISTERED = "yes \u2014 registered with Boosthis";

export interface WebStatusFacts {
  /** The install id this readout is reporting on, or null before the kit
   *  started. Without it, a developer comparing this readout with their
   *  dashboard cannot tell WHICH install it describes. */
  installId: string | null;
  /** Masked project key ("…1a2b"), or null when there is none. */
  projectKeyDisplay: string | null;
  /** Where the value came from, in plain words. */
  projectKeySource: string;
  /** Whether this page holds a key at all. */
  hasProjectKey: boolean;
  /** Boosthis's own answer about this install, never a guess from local state. */
  registration: "registered" | "unregistered" | "unknown";
  /** Did the kit run on this page at all? Separates "Boosthis says this
   *  install is not on file" from "there is no Boosthis here to ask" — both
   *  used to render as one unqualified "no". */
  kitStartedHere: boolean;
  /** Is the kit still chasing the registration by itself? A verdict taken
   *  while the retry ladder is still running is not a final one, and reading
   *  it as final is the fault this fact exists to prevent. */
  keepsTrying: boolean;
  /** When this browser last watched Boosthis accept THIS install id, or null.
   *  This browser's own record — never a verdict, and only ever shown where
   *  the server has left the question unanswered. */
  registeredBeforeAt: number | null;
  /** What became of the startup line's "Registering next." on THIS page — a
   *  local fact about our own behaviour, not a claim about the server. */
  startup: "not-announced" | "waiting" | "unkept" | "kept";
  /** The `{why}. {fix}` sentence for whatever went wrong, or null when nothing
   *  has. Identical to the console line and the panel notice. */
  problem: string | null;
  lastUploadFailAt: number | null;
  lastUploadFailReason: UploadFailReason | null;
  uploadsDropped: number;
  lastAttemptFailed: boolean;
  sharingActive: boolean;
  kitVersion: string;
  /** The on-device "press this, it opens that" map (controlMap.ts). Read from
   *  the same click watch the dead-click detector uses, worded by the same
   *  module the panel row uses, and — like everything else on this readout —
   *  never uploaded. */
  controlMap: ControlMapReport;
  /** What the page said about its own controls (controlCensus.ts): how
   *  many there are, how many have been pressed, and where a press led.
   *  Carries its own reason when there is no census, so this readout
   *  never has to invent one. */
  controlCensus: ControlCensusReport;
  /** What this visit consisted of (pageActivity.ts): address changes,
   *  presses, readings taken, and which of the four situations that is.
   *  Answers the question a near-silent install raises — an app with nothing
   *  to report and an app whose work the kit cannot see as page views are
   *  different findings and used to read identically here. */
  pageActivity: PageActivityFacts;
}

/** Read the kit's live state. Fail-safe in every direction: any readout error
 *  degrades to the most conservative honest answer rather than throwing into
 *  the host page. */
export function collectWebStatusFacts(): WebStatusFacts {
  let projectKeyDisplay: string | null = null;
  let projectKeySource = "no project key configured";
  let registration: WebStatusFacts["registration"] = "unknown";
  let startup: WebStatusFacts["startup"] = "not-announced";
  let problem: string | null = null;
  let lastUploadFailAt: number | null = null;
  let lastUploadFailReason: UploadFailReason | null = null;
  let uploadsDropped = 0;
  let lastAttemptFailed = false;
  let sharingActive = false;
  let keepsTrying = false;
  let registeredBeforeAt: number | null = null;
  let kitStartedHere = false;
  try {
    const key = getActiveProjectKeyStatus();
    projectKeyDisplay = key.display;
    projectKeySource = key.description;
  } catch {
    /* keep the conservative defaults */
  }
  try {
    registration = getRegistrationVerdict();
  } catch {
    registration = "unknown";
  }
  try {
    startup = getRegistrationPromiseState();
  } catch {
    startup = "not-announced";
  }
  try {
    keepsTrying = registrationKeepsTrying(getRegistrationRefusalKind());
  } catch {
    keepsTrying = false;
  }
  try {
    problem = getRegistrationRefusalSentence();
  } catch {
    problem = null;
  }
  try {
    const failure = getLastUploadFailure();
    lastUploadFailAt = failure?.at ?? null;
    lastUploadFailReason = failure?.reason ?? null;
    uploadsDropped = getDroppedUploadCount();
    lastAttemptFailed = isLastUploadAttemptFailed();
  } catch {
    /* keep healthy defaults */
  }
  let kitVersion = "";
  try {
    kitVersion = RUNTIME_VERSION;
  } catch {
    kitVersion = "";
  }
  let hasProjectKey = false;
  try {
    hasProjectKey = !!getActiveProjectKeyStatus().display;
  } catch {
    hasProjectKey = false;
  }
  // A page that never called enableTelemetry has no client at all — a LOCAL
  // fact, so it is a definite negative rather than a "cannot tell".
  // The control map answers for itself: it carries its own "not observable
  // here" state, so a failed read degrades to that rather than to an empty map
  // that would read as "we looked and found nothing".
  let controlMap: ControlMapReport;
  try {
    controlMap = getControlMap();
  } catch {
    controlMap = {
      watching: false,
      presses: 0,
      namedPresses: 0,
      unnamedPresses: 0,
      namedControls: 0,
      edges: [],
      edgesNotShown: 0,
      deadEnds: [],
      deadEndsNotShown: 0,
      deadEndsBelowRepeatGate: 0,
      supersededPresses: 0,
      suspendedPresses: 0,
    };
  }
  // The census answers for itself too: every reason there is no count is
  // one of its own status words, so a failed read degrades to "the page
  // refused the question" rather than to a page with no controls on it.
  let controlCensus: ControlCensusReport;
  try {
    // Named page, same as every other caller: the census joins a press to
    // the route it happened on, never to whichever page is showing now.
    controlCensus = controlCensusReport(currentRouteLabel());
  } catch {
    controlCensus = { status: "unreadable", entries: [] };
  }
  let installId: string | null = null;
  try {
    const client = getActiveTelemetryClient();
    if (!client) registration = "unregistered";
    kitStartedHere = !!client;
    sharingActive = !!client?.meterSharing;
    const id = client?.installId;
    installId = typeof id === "string" && id ? id : null;
  } catch {
    /* leave the verdict as read, and the identity unknown */
  }
  // This browser's own record of an earlier success. Read last, because it
  // needs the install id — and used only to fill a gap the server has left,
  // never to answer for it.
  try {
    registeredBeforeAt = readRegisteredAt(installId);
  } catch {
    registeredBeforeAt = null;
  }
  // Counts only, and it answers for itself: a page nothing was watching
  // reports "cannot tell" rather than an empty page.
  let pageActivity: PageActivityFacts;
  try {
    pageActivity = pageActivityFacts();
  } catch {
    pageActivity = {
      viewChanges: 0,
      viewChangesUntimed: 0,
      readings: 0,
      presses: 0,
      watching: false,
      activity: "unwatched",
    };
  }
  return {
    installId,
    pageActivity,
    projectKeyDisplay,
    projectKeySource,
    hasProjectKey,
    registration,
    kitStartedHere,
    keepsTrying,
    registeredBeforeAt,
    startup,
    problem,
    lastUploadFailAt,
    lastUploadFailReason,
    uploadsDropped,
    lastAttemptFailed,
    sharingActive,
    kitVersion,
    controlMap,
    controlCensus,
  };
}

/** The status readout as plain lines, newest concern first. Every byte is
 *  authored here. Never throws. */
export function webStatusLines(facts?: WebStatusFacts): string[] {
  try {
    const f = facts ?? collectWebStatusFacts();
    const lines = [
      `Boosthis web kit ${f.kitVersion}`,
      `${INSTALL_ID_LABEL}: ${f.installId ?? INSTALL_ID_UNKNOWN_TEXT}`,
      `Project key: ${f.projectKeyDisplay ?? "not set"}`,
      `Read from: ${f.projectKeySource}`,
      `Registered with Boosthis: ${registeredRowText(f)}`,
      `${STARTUP_ROW_LABEL}: ${startupRowText(f)}`,
    ];
    // An unanswered check is not a fault, and a bare "can't tell" reads like
    // one. Say what it means, in the same words the panel uses.
    if (f.registration === "unknown") lines.push(`${UNKNOWN_TITLE}: ${UNKNOWN_BODY}`);
    // …and where the server has not answered, say what this browser has seen
    // before. A registration is durable: an install that registered on an
    // earlier page load has NOT become a fresh failure because this page
    // could not produce a credential.
    if (f.registration !== "registered" && f.registeredBeforeAt !== null) {
      lines.push(
        `${REGISTERED_BEFORE_LABEL}: ${formatUploadFailureTime(
          f.registeredBeforeAt,
        )} \u2014 ${REGISTERED_BEFORE_NOTE}`,
      );
    }
    if (f.problem) lines.push(f.problem);
    if (
      f.registration === "registered" &&
      f.sharingActive &&
      f.lastAttemptFailed &&
      f.lastUploadFailAt !== null &&
      f.lastUploadFailReason !== null
    ) {
      lines.push(
        "Uploads are not getting through: This app is registered and sharing is on, but the last batch of measurements did not reach Boosthis, so your dashboard is missing the most recent data. The rows below say what happened and how much has been lost.",
      );
      lines.push(
        `Last upload failed: ${formatUploadFailureTime(f.lastUploadFailAt)} — ${uploadFailText(f.lastUploadFailReason)}`,
      );
      lines.push(`Uploads lost: ${f.uploadsDropped}`);
    }
    // "Press this, it opens that" — the same verdict the panel row shows, in
    // the room a one-line row does not have: how many presses came from a
    // control with no code-defined test id, and what is still below the
    // repeat gate. Always present, because "nothing pressed yet" and "this
    // browser cannot be watched" are answers a developer needs too.
    // What this visit consisted of, BEFORE the control rows: a developer who
    // came here because their dashboard shows almost no readings needs the
    // answer to "is my app quiet, or can you not see it?" first, and the
    // count of readings on its own has never distinguished the two.
    lines.push(pageActivityStatusLine(f.pageActivity));
    lines.push(controlMapStatusLine(f.controlMap));
    // ...and how many controls are THERE, pressed or not. The line above
    // can only speak about controls somebody has pressed; this one is the
    // page’s own answer about the ones nobody has touched, and it says
    // why there is no count whenever there is none.
    lines.push(controlCensusStatusLine(f.controlCensus));
    // ...and then the map itself. A count of arrows is not a map: the
    // developer came here to read WHICH control opened WHICH page, so each
    // pair is drawn, heaviest first, with unnamed controls drawn as unnamed
    // and anything past the limit or past the on-device cap said out loud.
    const drawn = controlMapRows(f.controlMap, CONTROL_MAP_STATUS_ROWS);
    for (const row of drawn.rows) {
      lines.push(
        (row.kind === "arrow" ? "Control \u2192 page: " : "Leads nowhere: ") +
          row.text,
      );
    }
    if (drawn.more > 0) {
      lines.push(
        `Controls \u2192 pages: ${drawn.more} more not drawn here \u2014 either past this list or past the on-device cap.`,
      );
    }
    return lines;
  } catch {
    return ["Boosthis web kit status could not be read."];
  }
}

/**
 * How many control → page lines the status readout draws before it starts
 * counting the rest. Wider than the 300px panel can hold, still a readout
 * rather than an inventory.
 */
const CONTROL_MAP_STATUS_ROWS = 12;

/** The startup row's answer. A page that announced a registration and never
 *  got one is a distinct outcome from one where Boosthis never ran, and from
 *  one that is still waiting — never folded into any of them. */
/** The Registered row's answer, with the provenance the reader takes away.
 *  "No" from Boosthis and "no, because Boosthis never ran here" are different
 *  facts, and the second was reported as the first on a page whose install was
 *  registered and measuring. */
function registeredRowText(f: WebStatusFacts): string {
  if (f.registration === "registered") return "yes";
  if (f.registration !== "unregistered") return STATUS_ROW_UNKNOWN;
  return f.kitStartedHere ? STATUS_ROW_NOT_ON_FILE : STATUS_ROW_NO_KIT_HERE;
}

function startupRowText(f: WebStatusFacts): string {
  if (f.startup === "kept" || f.registration === "registered") {
    return STARTUP_REGISTERED;
  }
  if (f.startup === "unkept") {
    return f.keepsTrying ? STARTUP_STILL_TRYING : STARTUP_NEVER_REGISTERED;
  }
  if (f.startup === "waiting") return STARTUP_WAITING;
  return STARTUP_NOT_ANNOUNCED;
}

function formatUploadFailureTime(at: number): string {
  return new Date(at).toISOString();
}

export function uploadFailText(reason: UploadFailReason): string {
  if (reason === "unauthorized") return "Refused — credentials rejected";
  if (reason === "rejected") return "Refused — batch rejected";
  if (reason === "server-error") return "Boosthis failed to store it";
  return "No answer — timed out or unreachable";
}

/** Print the status readout. One call, one block, never gated behind a debug
 *  flag — this exists for the developer who does not yet suspect the kit.
 *  Never throws into the host (a page that replaced `console` must not crash). */
export function printWebStatus(): void {
  try {
    console.log(webStatusLines().join("\n"));
  } catch {
    /* a host that replaced console must never crash on a status readout */
  }
}
