/**
 * account — in-page "Connect to your account" flow for the web Boosthis kit.
 *
 * Web port of the RN kit's `src/ui/account.ts`. It lets a developer sign into
 * their Boosthis dashboard account from inside the floating bubble and link
 * (claim) THIS install directly (server-side `installs.accountId`), so
 * ownership no longer depends on invite-key matching.
 *
 * Transport: plain `fetch` against the same `/api` base the telemetry client
 * targets. These calls are deliberately NOT routed through `safeTransmit` /
 * the PII guard — they carry the developer's OWN credentials (email, password,
 * session token, install delete token) on purpose; they are auth, not
 * telemetry, and must never be redacted or dropped by the privacy denylist.
 *
 * Persistence: session token in `localStorage` (RN-kit parity, a24). Earlier
 * versions were memory-only, which signed the developer out on every page
 * reload — including dev-server hot reloads they never see — breaking parity
 * with the app kit, whose sign-in survives restarts. The session token is now
 * persisted under `boosthis:account:v1`, the same trust level as the read
 * token the kit already keeps in `localStorage` (and as the RN kit's
 * AsyncStorage copy — host code can read both; the kit's storage was never a
 * boundary against the host). The kit's no-PII-persistence posture is kept:
 * the RAW email is NEVER written to storage — only a masked display form
 * (`f…@example.com`) rides along for the "Signed in as …" line; the full email
 * lives in memory for the current page session only. The token is still a
 * secret everywhere else: sent only as `Authorization: Bearer <token>`, never
 * logged. Password is never stored or logged. Sign-out and a 401 (revoked /
 * expired server-side, 30-day TTL) clear the stored copy.
 */

export interface StoredAccount {
  /** Account session Bearer token (raw). Treated as a secret. */
  token: string;
  /** The account's email, shown in the "Signed in as …" line. */
  email: string;
}

/**
 * A network/auth failure with a stable `code` the UI maps to a friendly line.
 * Codes: `network` (transport), `invalid_credentials` (401 on login),
 * `invalid_code` / `challenge_expired` (OTP), `unauthorized` (401 on claim —
 * stale session), `stale_install`, `not_found` (404), `already_claimed` (409),
 * `rate_limited` (429), `send_failed`, `server` (anything else).
 */
export class AccountError extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = "AccountError";
    this.code = code;
  }
}

import { unwatchedFetch } from "./callWatch";
import { storage } from "./storage";

/** Storage-adapter key (namespaced `boosthis:` by the adapter) for the
 *  persisted session (token + MASKED email). Same key shape as the RN kit. */
const ACCOUNT_STORAGE_KEY = "account:v1";

/**
 * Mask an email for persistence/display: first character of the local part +
 * `…` + the domain (`finitex8@x.com` → `f…@x.com`). The raw email must never
 * be written to storage (no-PII-persistence posture).
 */
export function maskEmail(email: string): string {
  const at = email.indexOf("@");
  if (at <= 0) return "…";
  return `${email[0]}…${email.slice(at)}`;
}

function readPersistedAccount(): StoredAccount | null {
  try {
    const raw = storage.get(ACCOUNT_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { token?: unknown; email?: unknown };
    if (typeof parsed.token !== "string" || parsed.token.length === 0) return null;
    return {
      token: parsed.token,
      email: typeof parsed.email === "string" ? parsed.email : "…",
    };
  } catch {
    // Corrupt JSON — treat as signed-out (the adapter itself never throws).
    return null;
  }
}

function writePersistedAccount(account: StoredAccount | null): void {
  if (account === null) {
    storage.remove(ACCOUNT_STORAGE_KEY);
  } else {
    storage.set(
      ACCOUNT_STORAGE_KEY,
      // ONLY the token + a masked email — never the raw address.
      JSON.stringify({ token: account.token, email: maskEmail(account.email) }),
    );
  }
}

/**
 * The current session. Module-scoped mirror of the persisted copy; holds the
 * RAW email for this page session when the user just signed in (storage only
 * ever sees the masked form). `null` = signed-out, `undefined` = not yet
 * loaded from storage.
 */
let memoryAccount: StoredAccount | null | undefined = undefined;

export function getStoredAccount(): StoredAccount | null {
  if (memoryAccount === undefined) memoryAccount = readPersistedAccount();
  return memoryAccount;
}

export function saveStoredAccount(account: StoredAccount): void {
  memoryAccount = account;
  writePersistedAccount(account);
}

export function clearStoredAccount(): void {
  memoryAccount = null;
  writePersistedAccount(null);
}

/** Test-only: reset the session (memory + storage) between cases. */
export function _resetStoredAccountForTests(): void {
  memoryAccount = undefined;
  writePersistedAccount(null);
}

/** Join an `/api` base with a leading-slash path, tolerating a trailing slash. */
function joinUrl(endpoint: string, path: string): string {
  return endpoint.replace(/\/+$/, "") + path;
}

/** Fetch impl (host-supplied override, else the global). Never PII-guarded —
 *  these are auth calls. Returns undefined when no fetch is available. */
type FetchImpl = typeof fetch;
function resolveFetch(fetchImpl?: FetchImpl): FetchImpl | undefined {
  if (typeof fetchImpl === "function") return fetchImpl;
  // Boosthis's own traffic is never one of the app's backend calls, so it goes
  // under the call watcher's wrapper when that is installed.
  const unwatched = unwatchedFetch();
  if (unwatched) return unwatched as unknown as FetchImpl;
  if (typeof fetch === "function") return fetch.bind(globalThis);
  return undefined;
}

/**
 * `fetch` with a bounded timeout so a stalled connection fails fast instead of
 * hanging the sign-in button forever. Composes an AbortController when the
 * runtime has one; otherwise races a timer. Never leaks a late rejection.
 */
async function fetchWithTimeout(
  f: FetchImpl,
  url: string,
  init: RequestInit,
  ms = 8000,
): Promise<Response> {
  let ctrl: AbortController | null = null;
  let armedInit = init;
  try {
    if (typeof AbortController === "function") {
      ctrl = new AbortController();
      armedInit = { ...init, signal: ctrl.signal };
    }
  } catch {
    ctrl = null;
    armedInit = init;
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      try {
        ctrl?.abort();
      } catch {
        // aborting must never mask the timeout
      }
      reject(new Error("boosthis: request timed out"));
    }, ms);
  });
  const fetchPromise = Promise.resolve().then(() => f(url, armedInit));
  // Swallow a late settle so it can never become an unhandled rejection.
  fetchPromise.catch(() => {});
  try {
    return await Promise.race([fetchPromise, timeout]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/** Growing gaps between retries — rides out a transient stall. Only the
 *  transport throw / timeout is retried; HTTP status codes are returned
 *  unretried so auth + validation errors stay fast and exact. */
const RETRY_BACKOFFS_MS = [1000, 3000];

async function fetchResilient(
  f: FetchImpl,
  url: string,
  initFactory: () => RequestInit,
  perAttemptTimeoutMs = 8000,
): Promise<Response> {
  let lastErr: unknown;
  for (let attempt = 0; attempt <= RETRY_BACKOFFS_MS.length; attempt++) {
    if (attempt > 0) {
      await new Promise((resolve) =>
        setTimeout(resolve, RETRY_BACKOFFS_MS[attempt - 1]),
      );
    }
    try {
      return await fetchWithTimeout(f, url, initFactory(), perAttemptTimeoutMs);
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr;
}

/**
 * The two possible outcomes of a successful password check:
 *   - `signed-in`   — the server returned a session token directly (it does
 *     this when the code email could not be sent). The account is persisted.
 *   - `otp-required` — the server emailed a 6-digit code and returned a
 *     challenge token. The UI must collect the code and call
 *     {@link verifyLoginOtp} with the challenge token to finish signing in.
 */
export type LoginResult =
  | { status: "signed-in"; account: StoredAccount }
  | { status: "otp-required"; challengeToken: string; email: string };

/**
 * Sign in with email + password against `POST /auth/login`. The endpoint
 * returns the SAME generic 401 for unknown-email and wrong-password (no
 * account enumeration), surfaced as a single "invalid credentials" message. A
 * correct password normally answers with an email-code challenge; only when
 * the code email could not be sent does the server return a session directly.
 */
export async function login(
  endpoint: string,
  email: string,
  password: string,
  fetchImpl?: FetchImpl,
): Promise<LoginResult> {
  const f = resolveFetch(fetchImpl);
  const url = joinUrl(endpoint, "/auth/login");
  if (!f) {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to sign in (no network function available). Check your connection.",
    );
  }
  let res: Response;
  try {
    res = await fetchResilient(f, url, () => ({
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({ email, password }),
    }));
  } catch {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to sign in. Check your connection.",
    );
  }

  if (res.status === 401) {
    throw new AccountError("invalid_credentials", "Wrong email or password.");
  }
  if (res.status === 429) {
    throw new AccountError("rate_limited", "Too many attempts. Try again later.");
  }
  if (!res.ok) {
    throw new AccountError("server", "Login failed. Please try again.");
  }

  let body: {
    token?: unknown;
    account?: { email?: unknown };
    otpRequired?: unknown;
    challengeToken?: unknown;
  };
  try {
    body = (await res.json()) as typeof body;
  } catch {
    throw new AccountError("server", "Login failed. Please try again.");
  }

  if (body.otpRequired === true && typeof body.challengeToken === "string") {
    return { status: "otp-required", challengeToken: body.challengeToken, email };
  }

  const token = typeof body.token === "string" ? body.token : null;
  const acctEmail =
    typeof body.account?.email === "string" ? body.account.email : email;
  if (!token) {
    throw new AccountError("server", "Login failed. Please try again.");
  }

  const account: StoredAccount = { token, email: acctEmail };
  saveStoredAccount(account);
  return { status: "signed-in", account };
}

/**
 * Finish a code-challenged sign-in: submit the emailed 6-digit code together
 * with the challenge token from {@link login} to `POST /auth/login/otp`.
 */
export async function verifyLoginOtp(
  endpoint: string,
  challengeToken: string,
  code: string,
  fallbackEmail: string,
  fetchImpl?: FetchImpl,
): Promise<StoredAccount> {
  const f = resolveFetch(fetchImpl);
  const url = joinUrl(endpoint, "/auth/login/otp");
  if (!f) {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to verify the code. Check your connection.",
    );
  }
  let res: Response;
  try {
    res = await fetchResilient(f, url, () => ({
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({ challengeToken, code }),
    }));
  } catch {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to verify the code. Check your connection.",
    );
  }

  if (res.status === 401) {
    throw new AccountError(
      "invalid_code",
      "That code isn't right. Check the newest email we sent and try again.",
    );
  }
  if (res.status === 400) {
    throw new AccountError(
      "challenge_expired",
      "That code has expired. Sign in again to get a new one.",
    );
  }
  if (res.status === 429) {
    throw new AccountError("rate_limited", "Too many attempts. Try again later.");
  }
  if (!res.ok) {
    throw new AccountError("server", "Couldn't verify the code. Please try again.");
  }

  let body: { token?: unknown; account?: { email?: unknown } };
  try {
    body = (await res.json()) as typeof body;
  } catch {
    throw new AccountError("server", "Couldn't verify the code. Please try again.");
  }
  const token = typeof body.token === "string" ? body.token : null;
  const acctEmail =
    typeof body.account?.email === "string" ? body.account.email : fallbackEmail;
  if (!token) {
    throw new AccountError("server", "Couldn't verify the code. Please try again.");
  }

  const account: StoredAccount = { token, email: acctEmail };
  saveStoredAccount(account);
  return account;
}

/**
 * Ask the server to email a fresh 6-digit code for a pending sign-in
 * (`POST /auth/login/otp/resend`).
 */
export async function resendLoginOtp(
  endpoint: string,
  challengeToken: string,
  fetchImpl?: FetchImpl,
): Promise<void> {
  const f = resolveFetch(fetchImpl);
  const url = joinUrl(endpoint, "/auth/login/otp/resend");
  if (!f) {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to resend the code. Check your connection.",
    );
  }
  let res: Response;
  try {
    res = await fetchResilient(f, url, () => ({
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({ challengeToken }),
    }));
  } catch {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to resend the code. Check your connection.",
    );
  }

  if (res.ok) return;
  if (res.status === 400) {
    throw new AccountError(
      "challenge_expired",
      "That sign-in attempt has expired. Sign in again to get a new code.",
    );
  }
  if (res.status === 429) {
    throw new AccountError(
      "rate_limited",
      "Too many resend requests. Please wait a moment.",
    );
  }
  if (res.status === 502) {
    throw new AccountError(
      "send_failed",
      "We couldn't send a new code right now. Try again in a few minutes.",
    );
  }
  throw new AccountError("server", "Couldn't resend the code. Please try again.");
}

/**
 * The account session is ALWAYS required; the install DELETE token is an
 * OPTIONAL second proof. Pass it when this page has it (the session that
 * registered the install) and it rides along as `X-Boosthis-Install-Token`.
 * Pass `null` and the header is OMITTED ENTIRELY — never sent empty, which the
 * server would read as a failed proof — so the server falls through to its
 * other sanctioned proof: the signed-in account owning the invite key this
 * project registered with.
 *
 * This is what unblocks every browser session after the first: the delete
 * token is deliberately never persisted, so only the registering session ever
 * has it, and requiring it left everyone else stuck on "reload after telemetry
 * registers" forever.
 */
function claimHeaders(
  token: string,
  deleteToken: string | null,
): Record<string, string> {
  return {
    accept: "application/json",
    authorization: `Bearer ${token}`,
    ...(deleteToken ? { "x-boosthis-install-token": deleteToken } : {}),
  };
}

/**
 * Link this install to the signed-in account via
 * `POST /installs/{installId}/claim`. Two proof modes, both requiring the
 * account session: the dual proof (pass the install's DELETE token — the read
 * token is intentionally rejected by the server), or the session-only mode
 * (pass `null`; the server accepts the session when that account owns the
 * project's invite key). Returns true when linked, throws an
 * {@link AccountError} otherwise.
 */
/**
 * The honest "no such install" line. It names WHAT WAS ASKED (the exact
 * identity this copy of the kit is holding) and WHAT CAME BACK (we have no
 * record of it) — and nothing else.
 *
 * It used to read "This project isn't registered yet. Make sure telemetry is
 * enabled." Both halves were wrong: the project WAS registered (under a
 * different identity), and the telemetry setting has nothing to do with a
 * lookup by id. That sentence is what sent readers off changing the install
 * line, which minted yet another identity and made the mismatch worse.
 */
export function claimNotFoundMessage(installId: string): string {
  return (
    `Boosthis has no record of install ${installId}, which is the id this page ` +
    `is using, so there was nothing to link. Compare that id with the one your ` +
    `dashboard shows for this project — if they differ, this page registered ` +
    `under a different id. Do not change the install line's id: that adds ` +
    `another device rather than fixing the mismatch.`
  );
}

export async function claimInstall(
  endpoint: string,
  token: string,
  installId: string,
  deleteToken: string | null,
  fetchImpl?: FetchImpl,
): Promise<boolean> {
  const f = resolveFetch(fetchImpl);
  const url = joinUrl(
    endpoint,
    `/installs/${encodeURIComponent(installId)}/claim`,
  );
  if (!f) {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to link this project. Check your connection.",
    );
  }
  let res: Response;
  try {
    res = await fetchResilient(f, url, () => ({
      method: "POST",
      headers: claimHeaders(token, deleteToken),
    }));
  } catch {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to link this project. Check your connection.",
    );
  }

  if (res.status === 200) {
    try {
      const body = (await res.json()) as { linked?: unknown };
      return body?.linked === true;
    } catch {
      return true;
    }
  }
  if (res.status === 401) {
    let reason: string | null = null;
    try {
      const body = (await res.json()) as { reason?: unknown };
      reason = typeof body?.reason === "string" ? body.reason : null;
    } catch {
      // Unreadable body — fall through to the session-expired default.
    }
    if (reason === "install_token") {
      throw new AccountError(
        "stale_install",
        // The OWNERSHIP proof failed — and linking is NOT impossible from
        // here. Name both routes that actually work instead of implying this
        // page is a dead end: this project's own connection credential (only
        // in the session where it registered), or an account that owns the
        // project's invite key.
        "Couldn't link this project. Linking needs either this project's own connection credential (available in the session where it registered) or an account that owns this project's project key — sign in with that account, or link it from your dashboard.",
      );
    }
    throw new AccountError("unauthorized", "Your session expired. Sign in again.");
  }
  // A 403 is the same class of answer as a 401/install_token: the ownership
  // proof was refused. Same honest explanation, not a hard "impossible".
  if (res.status === 403) {
    throw new AccountError(
      "stale_install",
      "Couldn't link this project. Linking needs either this project's own connection credential (available in the session where it registered) or an account that owns this project's project key — sign in with that account, or link it from your dashboard.",
    );
  }
  if (res.status === 404) {
    throw new AccountError("not_found", claimNotFoundMessage(installId));
  }
  if (res.status === 409) {
    throw new AccountError(
      "already_claimed",
      "This project is already linked to a different account.",
    );
  }
  if (res.status === 429) {
    throw new AccountError("rate_limited", "Too many attempts. Try again later.");
  }
  throw new AccountError("server", "Linking failed. Please try again.");
}

/**
 * Result of a successful claim that also surfaces the install's current
 * server-side full-telemetry override, so the bubble's account card can seed
 * its telemetry toggle in the right state right after linking. `fullTelemetry`
 * is `null` when the server omitted it (older server / re-claim that didn't
 * re-echo it); the card then falls back to the kit's current effective mode.
 */
export interface ClaimResult {
  linked: boolean;
  fullTelemetry: boolean | null;
}

/**
 * Like {@link claimInstall} but returns the claim response's `fullTelemetry`
 * override alongside `linked`. Same two proof modes (`deleteToken` optional —
 * see {@link claimHeaders}) and identical error mapping. Kept separate so the
 * long-standing boolean `claimInstall` public contract is unchanged for
 * existing callers.
 */
export async function claimInstallWithTelemetry(
  endpoint: string,
  token: string,
  installId: string,
  deleteToken: string | null,
  fetchImpl?: FetchImpl,
): Promise<ClaimResult> {
  const f = resolveFetch(fetchImpl);
  const url = joinUrl(
    endpoint,
    `/installs/${encodeURIComponent(installId)}/claim`,
  );
  if (!f) {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to link this project. Check your connection.",
    );
  }
  let res: Response;
  try {
    res = await fetchResilient(f, url, () => ({
      method: "POST",
      headers: claimHeaders(token, deleteToken),
    }));
  } catch {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to link this project. Check your connection.",
    );
  }

  if (res.status === 200) {
    try {
      const body = (await res.json()) as {
        linked?: unknown;
        fullTelemetry?: unknown;
      };
      return {
        linked: body?.linked === true,
        fullTelemetry:
          typeof body?.fullTelemetry === "boolean" ? body.fullTelemetry : null,
      };
    } catch {
      return { linked: true, fullTelemetry: null };
    }
  }
  if (res.status === 401) {
    let reason: string | null = null;
    try {
      const body = (await res.json()) as { reason?: unknown };
      reason = typeof body?.reason === "string" ? body.reason : null;
    } catch {
      // Unreadable body — fall through to the session-expired default.
    }
    if (reason === "install_token") {
      throw new AccountError(
        "stale_install",
        // The OWNERSHIP proof failed — and linking is NOT impossible from
        // here. Name both routes that actually work instead of implying this
        // page is a dead end: this project's own connection credential (only
        // in the session where it registered), or an account that owns the
        // project's invite key.
        "Couldn't link this project. Linking needs either this project's own connection credential (available in the session where it registered) or an account that owns this project's project key — sign in with that account, or link it from your dashboard.",
      );
    }
    throw new AccountError("unauthorized", "Your session expired. Sign in again.");
  }
  // Same ownership-refused class as the 401/install_token branch above.
  if (res.status === 403) {
    throw new AccountError(
      "stale_install",
      "Couldn't link this project. Linking needs either this project's own connection credential (available in the session where it registered) or an account that owns this project's project key — sign in with that account, or link it from your dashboard.",
    );
  }
  if (res.status === 404) {
    throw new AccountError("not_found", claimNotFoundMessage(installId));
  }
  if (res.status === 409) {
    throw new AccountError(
      "already_claimed",
      "This project is already linked to a different account.",
    );
  }
  if (res.status === 429) {
    throw new AccountError("rate_limited", "Too many attempts. Try again later.");
  }
  throw new AccountError("server", "Linking failed. Please try again.");
}

/**
 * Set this install's server-side full-telemetry override via
 * `POST /installs/{installId}/telemetry`. Two proof modes: the dual proof
 * (account session Bearer + install DELETE token — pass `deleteToken`), or the
 * session-only mode for a project ALREADY linked to this account (pass
 * `deleteToken: null`; the header is omitted and the server accepts the
 * session alone, exactly like the dashboard switch). Returns the applied value
 * (echoed by the server). Error codes map to friendly `AccountError`s the
 * toggle surfaces:
 * `plan_limit` (402 turning on), `billing_frozen` (402 frozen), `project_paused`
 * (403), `stale_install` (401 install_token), `unauthorized` (401 session),
 * `already_claimed` (409), `rate_limited` (429), `server` (anything else).
 */
export async function setInstallTelemetry(
  endpoint: string,
  token: string,
  installId: string,
  deleteToken: string | null,
  fullTelemetry: boolean,
  fetchImpl?: FetchImpl,
): Promise<boolean> {
  const f = resolveFetch(fetchImpl);
  const url = joinUrl(
    endpoint,
    `/installs/${encodeURIComponent(installId)}/telemetry`,
  );
  if (!f) {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to change telemetry. Check your connection.",
    );
  }
  let res: Response;
  try {
    res = await fetchResilient(f, url, () => ({
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json",
        authorization: `Bearer ${token}`,
        // Omitted in session-only mode (already-linked project, later
        // session): the server then verifies ownership via the session alone.
        ...(deleteToken ? { "x-boosthis-install-token": deleteToken } : {}),
      },
      body: JSON.stringify({ fullTelemetry }),
    }));
  } catch {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis to change telemetry. Check your connection.",
    );
  }

  if (res.status === 200) {
    try {
      const body = (await res.json()) as { fullTelemetry?: unknown };
      return typeof body?.fullTelemetry === "boolean"
        ? body.fullTelemetry
        : fullTelemetry;
    } catch {
      return fullTelemetry;
    }
  }
  if (res.status === 402) {
    let code: string | null = null;
    try {
      const body = (await res.json()) as { error?: unknown };
      code = typeof body?.error === "string" ? body.error : null;
    } catch {
      // Unreadable body — treat as the plan-limit default.
    }
    if (code === "billing_frozen") {
      throw new AccountError(
        "billing_frozen",
        "Billing is frozen — clear the balance to change this.",
      );
    }
    throw new AccountError("plan_limit", "Full telemetry is a Pro plan feature.");
  }
  if (res.status === 403) {
    throw new AccountError(
      "project_paused",
      "This project is paused — telemetry can't change while paused.",
    );
  }
  if (res.status === 401) {
    let reason: string | null = null;
    try {
      const body = (await res.json()) as { reason?: unknown };
      reason = typeof body?.reason === "string" ? body.reason : null;
    } catch {
      // Unreadable body — fall through to the session-expired default.
    }
    if (reason === "install_token") {
      throw new AccountError(
        "stale_install",
        // The credential this page presented was refused. The usual cause is
        // another browser (or tab) reconnecting the same install, which is
        // handed the install's credential and replaces the one held here — so
        // the honest advice is to reconnect, not to go pressing Repair in the
        // dashboard (that window is for an install with no credential at all).
        // The panel repairs this by itself; this line is what host code
        // calling the function directly gets to see.
        "This page's connection to Boosthis was replaced — another browser reconnected this project. Reload the page to reconnect it.",
      );
    }
    throw new AccountError("unauthorized", "Your session expired. Sign in again.");
  }
  if (res.status === 409) {
    throw new AccountError(
      "already_claimed",
      "This project is linked to a different account.",
    );
  }
  if (res.status === 429) {
    throw new AccountError(
      "rate_limited",
      "Too many changes. Try again in a moment.",
    );
  }
  throw new AccountError("server", "Couldn't change telemetry. Please try again.");
}

/** Result of {@link getInstallTelemetryStatus}. */
export interface TelemetryStatusResult {
  /** True when this install is already linked to the signed-in account. */
  linked: boolean;
  /** Current server-side override; null when unknown or not linked. */
  fullTelemetry: boolean | null;
}

/**
 * Read this install's link + full-telemetry state for the signed-in account
 * via `GET /installs/{installId}/telemetry`. Session-authed and read-only.
 * Used by the account card in a LATER browser session, where the delete token
 * (deliberately never persisted in browser storage) is unavailable: when the
 * project is already linked to this account, the card can still offer the
 * Full-telemetry switch in session-only mode. `linked: false` means either
 * "not linked" or "linked to someone else" — the server keeps the two
 * indistinguishable on purpose.
 */
export async function getInstallTelemetryStatus(
  endpoint: string,
  token: string,
  installId: string,
  fetchImpl?: FetchImpl,
): Promise<TelemetryStatusResult> {
  const f = resolveFetch(fetchImpl);
  const url = joinUrl(
    endpoint,
    `/installs/${encodeURIComponent(installId)}/telemetry`,
  );
  if (!f) {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis. Check your connection.",
    );
  }
  let res: Response;
  try {
    res = await fetchResilient(f, url, () => ({
      method: "GET",
      headers: {
        accept: "application/json",
        authorization: `Bearer ${token}`,
      },
    }));
  } catch {
    throw new AccountError(
      "network",
      "Couldn't reach Boosthis. Check your connection.",
    );
  }

  if (res.status === 200) {
    try {
      const body = (await res.json()) as {
        linked?: unknown;
        fullTelemetry?: unknown;
      };
      const linked = body?.linked === true;
      return {
        linked,
        fullTelemetry:
          linked && typeof body?.fullTelemetry === "boolean"
            ? body.fullTelemetry
            : null,
      };
    } catch {
      return { linked: false, fullTelemetry: null };
    }
  }
  if (res.status === 401) {
    throw new AccountError("unauthorized", "Your session expired. Sign in again.");
  }
  if (res.status === 404) {
    throw new AccountError("not_found", claimNotFoundMessage(installId));
  }
  if (res.status === 429) {
    throw new AccountError("rate_limited", "Too many attempts. Try again later.");
  }
  throw new AccountError("server", "Couldn't check this project's link status.");
}

/**
 * Log out: best-effort revoke the server session, then always clear the
 * in-memory session so the UI returns to the signed-out state even if the
 * network call fails.
 */
export async function logout(
  endpoint: string,
  token: string,
  fetchImpl?: FetchImpl,
): Promise<void> {
  const f = resolveFetch(fetchImpl);
  if (f) {
    try {
      await fetchWithTimeout(f, joinUrl(endpoint, "/auth/logout"), {
        method: "POST",
        headers: { authorization: `Bearer ${token}` },
      });
    } catch {
      // ignore — local clear below is what matters for the UI
    }
  }
  clearStoredAccount();
}
