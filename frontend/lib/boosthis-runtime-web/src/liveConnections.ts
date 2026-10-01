/** Long-lived connection watcher — the input for the Live Connections axis.
 *
 * A chat window, a live dashboard, a presence indicator or a streamed AI answer
 * holds ONE socket open for minutes. Every other reading this kit takes is
 * driven by things that FINISH — a resource that loaded, an interaction that
 * painted — so a page sitting on an open socket looks perfectly idle whether the
 * socket is carrying traffic or has been dead since the wifi blinked. This
 * module is the only place that watches the socket itself.
 *
 * WHAT IT WATCHES
 *   `WebSocket` and `EventSource`, wrapped at the constructor so every
 *   connection the page opens is seen, whichever library opened it. For each
 *   one: when it opened, when it closed, whether the close was the app's own
 *   decision or a drop, how many messages crossed it, and the rhythm of those
 *   messages (so a silence can be judged against the connection's OWN habits).
 *
 * GUEST SAFETY (this runs inside a customer's page):
 *   - the real constructors are kept and always called through,
 *   - the returned object is the real one — no proxy, no altered behaviour,
 *   - listeners are added passively and never swallow the app's own,
 *   - nothing here throws; every callback body is guarded,
 *   - the originals are restored on uninstall, and install is idempotent.
 *   Where wrapping is impossible the axis stays absent — never a guessed zero.
 *
 * PRIVACY: counts, durations and close CODES only. The address a connection
 * points at is folded into a non-reversible 32-bit number the instant it is
 * seen — used only to tell "the same endpoint again" from "a different one" in
 * memory — and neither the address nor any message ever leaves this module.
 * Message CONTENT is never read: only that a message arrived, and when.
 */

import type { Rating } from "./thresholds";
import { linearScore } from "./thresholds";
import type { AxisRating } from "./meterAxes";

import { earnedPerHour, earnedPerMin, windowMinOf } from "./rateHonesty";
/* ── The shared judgement constants ───────────────────────────────────────
 * Identical in the phone and Node kits, so one connection judged dead on the
 * client and on the server is judged by the same rules. */

/** Silence longer than this makes an open connection "quiet" — the point at
 *  which it is worth asking whether anything is still alive in there. */
export const LIVECONN_QUIET_MS = 30_000;
/** Message gaps needed before we claim to know a connection's rhythm. Below
 *  this the connection has not told us what normal looks like, so a silence
 *  cannot be judged and the reading abstains. */
export const LIVECONN_CADENCE_MIN_GAPS = 5;
/** Silence beyond this multiple of a connection's OWN typical gap (and beyond
 *  the quiet floor) is a connection that has stopped, not one that is resting. */
export const LIVECONN_DEAD_GAP_MULTIPLE = 6;
/** A new connection to the same endpoint within this of the last one ending is
 *  a reconnect rather than a fresh, unrelated connection. */
export const LIVECONN_RECONNECT_LINK_MS = 60_000;
/** Rolling window the reconnect-storm test looks back over. */
export const LIVECONN_STORM_WINDOW_MS = 60_000;
/** Reconnects to one endpoint inside that window before it counts as a storm. */
export const LIVECONN_STORM_THRESHOLD = 5;
/** Median gap between those reconnects below which there is no real back-off.
 *  A client that widens its gaps is doing the right thing and must never be
 *  faulted for reconnecting often. */
export const LIVECONN_STORM_BACKOFF_MS = 2_000;
/** Minimum observation before the axis reports anything at all. Deliberately
 *  SHORTER than the earned-rate window (rateHonesty.ts): what this axis mostly
 *  reports — sockets open, dropped, quiet, never closed — is counted, not
 *  projected, and a dropped connection must be visible the moment it happens.
 *  Only the per-hour and per-minute fields wait for the longer window. */
export const LIVECONN_MIN_WINDOW_MS = 30_000;
/** Share of screen changes that must each bring a NEW connection before the
 *  "it rebuilds the socket on every view" shape is called. Identical in the
 *  phone kit, so one client judged wasteful on a phone is judged wasteful in a
 *  browser. */
export const LIVECONN_PER_SCREEN_RATIO = 0.8;
/** Screen changes needed before that ratio means anything at all. */
export const LIVECONN_PER_SCREEN_MIN = 5;
/** How soon after a screen change a new connection is CAUSED BY it. A view
 *  that rebuilds its socket does so as it mounts; anything later is ordinary
 *  application behaviour. Without this window the test degrades into
 *  "connections ever opened ÷ screens ever changed", which faults an app that
 *  opened four feeds at startup and then navigated five times without
 *  reconnecting once. */
export const LIVECONN_PER_SCREEN_WINDOW_MS = 5_000;
/** Unexpected disconnects per hour at or below which a connection is behaving
 *  normally — networks blink, and a client that reopens immediately is not
 *  broken. Both the score and the wording of the caption read this one number,
 *  so the words can never name a fault the rating forgives. */
export const LIVECONN_DROPS_OK_PER_HOUR = 0.5;
/** How long a connection must have been open, with no close ever observed on
 *  its endpoint, before the count of them is worth reporting as a leak. */
export const LIVECONN_LEAK_AGE_MS = 5 * 60_000;
/** Concurrent open connections above which "never closed" is the likely story
 *  rather than "this app genuinely needs that many". */
export const LIVECONN_LEAK_MIN_OPEN = 8;

/** Upper bound on tracked connections, so a page that opens thousands cannot
 *  grow this module's memory without limit. */
const MAX_TRACKED = 200;
/** Upper bound on remembered reconnect timestamps per endpoint. */
const MAX_RECONNECT_STAMPS = 32;

type Transport = "ws" | "sse";

interface ConnRecord {
  /** Non-reversible fold of the address — grouping only, never emitted. */
  endpoint: number;
  transport: Transport;
  openedAt: number;
  /** Last time anything at all was seen on this connection. */
  lastSeenAt: number;
  msgCount: number;
  /** Ascending inter-message gaps, capped — the connection's own rhythm. */
  gaps: number[];
  /** Sum + count let us take a mean without keeping every gap. */
  closedAt: number | null;
  /** True when the app itself asked for the close (or the peer closed cleanly). */
  cleanClose: boolean;
  /** The live object, so `bufferedAmount` can be read at capture. */
  socket: unknown;
}

let installed = false;
let observedAny = false;
let firstSeenAt = 0;
let origWebSocket: unknown = null;
let origEventSource: unknown = null;

const records: ConnRecord[] = [];
/** endpoint → timestamps of connections opened to it, for the storm test. */
const reopensByEndpoint = new Map<number, number[]>();
/** endpoint → when its most recent connection ended (linking reconnects). */
const lastEndAt = new Map<number, number>();

let totalOpened = 0;
let totalClosed = 0;
let totalDrops = 0;
let totalReconnects = 0;
let totalMessages = 0;
let peakOpen = 0;
let stormPeak = 0;
/** The two halves of the "it rebuilds the socket on every view" shape: screen
 *  changes seen, and how many of them were FOLLOWED BY a new connection. A
 *  ratio, not a count, because one reconnect that happens to land after a
 *  navigation proves nothing. */
let screenChanges = 0;
let screensFollowedByOpen = 0;
/** The screen change still waiting to be answered by a connection. Null before
 *  the first navigation — so nothing an app opens at startup is ever blamed on
 *  a screen change — and null again once one connection has answered it, so a
 *  view that opens five sockets counts once, not five times. */
let pendingScreenAt: number | null = null;

let clock: () => number = () => Date.now();

/** Non-reversible 32-bit fold (FNV-1a). Used ONLY to tell one endpoint from
 *  another inside this process; the string it came from is dropped at once. */
function foldAddress(input: unknown): number {
  let h = 0x811c9dc5;
  try {
    const s = String(input ?? "");
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
  } catch {
    return 0;
  }
  return h >>> 0;
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[mid - 1] + sorted[mid]) / 2
    : sorted[mid];
}

function noteReopen(endpoint: number, now: number): void {
  let stamps = reopensByEndpoint.get(endpoint);
  if (!stamps) {
    if (reopensByEndpoint.size >= MAX_TRACKED) return;
    stamps = [];
    reopensByEndpoint.set(endpoint, stamps);
  }
  stamps.push(now);
  const cutoff = now - LIVECONN_STORM_WINDOW_MS;
  while (stamps.length > 0 && stamps[0] < cutoff) stamps.shift();
  if (stamps.length > MAX_RECONNECT_STAMPS) stamps.shift();
  if (
    stamps.length >= LIVECONN_STORM_THRESHOLD &&
    medianGap(stamps) < LIVECONN_STORM_BACKOFF_MS &&
    stamps.length > stormPeak
  ) {
    stormPeak = stamps.length;
  }
}

function medianGap(ascending: number[]): number {
  if (ascending.length < 2) return 0;
  const gaps: number[] = [];
  for (let i = 1; i < ascending.length; i++) {
    gaps.push(ascending[i] - ascending[i - 1]);
  }
  return median(gaps);
}

/** Register one newly-created connection. Exported for the phone-shaped case
 *  where a host reports its own transport (see `noteHostConnection`). */
function track(endpoint: number, transport: Transport, socket: unknown): ConnRecord | null {
  const now = clock();
  if (!observedAny) {
    observedAny = true;
    firstSeenAt = now;
  }
  totalOpened++;
  if (
    pendingScreenAt !== null &&
    now - pendingScreenAt <= LIVECONN_PER_SCREEN_WINDOW_MS
  ) {
    screensFollowedByOpen++;
    pendingScreenAt = null;
  }
  const previousEnd = lastEndAt.get(endpoint);
  if (
    previousEnd !== undefined &&
    now - previousEnd <= LIVECONN_RECONNECT_LINK_MS
  ) {
    totalReconnects++;
    noteReopen(endpoint, now);
  }
  if (records.length >= MAX_TRACKED) {
    // Drop the oldest CLOSED record first; a page with more than this many
    // simultaneously-open sockets has bigger problems than our bookkeeping.
    const idx = records.findIndex((r) => r.closedAt !== null);
    if (idx >= 0) records.splice(idx, 1);
    else return null;
  }
  const rec: ConnRecord = {
    endpoint,
    transport,
    openedAt: now,
    lastSeenAt: now,
    msgCount: 0,
    gaps: [],
    closedAt: null,
    cleanClose: false,
    socket,
  };
  records.push(rec);
  const open = records.filter((r) => r.closedAt === null).length;
  if (open > peakOpen) peakOpen = open;
  return rec;
}

function noteMessage(rec: ConnRecord): void {
  try {
    const now = clock();
    if (rec.msgCount > 0) {
      const gap = now - rec.lastSeenAt;
      if (gap >= 0) {
        rec.gaps.push(gap);
        if (rec.gaps.length > 64) rec.gaps.shift();
      }
    }
    rec.msgCount++;
    rec.lastSeenAt = now;
    totalMessages++;
  } catch {
    /* bookkeeping must never disturb the page */
  }
}

function noteClose(rec: ConnRecord, clean: boolean): void {
  try {
    if (rec.closedAt !== null) return;
    const now = clock();
    rec.closedAt = now;
    rec.cleanClose = clean;
    rec.lastSeenAt = now;
    totalClosed++;
    if (!clean) totalDrops++;
    lastEndAt.set(rec.endpoint, now);
    // The live object is released here so a closed connection cannot keep the
    // page's socket alive through our record.
    rec.socket = null;
  } catch {
    /* never disturb the page */
  }
}

/** A close is the app's own decision when the handshake ended normally.
 *  1000 = normal, 1001 = the page is going away, and `wasClean` covers the
 *  library-driven case where a code is not surfaced. Everything else — 1006
 *  (abnormal), 1011, 1012, a transport error — is a drop the app did not ask
 *  for, which is exactly what a customer means by "it keeps disconnecting". */
function isCleanClose(code: unknown, wasClean: unknown): boolean {
  const c = typeof code === "number" ? code : null;
  if (c === 1000 || c === 1001) return true;
  if (c !== null) return false;
  return wasClean === true;
}

/** Install the connection wrappers once. Guest-safe + idempotent. Returns true
 *  when at least one transport could be wrapped; false when neither could,
 *  in which case the axis stays absent rather than reporting a false calm. */
export function installLiveConnections(): boolean {
  if (installed) return true;
  let any = false;
  try {
    if (typeof window === "undefined") return false;
    const w = window as unknown as Record<string, unknown>;

    const RealWS = w.WebSocket;
    if (typeof RealWS === "function") {
      origWebSocket = RealWS;
      const Ctor = RealWS as unknown as new (
        ...a: unknown[]
      ) => Record<string, unknown>;
      const Wrapped = function (this: unknown, ...args: unknown[]) {
        const sock = new Ctor(...args);
        try {
          const rec = track(foldAddress(args[0]), "ws", sock);
          if (rec && typeof sock.addEventListener === "function") {
            const add = sock.addEventListener as (
              t: string,
              f: (e: unknown) => void,
            ) => void;
            add.call(sock, "message", () => noteMessage(rec));
            add.call(sock, "close", (e: unknown) => {
              const ev = (e ?? {}) as Record<string, unknown>;
              noteClose(rec, isCleanClose(ev.code, ev.wasClean));
            });
            add.call(sock, "error", () => {
              // An error alone is not a close — the close event follows and
              // decides. Recorded only as activity so an erroring socket is
              // not also mistaken for a silent one.
              rec.lastSeenAt = clock();
            });
          }
        } catch {
          /* the page gets its real socket either way */
        }
        return sock;
      } as unknown as typeof RealWS;
      copyStatics(RealWS, Wrapped);
      w.WebSocket = Wrapped;
      any = true;
    }

    const RealES = w.EventSource;
    if (typeof RealES === "function") {
      origEventSource = RealES;
      const Ctor = RealES as unknown as new (
        ...a: unknown[]
      ) => Record<string, unknown>;
      const Wrapped = function (this: unknown, ...args: unknown[]) {
        const es = new Ctor(...args);
        try {
          const endpoint = foldAddress(args[0]);
          // A server-sent stream RECONNECTS BY ITSELF. The browser keeps the
          // same EventSource object: on a lost connection it fires `error`
          // with the state back at CONNECTING, waits the retry interval, and
          // fires `open` again — forever, at a FIXED interval, with no
          // back-off of its own. So one object is a whole SERIES of
          // connections, and a watcher that counts constructor calls sees a
          // stream that opened once and never dropped, which is exactly the
          // green-while-broken reading this axis exists to prevent.
          //
          // Each life is therefore its own record: ended on the error, started
          // again on the next `open`. That makes an auto-retrying stream count
          // toward drops, reconnects and the storm test like any other client.
          let live = track(endpoint, "sse", es);
          if (typeof es.addEventListener === "function") {
            const add = es.addEventListener as (
              t: string,
              f: (e: unknown) => void,
            ) => void;
            // The first `open` belongs to the record made above; every later
            // one is the browser having reconnected on its own.
            add.call(es, "open", () => {
              try {
                if (!live || live.closedAt !== null) {
                  live = track(endpoint, "sse", es);
                }
              } catch {
                /* ignore */
              }
            });
            add.call(es, "message", () => {
              if (live) noteMessage(live);
            });
            // `error` is the ONLY signal a stream gives when it ends, and it
            // means one of two things: CLOSED (2) — the browser has given up —
            // or CONNECTING (0) — it will retry by itself. Neither was asked
            // for by the app, so both end this life as a drop; they differ
            // only in whether an `open` follows.
            add.call(es, "error", () => {
              try {
                if (live && live.closedAt === null) noteClose(live, false);
              } catch {
                /* ignore */
              }
            });
          }
          if (typeof es.close === "function") {
            const realClose = es.close as () => void;
            (es as Record<string, unknown>).close = function (this: unknown) {
              try {
                if (live && live.closedAt === null) noteClose(live, true);
              } catch {
                /* ignore */
              }
              return realClose.call(this);
            };
          }
        } catch {
          /* the page gets its real stream either way */
        }
        return es;
      } as unknown as typeof RealES;
      copyStatics(RealES, Wrapped);
      w.EventSource = Wrapped;
      any = true;
    }
  } catch {
    return false;
  }
  installed = any;
  return any;
}

/** Carry the constructor's own constants (OPEN/CLOSED/…) and prototype across,
 *  so `instanceof` and `WebSocket.OPEN` keep working in the host page. */
function copyStatics(from: unknown, to: unknown): void {
  try {
    const src = from as Record<string, unknown>;
    const dst = to as Record<string, unknown>;
    dst.prototype = src.prototype;
    for (const key of Object.getOwnPropertyNames(src)) {
      if (key === "prototype" || key === "length" || key === "name") continue;
      try {
        dst[key] = src[key];
      } catch {
        /* read-only static — skip */
      }
    }
  } catch {
    /* best-effort */
  }
}

/** Restore the page's own constructors and drop all state. */
export function uninstallLiveConnections(): void {
  try {
    if (typeof window !== "undefined") {
      const w = window as unknown as Record<string, unknown>;
      if (origWebSocket) w.WebSocket = origWebSocket;
      if (origEventSource) w.EventSource = origEventSource;
    }
  } catch {
    /* ignore */
  }
  origWebSocket = null;
  origEventSource = null;
  installed = false;
  resetLiveConnections();
}

function resetLiveConnections(): void {
  records.length = 0;
  reopensByEndpoint.clear();
  lastEndAt.clear();
  observedAny = false;
  firstSeenAt = 0;
  totalOpened = 0;
  totalClosed = 0;
  totalDrops = 0;
  totalReconnects = 0;
  totalMessages = 0;
  peakOpen = 0;
  stormPeak = 0;
  screenChanges = 0;
  screensFollowedByOpen = 0;
  pendingScreenAt = null;
}

/** Tell the watcher the page just moved to a different view. Called from the
 *  kit's existing navigation hook — no new listener. Lets the reading separate
 *  "reconnecting because the network dropped" from "reconnecting because the
 *  user changed screen", which have completely different fixes. */
export function noteLiveConnectionNavigation(): void {
  try {
    screenChanges++;
    // Now waiting to see whether this view brings its own connection. Only a
    // connection opened inside the window that follows counts as caused by it.
    pendingScreenAt = clock();
  } catch {
    /* ignore */
  }
}

/** How the quiet/dead question was answered for the open connections. */
export interface QuietVerdict {
  /** Open connections whose silence has passed the quiet floor. */
  quiet: number;
  /** Of those, ones the connection's OWN rhythm says have stopped. */
  stalled: number;
  /** Of those, ones we refuse to judge: the connection never established a
   *  rhythm, so silence proves nothing either way. */
  undecided: number;
}

function judgeQuiet(now: number): QuietVerdict {
  let quiet = 0;
  let stalled = 0;
  let undecided = 0;
  for (const rec of records) {
    if (rec.closedAt !== null) continue;
    const silence = now - rec.lastSeenAt;
    if (silence < LIVECONN_QUIET_MS) continue;
    quiet++;
    if (rec.gaps.length < LIVECONN_CADENCE_MIN_GAPS) {
      // Nothing to judge against. An app with no heartbeat gives us exactly
      // this, and saying "dead" here would be a guess dressed as a reading.
      undecided++;
      continue;
    }
    const typical = median(rec.gaps);
    if (typical <= 0) {
      undecided++;
      continue;
    }
    if (silence > typical * LIVECONN_DEAD_GAP_MULTIPLE) stalled++;
  }
  return { quiet, stalled, undecided };
}

/** Score a connection reading against its band. Delegates to the kit's one
 *  checked scorer — this was a private copy of the interpolation with no band
 *  check in it, so a reversed pair reached a published verdict unrefused. */
function linear(value: number, good: number, poor: number): number {
  return linearScore(value, good, poor);
}

function ratingFor(score: number): Rating {
  return score >= 85 ? "good" : score >= 60 ? "needs-work" : "poor";
}

/** The wire shape. Numbers, one rating and one caption — nothing else.
 *  A result only exists when a real connection was watched — absence still
 *  means "no realtime here". The score, though, is null while a term of it
 *  depends on a rate the window has not earned (rateHonesty.ts); the counts
 *  beside it are real either way. */
export interface LiveConnectionsResult {
  score: number | null;
  rating: AxisRating;
  caption: string;
  open: number;
  peakOpen: number;
  opened: number;
  closed: number;
  drops: number;
  reconnects: number;
  reconnectsPerHour: number | null;
  medianLifeMs: number;
  longestMs: number;
  stormCount: number;
  quiet: number;
  stalled: number;
  undecided: number;
  neverClosed: number;
  perScreen: number;
  msgsPerMin: number | null;
  flowMeasurable: number;
  backlog: number | null;
  windowMin: number;
  measurable: number;
}

/**
 * The current reading, or null when there is nothing honest to say.
 *
 * Null in exactly two cases, and they mean the same thing to a reader: this
 * page has no long-lived connections worth a tile. Either none was ever opened
 * (an app with no realtime shows NOTHING here, never a row of zeros), or the
 * first one is younger than the minimum window.
 */
export function readLiveConnections(): LiveConnectionsResult | null {
  try {
    if (!installed || !observedAny) return null;
    const now = clock();
    const elapsed = Math.max(0, now - firstSeenAt);
    if (elapsed < LIVECONN_MIN_WINDOW_MS) return null;
    // The window as it really is. Rounding thirty seconds up to "1m observed"
    // would put a minute we never watched into the caption and the payload.
    const windowMin = windowMinOf(elapsed);
    const hours = elapsed / 3_600_000;

    const openRecs = records.filter((r) => r.closedAt === null);
    const closedRecs = records.filter((r) => r.closedAt !== null);
    const lives = closedRecs.map((r) => (r.closedAt as number) - r.openedAt);
    const medianLifeMs = Math.round(median(lives));
    let longestMs = 0;
    for (const r of records) {
      const end = r.closedAt ?? now;
      const life = end - r.openedAt;
      if (life > longestMs) longestMs = life;
    }

    const verdict = judgeQuiet(now);
    const reconnectsPerHour = earnedPerHour(totalReconnects, elapsed);
    const stormCount = stormPeak >= LIVECONN_STORM_THRESHOLD ? stormPeak : 0;

    // "Never closed": long-lived sockets piling up with nothing ever closing.
    // Both halves are required — a chat app legitimately holds one socket open
    // all day, so age alone proves nothing.
    const aged = openRecs.filter(
      (r) => now - r.openedAt >= LIVECONN_LEAK_AGE_MS,
    ).length;
    const neverClosed =
      openRecs.length >= LIVECONN_LEAK_MIN_OPEN && aged >= LIVECONN_LEAK_MIN_OPEN
        ? aged
        : 0;

    // Message flow. Both browser transports report every message, so the rate
    // is always measurable here; the field exists because the phone and server
    // kits sometimes cannot see it.
    const msgsPerMin = earnedPerMin(totalMessages, elapsed);

    // Backlog: bytes the page has queued but not yet put on the wire, summed
    // across open sockets. Server-sent streams have no send side, so they
    // contribute nothing rather than a zero that pretends to be a reading.
    let backlog: number | null = null;
    for (const r of openRecs) {
      if (r.transport !== "ws") continue;
      try {
        const amount = (r.socket as Record<string, unknown> | null)?.bufferedAmount;
        if (typeof amount === "number" && Number.isFinite(amount)) {
          backlog = (backlog ?? 0) + amount;
        }
      } catch {
        /* a socket that refuses to answer contributes nothing */
      }
    }

    // A connection opened for nearly every screen change is the "it reconnects
    // when you move" shape — a verdict, not a count, because the ratio itself
    // is noisy and the fix is the same at any value above the line. Both halves
    // are attributed: the numerator counts screen changes that were ANSWERED by
    // a new connection, never connections in general, so startup feeds and
    // connections opened in the middle of a screen's life are not blamed on
    // navigation. Identical to the phone kit's test, so the same client is
    // judged the same way in a browser and in an app.
    const perScreen =
      screenChanges >= LIVECONN_PER_SCREEN_MIN &&
      screensFollowedByOpen / screenChanges >= LIVECONN_PER_SCREEN_RATIO
        ? 1
        : 0;

    // A quarter of this score is a per-hour drop rate, so until the window
    // has earned that projection there is no honest score to publish: a
    // substituted zero would rate a short look with real drops as healthy.
    // The counts, the window and every directly observed fault below are
    // reported either way — only the judgement waits.
    const dropsPerHour = earnedPerHour(totalDrops, elapsed);
    const dropScore =
      dropsPerHour === null ? null : linear(dropsPerHour, LIVECONN_DROPS_OK_PER_HOUR, 12);
    // Drops are the one fault with an honest tolerance, so the caption and the
    // rating have to agree about where that tolerance ends: below the line the
    // words say the disconnects were within the normal rate, above it they name
    // them and the rating stops being "good".
    const dropsNamed =
      dropsPerHour !== null && totalDrops > 0 && dropsPerHour > LIVECONN_DROPS_OK_PER_HOUR;
    const dropsUnjudged = dropsPerHour === null && totalDrops > 0;
    const stallScore = verdict.stalled === 0 ? 100 : verdict.stalled === 1 ? 40 : 0;
    const stormScore = stormCount === 0 ? 100 : 0;
    const wasteScore = neverClosed === 0 && perScreen === 0 ? 100 : 0;
    const score =
      dropScore === null
        ? null
        : Math.round(
            0.35 * dropScore + 0.3 * stallScore + 0.2 * stormScore + 0.15 * wasteScore,
          );
    // A reading that NAMES a problem may not also rate "good". The score is a
    // weighted average, so one real fault beside three clean terms still lands
    // in the good band — and every surface that reads the rating rather than
    // the caption (the account overview's attention rows, an AI asked whether
    // the chat is healthy) would report a clean bill of health directly beside
    // a sentence describing the fault. That is the exact green-while-broken
    // reading this axis exists to prevent.
    const namesAProblem =
      stormCount > 0 ||
      verdict.stalled > 0 ||
      perScreen === 1 ||
      neverClosed > 0 ||
      dropsNamed;
    const banded = score === null ? "pending" : ratingFor(score);
    const rating: AxisRating =
      namesAProblem && banded === "good" ? "needs-work" : banded;

    return {
      score,
      rating,
      caption: buildCaption({
        open: openRecs.length,
        drops: totalDrops,
        dropsNamed,
        dropsUnjudged,
        stalled: verdict.stalled,
        undecided: verdict.undecided,
        stormCount,
        neverClosed,
        perScreen,
      }),
      open: openRecs.length,
      peakOpen,
      opened: totalOpened,
      closed: totalClosed,
      drops: totalDrops,
      reconnects: totalReconnects,
      reconnectsPerHour,
      medianLifeMs,
      longestMs: Math.round(longestMs),
      stormCount,
      quiet: verdict.quiet,
      stalled: verdict.stalled,
      undecided: verdict.undecided,
      neverClosed,
      perScreen,
      msgsPerMin,
      flowMeasurable: 1,
      backlog,
      windowMin,
      measurable: 1,
    };
  } catch {
    return null;
  }
}

function buildCaption(s: {
  open: number;
  drops: number;
  dropsNamed: boolean;
  dropsUnjudged: boolean;
  stalled: number;
  undecided: number;
  stormCount: number;
  neverClosed: number;
  perScreen: number;
}): string {
  if (s.stormCount > 0) {
    return `${s.stormCount} reconnects in a minute with no widening gap — add back-off`;
  }
  if (s.stalled > 0) {
    return `${s.stalled} open but silent past its own rhythm — likely dead`;
  }
  if (s.perScreen === 1) {
    return "a new connection opens on almost every screen change";
  }
  if (s.neverClosed > 0) {
    return `${s.neverClosed} connections open for minutes and never closed`;
  }
  if (s.undecided > 0) {
    return `${s.undecided} quiet, and nothing sent regularly enough to tell dead from resting`;
  }
  if (s.dropsNamed) {
    return `${s.open} open · ${s.drops} unexpected disconnects`;
  }
  if (s.dropsUnjudged) {
    // The disconnects are named; the verdict on them is not, because the
    // window has not earned the rate that verdict would rest on.
    return `${s.open} open · ${s.drops} disconnects — too early to say whether that is a normal rate`;
  }
  if (s.drops > 0) {
    // Said plainly, and NOT as a fault: this is the rate the score gives full
    // marks to, and a caption that called it a fault would leave the words and
    // the rating disagreeing.
    return `${s.open} open · ${s.drops} brief disconnects, within the normal rate`;
  }
  return `${s.open} open · no unexpected disconnects`;
}

/* ── Test seams ───────────────────────────────────────────────────────────*/

export function _setLiveConnectionsClockForTests(fn: (() => number) | null): void {
  clock = fn ?? (() => Date.now());
}

export function _resetLiveConnectionsForTests(): void {
  resetLiveConnections();
  clock = () => Date.now();
}

/** Feed one synthetic connection for tests and for the live-proof rig, using
 *  the SAME bookkeeping the wrapped constructors use. */
export const _liveConnectionsInternals = {
  track,
  noteMessage,
  noteClose,
  judgeQuiet,
  markInstalled(on: boolean): void {
    installed = on;
  },
};
