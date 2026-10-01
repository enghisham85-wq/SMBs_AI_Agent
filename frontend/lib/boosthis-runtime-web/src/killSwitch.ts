/* ─── Boosthis: remote kill-switch / entitlement client (Web) ───────────
 *
 * Faithful port of `lib/boosthis-runtime-node/src/killSwitch.ts` (the canonical
 * completed port of the RN Rung-1 design). The doctrine applies verbatim: the
 * VALUE lives on the server and the kit only ever asks "am I still entitled?".
 * When the server says anything other than "active" the kit goes fully INERT:
 * no bubble panel meters, no measuring, no telemetry of any kind, no upload.
 *
 * Web differences from the Node port (kept idiomatic):
 *   - persistence is the web `storage` module (SYNCHRONOUS localStorage with an
 *     in-memory fallback) — exactly how the web kit already persists its
 *     self-scoped read token. Because the store is synchronous, `hydrate`
 *     reads inline and the gate is correct on the very next tick.
 *   - the check-in fetch uses the global `fetch` bounded by a `Promise.race`
 *     timer — NO AbortController (parity with the RN/Node contract).
 *   - the monotonic clock is `performance.now()` where available.
 *   - the UI is the web bubble: `getEntitlementGateKind()` drives the bubble's
 *     blocking lock overlay, and `subscribeEntitlement()` lets an open panel
 *     reflect a live flip.
 *
 * Offline policy = GRACE (7 days). Cache the last confirmed-"active" answer and
 * keep working offline for `graceSeconds`; die the moment we (a) learn a
 * non-active status (sticky immediately, across reloads) or (b) the grace
 * window lapses with no successful re-check.
 *
 * ACTIVATION LOCK (ships locked): a copy that has NEVER completed a successful
 * server handshake is fully inert. "Activated" = the PRESENCE of a persisted
 * entitlement cache (even a revoked one — a registered-but-killed install must
 * keep heartbeating so it can learn it was restored). `forget()`/`clear...`
 * wipes the cache, re-locking the kit.
 *
 * CRITICAL invariants (identical to RN/Node):
 *  - `isRuntimeInert()` is SYNCHRONOUS (transmit hot path + bubble render call
 *    it). It reads an in-memory flag hydrated synchronously from cache.
 *  - The check-in itself MUST bypass the inert gate (it is the recovery path).
 *  - The check-in fetch bounds itself with a Promise.race timer, NEVER an
 *    AbortController signal.
 *  - This module imports ONLY runtimeFlags / no-pii / storage — never
 *    telemetry / transmit (those import IT), so there is no import cycle.
 */

import { isBoosthisDisabled } from "./runtimeFlags";
import { unwatchedFetch } from "./callWatch";
import { checkNoPII } from "./no-pii";
import { storage } from "./storage";

/** The wire entitlement states. Mirrors the server's `EntitlementCheckResponse`
 *  status enum. "paused" is the calm variant of the owner's dashboard
 *  Disconnect pause; the kit goes inert on it exactly like "revoked". */
export type EntitlementStatus =
  | "active"
  | "revoked"
  | "unpaid"
  | "tampered"
  | "paused";

/** Storage key for the cached last-good answer. Versioned so the shape can
 *  evolve without misreading an old record. Byte-equal to the RN/Node key. */
const CACHE_KEY = "boosthis.entitlement.v1";

/** Default offline grace if the server answer omits one. 7 days — matches the
 *  server's `GRACE_SECONDS`. Only a fallback; the server is canonical. */
const DEFAULT_GRACE_SECONDS = 7 * 24 * 60 * 60;

/** Hard ceiling on a cached grace window (30 days) — clamps a corrupt/tampered
 *  grace so it can never indefinitely defeat the kill-switch. */
const MAX_GRACE_SECONDS = 30 * 24 * 60 * 60;

/** Clock-jitter tolerance for a cache whose `checkedAt` sits in the future. */
const CLOCK_SKEW_TOLERANCE_MS = 5 * 60 * 1000;

/** Periodic heartbeat interval once started. 6h — unchanged from RN/Node. */
const DEFAULT_INTERVAL_MS = 6 * 60 * 60 * 1000;

/** Network timeout for a single check-in. */
const DEFAULT_TIMEOUT_MS = 8000;

/** Minimum spacing between FORCED on-open entitlement checks. The dev bubble /
 *  panel opening is a user-driven enforcement edge (VAULT contract), but a user
 *  who opens/closes repeatedly must not hammer the server, so a forced on-open
 *  check runs at most once per this window. The periodic 6h heartbeat and the
 *  unconditional launch check are unaffected. */
const FORCE_CHECK_THROTTLE_MS = 60 * 1000;

interface EntitlementCache {
  status: EntitlementStatus;
  /** ms-epoch when the server confirmed this answer. */
  checkedAt: number;
  graceSeconds: number;
  /** High-water mark: the maximum wall-clock time ever OBSERVED for this active
   *  answer, so a clock rollback across a reload can't shrink proven elapsed
   *  time. Absent on legacy caches (treated as `checkedAt`). */
  maxSeenWallMs?: number;
}

/* ─── In-memory state (the synchronous source of truth for the gate) ──── */

let memStatus: EntitlementStatus = "active";
let memInert = false;
let memMessage: string | null = null;
let hydrated = false;

/** ACTIVATION LOCK tri-state: null = pre-hydration (LOCKED, fail-closed),
 *  false = no valid cache (LOCKED), true = handshake proven (normal model). */
let memActivated: boolean | null = null;

const LOCKED_MESSAGE =
  "Boosthis is locked. Connect this app to Boosthis with your project key to activate it.";

/** Active-answer grace timing (see RN/Node notes). */
let memCheckedAt: number | null = null;
let memGraceSeconds: number = DEFAULT_GRACE_SECONDS;
let memInitialAgeMs = 0;
let memMonoAt: number | null = null;
let memMaxSeenWall: number | null = null;

/** Monotonic timestamp (ms) of the last forced on-open check dispatched, or
 *  null if none this run. In-memory only. */
let lastForcedCheckAt: number | null = null;

/**
 * Which install + credential the last check-in this run actually went out
 * with, or null if none has. In-memory only.
 *
 * Launching a page used to ask the server the same permission question twice:
 * once when the heartbeat started, and again the moment registration came
 * back. Whenever the browser already held a credential both knocks carried the
 * SAME install and the SAME token, so the second one could only ever repeat
 * the first answer — a whole extra round trip added to every launch, on the
 * slowest part of connecting.
 *
 * Recording the identity lets the post-registration site ask only when the
 * question has genuinely changed. It is the pair, not a boolean: a first-ever
 * visitor has no credential at launch, so the launch check cannot run and the
 * post-registration one MUST; and an identity rotation mid-session mints a new
 * pair, which must be asked about even though a check already ran.
 */
let lastCheckedIdentity: string | null = null;

/** The install+credential pair a check would go out with now, or null when
 *  there is no credential yet (nothing to ask with). */
function currentCheckIdentity(cfg: EntitlementCheckinConfig): string | null {
  let token: string | null = null;
  try {
    token = cfg.getToken();
  } catch {
    token = null; // a throwing getter is treated as "no credential"
  }
  return token ? `${cfg.installId}\u0000${token}` : null;
}

/* ─── Check-in configuration (installed by enableTelemetry) ───────────── */

export interface EntitlementCheckinConfig {
  /** API origin + base path, e.g. "https://www.boosthis.com/api". */
  endpoint: string;
  installId: string;
  /** Lazy getter — the token (read OR delete) is issued by consent AFTER
   *  enableTelemetry runs, so read the latest value on every check-in. */
  getToken: () => string | null;
  /** The kit RUNTIME_VERSION, reported for admin visibility. */
  kitVersion: string;
  /** Tamper-evidence signal (Rung 2), forwarded from the host config. */
  integrity?: { status?: string; manifestHash?: string | null } | null;
  fetchImpl?: typeof fetch;
  intervalMs?: number;
  timeoutMs?: number;
}

let config: EntitlementCheckinConfig | null = null;
let timer: ReturnType<typeof setInterval> | null = null;

/* ─── Change subscription (lets the panel re-read when the gate flips) ── */

const listeners = new Set<() => void>();

function notify(): void {
  for (const l of listeners) {
    try {
      l();
    } catch {
      /* a listener throwing must never break the kit */
    }
  }
}

/**
 * Subscribe to inert-state changes. Returns an unsubscribe function. On first
 * subscription this also runs a lazy hydrate so a host that renders the bubble
 * before `enableTelemetry` still respects a cached kill from a previous load.
 */
export function subscribeEntitlement(listener: () => void): () => void {
  listeners.add(listener);
  if (!hydrated) hydrateEntitlement();
  return () => {
    listeners.delete(listener);
  };
}

/* ─── The synchronous gate ────────────────────────────────────────────── */

/**
 * The single combined gate every hot path consults. True ⇒ the kit must do
 * NOTHING. The global env kill-switch always wins; the ACTIVATION LOCK comes
 * next; the server-controlled entitlement state is the lever on top of both.
 */
export function isRuntimeInert(): boolean {
  return (
    isBoosthisDisabled() ||
    memActivated !== true ||
    memInert ||
    activeGraceExpired()
  );
}

/** How the kit UI should present the current gate. Distinct from the boolean
 *  `isRuntimeInert()` measuring gate: measuring/uploads are ALWAYS off when
 *  inert, but the owner's UX requirement is that a REVOKED or UNPAID project is
 *  shown as VISIBLY LOCKED (a blocking overlay in the dev bubble panel), not
 *  silently gone.
 *
 *  - "none"    — not inert (or the env kill-switch, which stays a SILENT total
 *                off-switch): render normally / render nothing.
 *  - "hidden"  — inert, but the kit must vanish rather than show a lock overlay
 *                (env kill-switch, tampered, grace-expired offline). NOT the
 *                never-checked-in case.
 *  - "unregistered" — running, but no check-in has ever succeeded. The bubble
 *                DRAWS and the panel renders its own "Not registered yet"
 *                notice: no lock overlay, nothing measured or uploaded (that
 *                gate is `inert`). See docs/kit-bubble-draw-contract.md.
 *  - "revoked" — the account owner revoked this project's access. Blocking
 *                overlay; the bubble stays visible and opens straight to it.
 *  - "unpaid"  — the account's subscription is unpaid/frozen. Same visible-lock
 *                treatment with payment-oriented copy.
 *  - "paused"  — the owner paused (reversible) — calm blocking notice. */
export type EntitlementGateKind =
  | "none"
  | "unregistered"
  | "hidden"
  | "revoked"
  | "unpaid"
  | "paused";

/**
 * Classify how the kit UI should present the gate (see `EntitlementGateKind`).
 * SYNCHRONOUS. The env kill-switch always resolves to "hidden" (it must stay a
 * silent, total off-switch — never a visible overlay).
 */
export function getEntitlementGateKind(): EntitlementGateKind {
  if (isBoosthisDisabled()) return "hidden";
  if (!isRuntimeInert()) return "none";
  // Never handshaken (ACTIVATION LOCK). NOT a silent state: the bubble draws
  // and the panel renders its own "Not registered yet" notice, which is the
  // only readable explanation a developer gets when the check-in cannot
  // complete at all. Measuring/uploading stay gated on `inert`, untouched.
  // See docs/kit-bubble-draw-contract.md.
  if (memActivated !== true) return "unregistered";
  switch (memStatus) {
    case "revoked":
      return "revoked";
    case "unpaid":
      return "unpaid";
    case "paused":
      return "paused";
    // "active" here means the offline grace window lapsed: no server verdict to
    // explain → vanish. "tampered" also hides (no user-facing overlay copy).
    default:
      return "hidden";
  }
}

/**
 * @internal Killed-only variant of the gate: TRUE when the env kill-switch, a
 * non-active server answer, or a lapsed grace window silences the kit — but NOT
 * when the kit is merely LOCKED (never activated). Used only by the
 * consent/registration transmit path so a fresh install can still register.
 */
export function _isRuntimeKilledInternal(): boolean {
  if (isBoosthisDisabled()) return true;
  // A real server answer — revoked / unpaid / paused / tampered. Sticky, and
  // registration is not a way around it.
  if (memStatus !== "active") return memInert;
  // Inert under an ACTIVE answer can only be a grace window that ran out. That
  // term is the one that can trap an install for good, because registration is
  // the only way back and this gate stands in front of it: an install holding
  // NO credential cannot check in either (the check-in needs the token it has
  // not got), so refusing its registration leaves nothing that could ever
  // clear the state. A lapsed grace therefore silences the install that EARNED
  // it, and no other — which is what the comment above has always claimed.
  if (!(memInert || activeGraceExpired())) return false;
  return hasInstallCredential();
}

/** True once this copy has proven a completed server handshake. */
export function isActivated(): boolean {
  return memActivated === true;
}

function clampGrace(graceSeconds: number): number {
  let g = graceSeconds;
  if (!(g > 0)) g = DEFAULT_GRACE_SECONDS;
  if (g > MAX_GRACE_SECONDS) g = MAX_GRACE_SECONDS;
  return g;
}

/** Read a monotonic clock, never throwing — the synchronous gate stays crash-
 *  proof. */
function monoNow(): number {
  try {
    if (
      typeof performance !== "undefined" &&
      typeof performance.now === "function"
    ) {
      return performance.now();
    }
  } catch {
    /* fall through */
  }
  return Date.now();
}

function recordActiveTiming(
  checkedAt: number,
  graceSeconds: number,
  maxSeenWallMs?: number,
): void {
  memCheckedAt = checkedAt;
  memGraceSeconds = clampGrace(graceSeconds);
  memInitialAgeMs = Math.max(0, Date.now() - checkedAt);
  memMonoAt = monoNow();
  memMaxSeenWall =
    typeof maxSeenWallMs === "number" && maxSeenWallMs > checkedAt
      ? maxSeenWallMs
      : checkedAt;
}

function clearActiveTiming(): void {
  memCheckedAt = null;
  memMonoAt = null;
  memInitialAgeMs = 0;
  memMaxSeenWall = null;
}

/** True iff we hold an ACTIVE answer whose grace window has now lapsed.
 *  Effective age = MAX(wall delta, monotonic age, high-water age) so rolling
 *  the clock backward can only make the kit expire sooner, never later. */
function activeGraceExpired(): boolean {
  if (memStatus !== "active" || memCheckedAt === null) return false;
  const wallAgeMs = Date.now() - memCheckedAt;
  const monoAgeMs =
    memMonoAt === null
      ? Number.NEGATIVE_INFINITY
      : memInitialAgeMs + (monoNow() - memMonoAt);
  const highWaterAgeMs =
    memMaxSeenWall === null
      ? Number.NEGATIVE_INFINITY
      : memMaxSeenWall - memCheckedAt;
  const ageMs = Math.max(wallAgeMs, monoAgeMs, highWaterAgeMs);
  if (ageMs < 0) return false;
  return ageMs > memGraceSeconds * 1000;
}

function maybeExpireActiveGrace(): void {
  if (!memInert && activeGraceExpired()) {
    memInert = true;
    notify();
  }
}

function bumpAndPersistMaxSeenWall(): void {
  if (memStatus !== "active" || memCheckedAt === null) return;
  const now = Date.now();
  if (memMaxSeenWall !== null && now <= memMaxSeenWall) return;
  memMaxSeenWall = now;
  try {
    const cache: EntitlementCache = {
      status: "active",
      checkedAt: memCheckedAt,
      graceSeconds: memGraceSeconds,
      maxSeenWallMs: memMaxSeenWall,
    };
    storage.set(CACHE_KEY, JSON.stringify(cache));
  } catch {
    /* non-fatal — the in-memory high-water mark already advanced */
  }
}

function onOfflineCheckin(): void {
  bumpAndPersistMaxSeenWall();
  maybeExpireActiveGrace();
}

/** Last known entitlement status (cached). For the panel's inert message. */
export function getEntitlementStatus(): EntitlementStatus {
  return memStatus;
}

/** Human-readable reason for the current non-active status, if any. */
export function getEntitlementMessage(): string | null {
  if (!isBoosthisDisabled() && memActivated !== true && !memInert) {
    return LOCKED_MESSAGE;
  }
  return memMessage;
}

/* ─── Hydration from cache ────────────────────────────────────────────── */

function applyCache(cache: EntitlementCache): void {
  memActivated = true;
  memStatus = cache.status;
  if (cache.status !== "active") {
    memInert = true;
    clearActiveTiming();
    return;
  }
  let checkedAt = cache.checkedAt;
  const ageMs = Date.now() - checkedAt;
  if (ageMs < 0) {
    if (-ageMs <= CLOCK_SKEW_TOLERANCE_MS) {
      checkedAt = Date.now();
    } else {
      memInert = true;
      clearActiveTiming();
      return;
    }
  }
  recordActiveTiming(checkedAt, cache.graceSeconds, cache.maxSeenWallMs);
  memInert = activeGraceExpired();
}

/**
 * Read the cached last-good answer into the in-memory gate. Idempotent — runs
 * its real work at most once. No valid cache ⇒ the ACTIVATION LOCK stays
 * engaged (fail-closed). Never throws. SYNCHRONOUS (web storage is sync); the
 * gate is correct the instant this returns.
 */
export function hydrateEntitlement(): void {
  if (hydrated) return;
  let activated = false;
  try {
    const raw = storage.get(CACHE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<EntitlementCache>;
      if (
        parsed &&
        (parsed.status === "active" ||
          parsed.status === "revoked" ||
          parsed.status === "unpaid" ||
          parsed.status === "tampered" ||
          parsed.status === "paused") &&
        typeof parsed.checkedAt === "number"
      ) {
        applyCache({
          status: parsed.status,
          checkedAt: parsed.checkedAt,
          graceSeconds:
            typeof parsed.graceSeconds === "number"
              ? parsed.graceSeconds
              : DEFAULT_GRACE_SECONDS,
          maxSeenWallMs:
            typeof parsed.maxSeenWallMs === "number"
              ? parsed.maxSeenWallMs
              : undefined,
        });
        activated = true;
      }
    }
  } catch {
    /* storage unreadable ⇒ LOCKED (fail-closed) */
  } finally {
    // Never DOWNGRADE: a live server answer may have activated the kit already.
    if (memActivated !== true) memActivated = activated;
    hydrated = true;
    notify();
  }
}

/* ─── The check-in (recovery path — never gated by inert) ──────────────── */

function applyServerAnswer(
  status: EntitlementStatus,
  graceSeconds: number,
  message: string | null,
): void {
  const wasInert = isRuntimeInert();
  // Also compare how the gate must be PRESENTED. The first answer a
  // never-confirmed install ever gets can be a non-active one (revoked,
  // unpaid, paused, tampered): that leaves the inert boolean unchanged at
  // true, while the screen goes from "never handshaken, show nothing" to a
  // visible lock. A subscriber told only about the boolean would go on waiting
  // for an answer that has already arrived.
  const wasKind = getEntitlementGateKind();
  // …and whether the handshake itself completed. A first "tampered" answer
  // changes neither the boolean nor the presented kind (both stay hidden and
  // inert), yet it IS the answer this install was waiting for — a subscriber
  // never told would go on waiting for something that already happened.
  const wasActivated = memActivated === true;
  memActivated = true;
  memStatus = status;
  memMessage = status === "active" ? null : message;
  if (status === "active") {
    recordActiveTiming(Date.now(), graceSeconds);
    memInert = false;
  } else {
    memInert = true;
    clearActiveTiming();
  }
  if (
    !wasActivated ||
    isRuntimeInert() !== wasInert ||
    getEntitlementGateKind() !== wasKind
  ) {
    notify();
  }
}

/** POST the check-in, bounded by a Promise.race timer (NO AbortController). */
async function postCheckin(
  cfg: EntitlementCheckinConfig,
  token: string,
): Promise<Response | null> {
  const f =
    cfg.fetchImpl ??
    // Our own check-in is not one of the app's backend calls.
    unwatchedFetch() ??
    (typeof fetch === "function" ? (fetch as typeof fetch) : undefined);
  // No usable fetch → return null (no server status to apply), like an offline
  // tick. The check-in must never throw on a transport problem.
  if (!f) return null;
  const url = `${cfg.endpoint.replace(/\/$/, "")}/entitlements/check`;
  const body: {
    installId: string;
    kitVersion: string;
    integrity?: { status: "ok" | "mismatch"; manifestHash?: string };
  } = { installId: cfg.installId, kitVersion: cfg.kitVersion };
  const integ = cfg.integrity;
  if (integ && (integ.status === "ok" || integ.status === "mismatch")) {
    body.integrity = {
      status: integ.status,
      ...(typeof integ.manifestHash === "string" && integ.manifestHash
        ? { manifestHash: integ.manifestHash }
        : {}),
    };
  }
  // Defence-in-depth: the payload is fully code-defined, but run the shared PII
  // guard anyway. A hit means abort the send (never throw).
  if (checkNoPII(body)) return null;
  const timeoutMs = cfg.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const timeout = new Promise<null>((resolve) => {
    const t = setTimeout(() => resolve(null), timeoutMs);
    (t as unknown as { unref?: () => void }).unref?.();
  });
  const req = Promise.resolve()
    .then(() =>
      f(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(body),
      }),
    )
    .catch(() => null);
  return Promise.race([req, timeout]);
}

/**
 * Run ONE check-in using the installed config. Returns the resolved status, or
 * null when the check couldn't complete. A null result deliberately leaves the
 * cached state intact — only an explicit server answer changes it (fail-open /
 * sticky-kill).
 *
 * NOTE: intentionally NOT gated by `isRuntimeInert()` — this is the path by
 * which a revoked install learns it has been restored.
 */
export async function checkEntitlementNow(): Promise<EntitlementStatus | null> {
  if (isBoosthisDisabled()) return null;
  const cfg = config;
  if (!cfg) return null;
  const token = cfg.getToken();
  if (!token) return null; // not registered yet — the kit stays LOCKED
  try {
    const res = await postCheckin(cfg, token);
    if (!res || !res.ok) {
      if (res?.status === 401) {
        try {
          const refusal = (await res.json()) as {
            error?: unknown;
            detail?: unknown;
            reason?: unknown;
          };
          if (refusal?.error === "credentials_cut") {
            const reason = refusal.reason;
            const status: EntitlementStatus =
              reason === "revoked" ||
              reason === "unpaid" ||
              reason === "tampered" ||
              reason === "paused"
                ? reason
                : "revoked";
            const message =
              typeof refusal.detail === "string" ? refusal.detail : null;
            applyServerAnswer(status, DEFAULT_GRACE_SECONDS, message);
            try {
              const cache: EntitlementCache = {
                status,
                checkedAt: Date.now(),
                graceSeconds: DEFAULT_GRACE_SECONDS,
              };
              storage.set(CACHE_KEY, JSON.stringify(cache));
            } catch {
              /* cache write failure is non-fatal */
            }
            hydrated = true;
            return status;
          }
        } catch {
          /* unreadable refusal body falls through to offline handling */
        }
      }
      // Unreachable / unauthorized / 429 → keep the cached answer, advance the
      // high-water marker, and let a lapsed active grace expire.
      onOfflineCheckin();
      return null;
    }
    const data = (await res.json()) as {
      status?: string;
      graceSeconds?: number;
      message?: string | null;
    };
    const status = data.status;
    if (
      status !== "active" &&
      status !== "revoked" &&
      status !== "unpaid" &&
      status !== "tampered" &&
      status !== "paused"
    ) {
      return null; // unexpected shape — don't act on it
    }
    const graceSeconds =
      typeof data.graceSeconds === "number"
        ? data.graceSeconds
        : DEFAULT_GRACE_SECONDS;
    applyServerAnswer(status, graceSeconds, data.message ?? null);
    try {
      const now = Date.now();
      const cache: EntitlementCache = {
        status,
        checkedAt: now,
        graceSeconds,
        ...(status === "active" ? { maxSeenWallMs: now } : {}),
      };
      storage.set(CACHE_KEY, JSON.stringify(cache));
    } catch {
      /* cache write failure is non-fatal */
    }
    hydrated = true;
    return status;
  } catch {
    onOfflineCheckin();
    return null; // never let a check-in throw into the host
  }
}

/**
 * Trigger a FRESH server entitlement check — the on-open / cold-start
 * enforcement edge of the VAULT contract. Unlike the cached synchronous gate,
 * this always attempts to reconfirm with the server (subject to the throttle);
 * it does NOT short-circuit on a cache-satisfied "active" answer.
 *
 * Non-blocking, fully self-guarded (never throws into the host, no
 * AbortController, Promise.race-bounded fetch). The verdict is applied by
 * `checkEntitlementNow()` when it lands, flipping the synchronous gate +
 * notifying subscribers so the open panel re-reads (a revoked verdict arriving
 * while the panel is open swaps it to the blocking lock overlay).
 *
 * Offline / unreachable stays UNCHANGED: a network failure keeps the cached
 * answer + grace window, so a transient outage can never brick a paying app —
 * EXCEPT a sticky revoked/killed answer, which the cache holds inert.
 *
 * @param force When true (cold start / init), bypass the throttle. When
 *   false/omitted (on-open), dispatch at most once per FORCE_CHECK_THROTTLE_MS.
 */
export function forceEntitlementCheck(force = false): void {
  if (isBoosthisDisabled()) return;
  if (!config) return;
  if (!force) {
    const now = monoNow();
    if (
      lastForcedCheckAt !== null &&
      now - lastForcedCheckAt < FORCE_CHECK_THROTTLE_MS
    ) {
      return; // throttled — a forced check ran within the window
    }
    lastForcedCheckAt = now;
  } else {
    // A forced (launch) check resets the throttle window so an on-open check
    // immediately after cold start doesn't fire a redundant second knock.
    lastForcedCheckAt = monoNow();
  }
  const identity = currentCheckIdentity(config);
  if (identity) lastCheckedIdentity = identity;
  void checkEntitlementNow();
}

/**
 * Ask the server for this install's permission status ONLY if that exact
 * question (this install, this credential) has not already been asked this
 * run. Used by the post-registration edge so a launch produces ONE check
 * rather than two.
 *
 * When registration has just minted the kit's first credential the pair is
 * new, so this asks — that is the first-ever visitor's single check. When the
 * browser already had a credential, the launch check already carried the same
 * pair and this is a no-op.
 */
export function ensureEntitlementChecked(): void {
  if (isBoosthisDisabled()) return;
  if (!config) return;
  const identity = currentCheckIdentity(config);
  // Already asked with exactly this install + credential — the answer that is
  // already in flight (or already applied) is the answer this call wants.
  if (identity !== null && identity === lastCheckedIdentity) return;
  forceEntitlementCheck(true);
}

/* ─── Chasing the FIRST confirmation ──────────────────────────────────── */

/**
 * Delays for the short ladder of extra checks a NEVER-CONFIRMED install runs.
 * Roughly 45 seconds in five knocks, front-loaded.
 *
 * WHY (live customer install, Aug 2026): until the server confirms an install
 * the activation lock keeps the badge hidden and the kit inert — and the only
 * things that ever asked again were the post-registration edge (once) and the
 * 6-hourly heartbeat. So a confirmation that was not ready at that one moment
 * left a correct install dark for as long as the developer kept the tab open;
 * measured at 22 minutes on the project that reported it. Every other state
 * has an event that revisits it; this one had a single shot.
 *
 * Deliberately bounded and self-cancelling: it stops the instant the install is
 * confirmed, and a still-unconfirmed install is left to the heartbeat rather
 * than knocking forever.
 */
const ACTIVATION_CHASE_DELAYS_MS = [1_500, 3_000, 6_000, 12_000, 24_000];

let chaseTimer: ReturnType<typeof setTimeout> | null = null;
let chaseStep = 0;
let chasing = false;

function stopActivationChase(): void {
  if (chaseTimer !== null) {
    try {
      clearTimeout(chaseTimer);
    } catch {
      /* nothing to do */
    }
    chaseTimer = null;
  }
  chaseStep = 0;
  chasing = false;
}

function scheduleActivationChase(): void {
  if (chaseStep >= ACTIVATION_CHASE_DELAYS_MS.length) {
    stopActivationChase();
    return;
  }
  const delay = ACTIVATION_CHASE_DELAYS_MS[chaseStep++];
  try {
    chaseTimer = setTimeout(() => {
      chaseTimer = null;
      if (isBoosthisDisabled() || !config || isActivated()) {
        stopActivationChase();
        return;
      }
      void checkEntitlementNow()
        .then(() => {
          if (isActivated()) stopActivationChase();
          else scheduleActivationChase();
        })
        .catch(() => {
          scheduleActivationChase();
        });
    }, delay);
    // Never keep a Node-side entrypoint alive just to chase a confirmation.
    (chaseTimer as unknown as { unref?: () => void }).unref?.();
  } catch {
    stopActivationChase();
  }
}

/**
 * Ask again, a few times, while this install has never been confirmed.
 *
 * Idempotent and cheap: it does nothing for an install that is already
 * confirmed (every returning visitor), and a knock made before the credential
 * exists costs no request at all — `checkEntitlementNow()` returns immediately
 * without one, and the ladder simply carries on until registration has minted
 * it. Started both when the heartbeat is installed and again when registration
 * succeeds, so a first-ever visitor is covered whichever lands first.
 */
export function chaseFirstActivation(): void {
  if (isBoosthisDisabled()) return;
  if (!config) return;
  if (chasing) return;
  if (isActivated()) return;
  chasing = true;
  chaseStep = 0;
  scheduleActivationChase();
}

/**
 * Does this install actually hold a credential yet — i.e. did registration
 * succeed?
 *
 * The waiting notice speaks in the first person about being REGISTERED, so it
 * may only speak once that is true. An install whose registration failed or
 * has not answered yet is a different state with its own single line, and two
 * contradictory explanations are worse than one.
 */
export function hasInstallCredential(): boolean {
  if (credentialForTests !== undefined) return credentialForTests !== null;
  if (!config) return false;
  try {
    const token = config.getToken();
    return typeof token === "string" && token !== "";
  } catch {
    return false; // a throwing getter is treated as "no credential"
  }
}

let credentialForTests: string | null | undefined = undefined;

/** @internal test hook — stand in for a registration that has (not) landed. */
export function _setInstallCredentialForTests(
  token: string | null | undefined,
): void {
  credentialForTests = token;
}

/* ─── Lifecycle (called by enableTelemetry / forget) ──────────────────── */

/**
 * Install the check-in config and start the heartbeat: hydrate the cache, run
 * an immediate FORCED launch check-in, then poll on an interval. Safe to call
 * repeatedly — it replaces any prior config and timer. All async work is fire-
 * and-forget and self-guarded so it can never crash the host.
 */
export function startEntitlementCheckin(cfg: EntitlementCheckinConfig): void {
  config = cfg;
  stopTimer();
  hydrateEntitlement();
  // Cold-start / init enforcement edge (VAULT contract): a FORCED launch
  // check that bypasses the cache-satisfied fast path.
  //
  // It can only actually GO OUT if this browser already holds a credential —
  // checkEntitlementNow returns immediately without a request otherwise. So
  // the throttle and the asked-already marker are seeded only when a check
  // really was dispatched; seeding them regardless (as this used to) would
  // silence the post-registration check on a first-ever visitor, who has no
  // credential here and therefore never gets an answer at all.
  const identity = currentCheckIdentity(cfg);
  if (identity !== null) {
    lastCheckedIdentity = identity;
    void checkEntitlementNow();
    // A launch check just ran — seed the throttle so an immediate on-open
    // check doesn't fire a redundant second knock.
    lastForcedCheckAt = monoNow();
  }
  const interval = cfg.intervalMs ?? DEFAULT_INTERVAL_MS;
  timer = setInterval(() => {
    void checkEntitlementNow();
  }, interval);
  // Don't keep a Node-side entrypoint alive just for the heartbeat.
  (timer as unknown as { unref?: () => void }).unref?.();
  // A never-confirmed install gets a short ladder of extra knocks on top of
  // the launch check, so the badge appears seconds after the server is ready
  // rather than at the next lifecycle edge.
  chaseFirstActivation();
}

function stopTimer(): void {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

/** Stop the heartbeat and drop the config. Called from `forget()`. */
export function stopEntitlementCheckin(): void {
  stopTimer();
  stopActivationChase();
  config = null;
  // forget() wipes the credential, so whatever was asked before this point is
  // no longer the question a later registration will need answered.
  lastCheckedIdentity = null;
}

/**
 * Erase the persisted entitlement cache and RE-LOCK the kit. Called from
 * `forget()`. Best-effort, never throws. SYNCHRONOUS (web storage is sync).
 */
export function clearEntitlementCache(): void {
  const wasInert = isRuntimeInert();
  memActivated = false;
  memStatus = "active";
  memInert = false;
  memMessage = null;
  clearActiveTiming();
  memGraceSeconds = DEFAULT_GRACE_SECONDS;
  hydrated = true; // the (now empty) state IS the truth — don't rehydrate over it
  try {
    storage.remove(CACHE_KEY);
  } catch {
    /* storage failure is non-fatal — the in-memory lock is already engaged */
  }
  if (isRuntimeInert() !== wasInert) notify();
}

/* ─── Test helpers ────────────────────────────────────────────────────── */

/** Reset ALL module state to first-run defaults (pre-hydration ⇒ LOCKED). */
export function _resetEntitlementForTests(): void {
  stopTimer();
  stopActivationChase();
  config = null;
  memActivated = null;
  memStatus = "active";
  memInert = false;
  memMessage = null;
  memCheckedAt = null;
  memGraceSeconds = DEFAULT_GRACE_SECONDS;
  memInitialAgeMs = 0;
  memMonoAt = null;
  memMaxSeenWall = null;
  lastForcedCheckAt = null;
  lastCheckedIdentity = null;
  hydrated = false;
  listeners.clear();
}

/** Force the in-memory gate (bypasses cache/network) for assertions. */
export function _setEntitlementStateForTests(
  status: EntitlementStatus,
  inert: boolean,
  message: string | null = null,
): void {
  memActivated = true;
  memStatus = status;
  memInert = inert;
  memMessage = message;
  memCheckedAt = null;
  memGraceSeconds = DEFAULT_GRACE_SECONDS;
  memInitialAgeMs = 0;
  memMonoAt = null;
  memMaxSeenWall = null;
  hydrated = true;
  notify();
}
