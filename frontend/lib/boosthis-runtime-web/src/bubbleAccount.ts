/**
 * bubbleAccount — the web bubble's "Connect to your account" card (DOM layer).
 *
 * Web port of the RN kit's BoosthisAccountCard. It renders inside the bubble's
 * CLOSED shadow root and drives the sign-in flow in `account.ts`:
 *   - The sign-in form is ALWAYS shown when an endpoint is configured (i.e. the
 *     host called `enableTelemetry`), even before the install registers.
 *   - After sign-in it shows the signed-in state.
 *   - The CLAIM (link this project) step is offered ONLY when the web kit
 *     actually has BOTH an `installId` AND the `deleteToken` claim credential.
 *     The web kit persists only the READ token itself; the delete token lives
 *     in memory on the active client (seeded via `enableTelemetry({ deleteToken
 *     })` or issued at consent). When it isn't available we show the signed-in
 *     state WITHOUT a claim button rather than a broken one.
 *
 * Guest-safety: this is guest code in a host page. Every listener + async
 * handler is wrapped in try/catch and NEVER throws into the host. Email and
 * password are read from the inputs, passed straight to `account.ts`, and are
 * NEVER logged or persisted. All dynamic values use `textContent`.
 */

import {
  AccountError,
  claimInstallWithTelemetry,
  clearStoredAccount,
  getInstallTelemetryStatus,
  getStoredAccount,
  login,
  logout,
  resendLoginOtp,
  setInstallTelemetry,
  verifyLoginOtp,
  type StoredAccount,
} from "./account";
import { applyServerFullTelemetry, getActiveTelemetryClient } from "./telemetry";

export interface AccountCardHandle {
  /** Re-derive the card's view from the current stored account + client. */
  refresh(): void;
  /** Remove listeners / tear down. Safe to call repeatedly. */
  destroy(): void;
}

const ORANGE = "#f97316";
const MUTED = "#8b8f99";
const ERR = "#ef4444";
const OK = "#22c55e";

type View =
  | { kind: "signed-out" }
  | { kind: "otp"; challengeToken: string; email: string }
  | { kind: "signed-in"; account: StoredAccount };

// Canonical RN DARK_THEME palette — kept byte-identical with the bubble panel
// (card #15171c, borders #262932, input bg #0b0c10, fg #e6e7eb, muted #8b8f99,
// orange #f97316 buttons) so the account card matches the rest of the panel.
const ACCOUNT_CARD_CSS = `
    .acct { background: #15171c; border: 1px solid #262932; border-radius: 12px; padding: 12px; margin-bottom: 12px; }
    .acct .t { font-size: 12px; font-weight: 700; color: #e6e7eb; margin-bottom: 4px; }
    .acct .s { font-size: 10.5px; color: ${MUTED}; margin-bottom: 10px; line-height: 1.5; }
    .acct input {
      width: 100%; margin-bottom: 8px; padding: 8px 10px; font-size: 12.5px;
      background: #0b0c10; color: #e6e7eb; border: 1px solid #262932; border-radius: 8px;
      outline: none;
    }
    .acct input:focus { border-color: ${ORANGE}; }
    .acct button {
      width: 100%; padding: 9px 10px; font-size: 12.5px; font-weight: 700;
      background: ${ORANGE}; color: #0b0c10; border: none; border-radius: 8px;
      cursor: pointer;
    }
    .acct button[disabled] { opacity: 0.55; cursor: default; }
    .acct button.ghost { background: transparent; color: ${MUTED}; border: 1px solid #262932; margin-top: 8px; }
    .acct .msg { margin-top: 8px; font-size: 10.5px; line-height: 1.5; }
    .acct .who { font-size: 12.5px; color: #e6e7eb; font-weight: 600; }
    .acct .tele { margin-top: 12px; padding-top: 12px; border-top: 1px solid #262932; }
    .acct .tele-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .acct .tele-lw { flex: 1; min-width: 0; }
    .acct .tele-lb { font-size: 12.5px; font-weight: 700; color: #e6e7eb; }
    .acct .tele-sub { font-size: 10.5px; color: ${MUTED}; margin-top: 2px; line-height: 1.5; }
    .acct .tele-sw {
      flex: 0 0 auto; width: 46px; height: 26px; border-radius: 999px; border: none;
      cursor: pointer; position: relative; padding: 0; transition: background 0.15s;
      background: #262932;
    }
    .acct .tele-sw.on { background: ${ORANGE}; }
    .acct .tele-sw[disabled] { opacity: 0.6; cursor: default; }
    .acct .tele-knob {
      position: absolute; top: 3px; left: 3px; width: 20px; height: 20px;
      border-radius: 50%; background: #fff; transition: left 0.15s;
    }
    .acct .tele-sw.on .tele-knob { left: 23px; }
  `;

/** @internal test hook — the account card's canonical-palette CSS, exposed so
 *  parity tests can assert it matches the panel's DARK_THEME without a DOM. */
export function _getAccountCardCssForTests(): string {
  return ACCOUNT_CARD_CSS;
}

/** Read the live auth context from the active telemetry client. When no client
 *  is active (host never called enableTelemetry) the endpoint is null and the
 *  card renders a short "connect telemetry first" note instead of a form. */
function authContext(): {
  endpoint: string | null;
  installId: string | null;
  deleteToken: string | null;
  fullTelemetryEffective: boolean;
  fetchImpl?: typeof fetch;
} {
  try {
    const c = getActiveTelemetryClient();
    if (!c) {
      return {
        endpoint: null,
        installId: null,
        deleteToken: null,
        fullTelemetryEffective: false,
      };
    }
    return {
      endpoint: c.endpoint,
      installId: c.installId,
      deleteToken: c.deleteToken,
      fullTelemetryEffective: c.fullTelemetryEffective === true,
    };
  } catch {
    return {
      endpoint: null,
      installId: null,
      deleteToken: null,
      fullTelemetryEffective: false,
    };
  }
}

/** Ask the kit to replace the install credential this page is holding — see
 *  `TelemetryClient.renewCredential`. Never throws and never waits on a client
 *  that cannot do it (an older kit, or none at all): both answer null, exactly
 *  like a renewal that found nothing to restore. */
async function renewInstallCredential(): Promise<string | null> {
  try {
    const c = getActiveTelemetryClient();
    if (!c || typeof c.renewCredential !== "function") return null;
    return await c.renewCredential();
  } catch {
    return null;
  }
}

/** True when the refusal was the server rejecting the install credential this
 *  call PRESENTED. Both halves matter: a call that sent no credential was not
 *  refused for holding a replaced one, so it must never trigger a renewal. */
function credentialRefused(err: unknown, sentToken: string | null): boolean {
  return (
    !!sentToken && err instanceof AccountError && err.code === "stale_install"
  );
}

/** Prefix on the line a self-healed action shows. Plain words on purpose: the
 *  developer did nothing wrong, nothing is broken, and the action went
 *  through — they only need to know why the page reconnected itself. */
const CREDENTIAL_RENEWED_NOTE =
  "Another browser had taken over this project's connection, so this page reconnected itself first.";

/** Shown when the credential could not be renewed: the action genuinely did
 *  not happen, and reloading is the one step that reliably fixes it. */
const CREDENTIAL_LOST_LINE =
  "Another browser has taken over this project's connection, so this page can no longer act on it. Reload this page to reconnect. If that doesn't help, check the project isn't paused or removed in your dashboard.";

/** How often the card re-reads the auth context while it is still incomplete
 *  (no installId / no deleteToken), and how long it keeps trying. A FRESH
 *  install registers SECONDS after the page loaded, so a one-shot read left the
 *  developer stuck on "reload after telemetry registers" — a dead end. Mirrors
 *  the Node kit's acctPollStart() (2s cadence, 2-minute window). */
const CONTEXT_POLL_MS = 2000;
const CONTEXT_POLL_WINDOW_MS = 120_000;

export interface MountAccountCardOptions {
  /**
   * Called with `true` the FIRST time a claim call verifies the project is
   * linked to the signed-in account (a successful `claimInstall` — HTTP 200,
   * whether it linked now or was already linked). Mere sign-in does NOT fire
   * this. The gate uses it to decide `connected`. Best-effort: a throw from
   * this host-provided callback is swallowed so it can never break the panel.
   */
  onConnectedChange?: (connected: boolean) => void;
  /**
   * Called whenever the auto-refresh poll observes the auth context CHANGE —
   * most importantly when a fresh install's `installId`/`deleteToken` finally
   * land, seconds after the page loaded. The bubble re-runs `syncGate()` so the
   * gate re-resolves against the now-known install with no page reload.
   * Best-effort: a throw from this host-provided callback is swallowed.
   */
  onContextChange?: () => void;
  /**
   * Called after the developer signs out of the kit (session already cleared).
   * The bubble uses it to wipe terms acceptance and return to the full gate
   * flow (sign-in → telemetry → terms). Best-effort: a throw from this
   * host-provided callback is swallowed so it can never break the panel.
   */
  onSignedOut?: () => void;
}

export function mountAccountCard(
  doc: Document,
  container: HTMLElement,
  options?: MountAccountCardOptions,
): AccountCardHandle {
  const onConnectedChange = options?.onConnectedChange;
  const onSignedOut = options?.onSignedOut;
  const onContextChange = options?.onContextChange;
  const emitContextChange = () => {
    try {
      onContextChange?.();
    } catch {
      // a host callback throw must never break the panel or host page
    }
  };
  const emitSignedOut = () => {
    try {
      onSignedOut?.();
    } catch {
      // a host callback throw must never break the panel or host page
    }
  };
  const emitConnected = () => {
    try {
      onConnectedChange?.(true);
    } catch {
      // a host callback throw must never break the panel or host page
    }
  };
  const css = doc.createElement("style");
  css.textContent = ACCOUNT_CARD_CSS;
  container.appendChild(css);

  const card = doc.createElement("div");
  card.className = "acct";
  container.appendChild(card);

  let view: View = (() => {
    const acct = getStoredAccount();
    return acct ? { kind: "signed-in", account: acct } : { kind: "signed-out" };
  })();
  let busy = false;
  let destroyed = false;

  // Full-telemetry toggle state. `teleKnown` flips true once a claim is
  // VERIFIED by the server — in either proof mode: the dual proof (session +
  // this page's delete token) or the session-only proof (the delete token is
  // deliberately never persisted, so a later browser session has none; the
  // server then accepts the account's ownership of the project's invite key,
  // or an existing link to that same account, like the dashboard switch does).
  // `teleOn` is seeded from the claim response (falling back to the kit's
  // current effective mode). `teleBusy` guards the in-flight POST. Reset to
  // unknown on sign-out.
  let teleOn = false;
  let teleKnown = false;
  let teleBusy = false;
  // Auto-link bookkeeping. `autoClaimedFor` remembers the (session, install)
  // pair already attempted so the automatic claim fires exactly once; a manual
  // tap marks the pair too, so a failure is never retried in a loop. `claiming`
  // guards the in-flight call for BOTH paths (manual tap and auto-link).
  let autoClaimedFor: string | null = null;
  let claiming = false;
  let claimMsg: { text: string; ok: boolean } | null = null;
  // Credential self-heal bookkeeping. A second browser (or tab) that reconnects
  // this same install is handed the install's credential, and that re-issue
  // rotates — so the copy holding the previous one starts having its panel
  // actions refused with nothing to show for it. The first such refusal renews
  // this page's credential and retries the action; ONCE per panel, so a
  // refusal with any other cause can never become a re-consent loop.
  let credentialRenewAttempted = false;

  /** Renew this page's install credential at most once. Returns the fresh
   *  credential, or null when there is none to be had (already tried, no kit,
   *  or the server had nothing to hand back). */
  async function renewCredentialOnce(): Promise<string | null> {
    if (credentialRenewAttempted) return null;
    credentialRenewAttempted = true;
    return renewInstallCredential();
  }
  // Link PRE-CHECK state. Before offering to link anything we ASK whether this
  // project is already reachable under the signed-in account, instead of
  // offering an action that is already done and then reporting its failure as
  // "this app never registered":
  //   linked    already in this account's dashboard — nothing to offer
  //   missing   we have no record of THIS identity — linking cannot work, and
  //             the reason must name the identity, not a telemetry setting
  //   unlinked  a real, offerable link
  //   error     we could not tell; behave exactly as before (offer it)
  type LinkStatus = "unknown" | "linked" | "unlinked" | "missing" | "error";
  let linkStatus: LinkStatus = "unknown";
  let linkProbedFor: string | null = null;
  let linkProbing = false;
  // Auth-context auto-refresh: the interval handle and its deadline.
  let contextTimer: ReturnType<typeof setInterval> | null = null;
  let contextDeadline = 0;
  let lastContextKey = "";

  const setMsg = (msgEl: HTMLElement, text: string, color: string) => {
    msgEl.textContent = text;
    msgEl.style.color = color;
  };

  /** Wrap an async click handler so it can NEVER throw into the host. */
  const guard = (fn: () => Promise<void>) => {
    return () => {
      if (busy) return;
      Promise.resolve()
        .then(fn)
        .catch(() => {
          // Every handler already surfaces its own message; this is the
          // last-resort net so a bug can never break the host page.
        });
    };
  };

  function render(): void {
    if (destroyed) return;
    // Rebuild the card body from scratch each render (small + simple).
    card.textContent = "";

    const { endpoint, installId, deleteToken, fullTelemetryEffective } =
      authContext();

    const title = doc.createElement("div");
    title.className = "t";
    title.textContent = "Connect to your account";
    card.appendChild(title);

    if (!endpoint) {
      const s = doc.createElement("div");
      s.className = "s";
      s.textContent =
        "Enable Boosthis telemetry in this project to sign in and link it to your dashboard account.";
      card.appendChild(s);
      return;
    }

    if (view.kind === "signed-in") {
      renderSignedIn(
        endpoint,
        installId,
        deleteToken,
        fullTelemetryEffective,
        view.account,
      );
      return;
    }
    if (view.kind === "otp") {
      renderOtp(endpoint, view.challengeToken, view.email);
      return;
    }
    renderSignedOut(endpoint);
  }

  function renderSignedOut(endpoint: string): void {
    const s = doc.createElement("div");
    s.className = "s";
    s.textContent =
      "Sign in with your Boosthis dashboard account to link this project directly — no project-key matching needed.";
    card.appendChild(s);

    const emailInput = doc.createElement("input");
    emailInput.type = "email";
    emailInput.placeholder = "Email";
    emailInput.setAttribute("autocomplete", "username");
    card.appendChild(emailInput);

    const pwInput = doc.createElement("input");
    pwInput.type = "password";
    pwInput.placeholder = "Password";
    pwInput.setAttribute("autocomplete", "current-password");
    card.appendChild(pwInput);

    const btn = doc.createElement("button");
    btn.textContent = "Sign in";
    card.appendChild(btn);

    const msg = doc.createElement("div");
    msg.className = "msg";
    card.appendChild(msg);

    btn.addEventListener(
      "click",
      guard(async () => {
        const email = emailInput.value.trim();
        const password = pwInput.value;
        if (!email || !password) {
          setMsg(msg, "Enter your email and password.", ERR);
          return;
        }
        busy = true;
        btn.disabled = true;
        btn.textContent = "Signing in…";
        setMsg(msg, "", MUTED);
        try {
          const result = await login(endpoint, email, password);
          // Never keep the password around a moment longer than needed.
          pwInput.value = "";
          if (result.status === "otp-required") {
            view = {
              kind: "otp",
              challengeToken: result.challengeToken,
              email: result.email,
            };
            render();
            return;
          }
          view = { kind: "signed-in", account: result.account };
          render();
        } catch (err) {
          setMsg(msg, messageFor(err), ERR);
        } finally {
          busy = false;
          if (!destroyed) {
            btn.disabled = false;
            btn.textContent = "Sign in";
          }
        }
      }),
    );
  }

  function renderOtp(endpoint: string, challengeToken: string, email: string): void {
    const s = doc.createElement("div");
    s.className = "s";
    s.textContent = `We emailed a 6-digit code to ${email}. Enter it below to finish signing in.`;
    card.appendChild(s);

    const codeInput = doc.createElement("input");
    codeInput.type = "text";
    codeInput.inputMode = "numeric";
    codeInput.placeholder = "6-digit code";
    codeInput.setAttribute("autocomplete", "one-time-code");
    card.appendChild(codeInput);

    const btn = doc.createElement("button");
    btn.textContent = "Verify";
    card.appendChild(btn);

    const resend = doc.createElement("button");
    resend.className = "ghost";
    resend.textContent = "Resend code";
    card.appendChild(resend);

    const msg = doc.createElement("div");
    msg.className = "msg";
    card.appendChild(msg);

    btn.addEventListener(
      "click",
      guard(async () => {
        const code = codeInput.value.trim();
        if (!code) {
          setMsg(msg, "Enter the code from your email.", ERR);
          return;
        }
        busy = true;
        btn.disabled = true;
        btn.textContent = "Verifying…";
        setMsg(msg, "", MUTED);
        try {
          const account = await verifyLoginOtp(
            endpoint,
            challengeToken,
            code,
            email,
          );
          view = { kind: "signed-in", account };
          render();
        } catch (err) {
          if (err instanceof AccountError && err.code === "challenge_expired") {
            view = { kind: "signed-out" };
            render();
            return;
          }
          setMsg(msg, messageFor(err), ERR);
        } finally {
          busy = false;
          if (!destroyed) {
            btn.disabled = false;
            btn.textContent = "Verify";
          }
        }
      }),
    );

    resend.addEventListener(
      "click",
      guard(async () => {
        busy = true;
        resend.disabled = true;
        setMsg(msg, "", MUTED);
        try {
          await resendLoginOtp(endpoint, challengeToken);
          setMsg(msg, "A new code is on its way.", OK);
        } catch (err) {
          if (err instanceof AccountError && err.code === "challenge_expired") {
            view = { kind: "signed-out" };
            render();
            return;
          }
          setMsg(msg, messageFor(err), ERR);
        } finally {
          busy = false;
          if (!destroyed) resend.disabled = false;
        }
      }),
    );
  }

  function renderSignedIn(
    endpoint: string,
    installId: string | null,
    deleteToken: string | null,
    fullTelemetryEffective: boolean,
    account: StoredAccount,
  ): void {
    const who = doc.createElement("div");
    who.className = "who";
    who.textContent = `Signed in as ${account.email}`;
    card.appendChild(who);

    const msg = doc.createElement("div");
    msg.className = "msg";

    // The CLAIM step needs an installId and NOTHING ELSE from this page. The
    // delete token is a bonus proof, not a precondition: when it is absent the
    // claim fires without the X-Boosthis-Install-Token header and the SERVER
    // decides, accepting the signed-in account when it owns the invite key
    // this project registered under. Requiring the token here is exactly what
    // deadlocked every browser session after the first — the token is never
    // persisted, so only the registering session ever has it.
    //
    // …but it is only OFFERED when there is something to do. A project that is
    // already reachable under this account needs no linking, and an identity we
    // have no record of cannot be linked at all — offering the button in either
    // case produces a red failure that says nothing true.
    const canClaim =
      !!installId && linkStatus !== "linked" && linkStatus !== "missing";
    if (installId && linkStatus === "linked") {
      const s = doc.createElement("div");
      s.className = "s";
      s.style.marginTop = "8px";
      s.textContent =
        "This project is already in your dashboard under this account — there is nothing to link.";
      card.appendChild(s);
      const idLine = doc.createElement("div");
      idLine.className = "s";
      idLine.style.marginTop = "2px";
      idLine.textContent = `Install ID: ${installId}`;
      card.appendChild(idLine);
      card.appendChild(msg);
    } else if (installId && linkStatus === "missing") {
      const s = doc.createElement("div");
      s.className = "s";
      s.style.marginTop = "8px";
      // Names what was asked and what came back — never a telemetry setting.
      s.textContent = claimMsg?.text ?? "";
      card.appendChild(s);
      card.appendChild(msg);
    } else if (canClaim) {
      const s = doc.createElement("div");
      s.className = "s";
      s.style.marginTop = "8px";
      s.textContent = "Link this project to your account so it appears in your dashboard.";
      card.appendChild(s);
      const idLine = doc.createElement("div");
      idLine.className = "s";
      idLine.style.marginTop = "2px";
      // The identity this page is holding, so it can be compared with the
      // dashboard instead of guessed at.
      idLine.textContent = `Install ID: ${installId}`;
      card.appendChild(idLine);

      // One button, three honest states: idle (tap to link), in-flight (a
      // manual tap OR the automatic link-on-sign-in), and done (already
      // verified — disabled, because the work happened without the developer
      // lifting a finger).
      const claimBtn = doc.createElement("button");
      claimBtn.textContent = teleKnown
        ? "Linked"
        : claiming
          ? "Linking…"
          : "Link this project";
      claimBtn.disabled = teleKnown || claiming;
      card.appendChild(claimBtn);
      card.appendChild(msg);
      // The last claim outcome lives in shared state so it survives the
      // re-render that reveals the Full-telemetry toggle, and so a manual tap
      // and the automatic link say exactly the same thing.
      if (claimMsg) setMsg(msg, claimMsg.text, claimMsg.ok ? OK : ERR);

      claimBtn.addEventListener(
        "click",
        guard(async () => {
          if (claiming || teleKnown) return;
          busy = true;
          claimBtn.disabled = true;
          claimBtn.textContent = "Linking…";
          setMsg(msg, "", MUTED);
          try {
            const result = await runClaim(
              endpoint,
              account,
              installId as string,
              deleteToken,
              fullTelemetryEffective,
            );
            claimMsg = result;
          } finally {
            busy = false;
            if (!destroyed) render();
          }
        }),
      );

    } else {
      const s = doc.createElement("div");
      s.className = "s";
      s.style.marginTop = "8px";
      // The only way to land here now is with NO installId at all: the project
      // hasn't registered yet, so there is nothing to link. (The context poll
      // keeps re-reading and this flips to the claim branch the moment it
      // does.)
      s.textContent =
        "This project hasn't registered with Boosthis yet, so it can't be linked from here.";
      card.appendChild(s);
      card.appendChild(msg);
    }

    // Ask before offering: run the link pre-check once per (session, install),
    // and auto-link ONLY when the answer says a link is genuinely missing. The
    // old code claimed first and used the failure as its status line, which is
    // how a project that was already in the dashboard ended up being described
    // as an app that had never registered.
    if (installId) {
      maybeResolveLink(
        endpoint,
        account,
        installId,
        deleteToken,
        fullTelemetryEffective,
      );
    }

    // ---- Full-telemetry toggle -------------------------------------------
    // Rendered once the server has VERIFIED the link (teleKnown, set only by a
    // 200 from the claim). The delete token is no longer part of the
    // condition: /installs/:id/telemetry accepts the account session ALONE for
    // an install linked to that account — the session-only mode, the exact
    // proof the dashboard switch uses — so the toggle works in a later browser
    // session that never saw the delete token. It is still passed through when
    // this page HAS it. On tap it POSTs to the endpoint and, on 200, applies
    // IMMEDIATELY in-session via applyServerFullTelemetry (the SAME
    // directive-apply path the consent response uses) so full telemetry starts
    // without a relaunch. Errors surface a friendly line.
    if (teleKnown && installId) {
      renderTelemetryToggle(endpoint, installId, account);
    }

    const out = doc.createElement("button");
    out.className = "ghost";
    out.textContent = "Sign out";
    card.appendChild(out);

    out.addEventListener(
      "click",
      guard(async () => {
        busy = true;
        out.disabled = true;
        try {
          await logout(endpoint, account.token);
        } catch {
          // logout already clears the in-memory session on failure; be extra safe.
          try {
            clearStoredAccount();
          } catch {
            // ignore
          }
        } finally {
          busy = false;
          view = { kind: "signed-out" };
          // A new sign-in must re-verify the link (and auto-link again from
          // scratch) before the toggle reappears.
          teleKnown = false;
          teleBusy = false;
          autoClaimedFor = null;
          // A new sign-in must re-ask whether the project is reachable under
          // THAT account before it offers (or hides) the link action.
          linkStatus = "unknown";
          linkProbedFor = null;
          linkProbing = false;
          claimMsg = null;
          render();
          // Sign-out restarts the WHOLE gate flow: the bubble clears terms
          // acceptance + the connected flag and swaps back to the gate, so the
          // developer walks sign-in → telemetry → terms again (owner
          // requirement, Jul 2026).
          emitSignedOut();
        }
      }),
    );
  }

  /**
   * ONE code path for BOTH the manual "Link this project" tap and the automatic
   * link-on-sign-in. The account session is ALWAYS required; the install delete
   * token rides along when this page has it and is OMITTED (not sent empty)
   * when it does not, in which case the server decides via the account's
   * ownership of the project's invite key. Returns the friendly, code-mapped
   * line the card shows (401/404/409 wording untouched).
   */
  async function runClaim(
    endpoint: string,
    account: StoredAccount,
    installId: string,
    deleteToken: string | null,
    fullTelemetryEffective: boolean,
  ): Promise<{ text: string; ok: boolean }> {
    claiming = true;
    autoClaimedFor = statusKey(account, installId);
    /** Record a verified link and word its line (prefixed when this page had
     *  to reconnect itself first). */
    const linked = (
      result: { linked: boolean; fullTelemetry: boolean | null },
      note: string,
    ): { text: string; ok: boolean } => {
      // A successful claim (linked now OR already linked) is the ONLY signal
      // that counts as connected for the terms gate.
      emitConnected();
      // Seed the telemetry toggle from the claim response (falling back to the
      // kit's current effective mode) so it appears on the next render.
      teleOn =
        result.fullTelemetry === null
          ? fullTelemetryEffective
          : result.fullTelemetry;
      teleKnown = true;
      return {
        text:
          note +
          (result.linked
            ? "Linked. This project now appears in your dashboard."
            : "This project is already linked to your account."),
        ok: true,
      };
    };
    try {
      try {
        return linked(
          await claimInstallWithTelemetry(
            endpoint,
            account.token,
            installId,
            deleteToken,
          ),
          "",
        );
      } catch (err) {
        // Anything but "the credential you presented was refused" is not ours
        // to repair — let it fall through to the friendly mapping below.
        if (!credentialRefused(err, deleteToken)) throw err;
        const fresh = await renewCredentialOnce();
        // Nothing to act with: say so in plain words rather than leaving the
        // developer with a refusal that reads like their sign-in is broken.
        if (!fresh) return { text: CREDENTIAL_LOST_LINE, ok: false };
        return linked(
          await claimInstallWithTelemetry(
            endpoint,
            account.token,
            installId,
            fresh,
          ),
          `${CREDENTIAL_RENEWED_NOTE} `,
        );
      }
    } catch (err) {
      // A 401 means the persisted session is dead server-side (revoked or past
      // its 30-day TTL) — clear it so the card honestly shows the sign-in form
      // instead of a signed-in state that can't act.
      dropSessionIfUnauthorized(err);
      return { text: messageFor(err), ok: false };
    } finally {
      claiming = false;
    }
  }

  /**
   * ASK FIRST: find out whether this project is already reachable under the
   * signed-in account before offering (or firing) a link.
   *
   * `GET /installs/{id}/telemetry` is session-authed and read-only, and answers
   * three different things this card must not conflate:
   *   200 linked:true   already in this account's dashboard — nothing to offer
   *   200 linked:false  a genuine, offerable link → auto-link as before
   *   404               we have no record of THIS identity at all
   *
   * The 404 is the case the card used to render as "This project isn't
   * registered yet. Make sure telemetry is enabled." — a sentence that was
   * wrong twice over (the project WAS registered, under another identity, and
   * the telemetry setting has nothing to do with a lookup by id). It now says
   * which identity was asked about and that we have no record of it.
   *
   * Runs at most once per (session, install) pair; anything we cannot classify
   * falls back to the previous behaviour (offer the button, auto-link).
   */
  function maybeResolveLink(
    endpoint: string,
    account: StoredAccount,
    installId: string,
    deleteToken: string | null,
    fullTelemetryEffective: boolean,
  ): void {
    if (teleKnown || claiming || linkProbing) return;
    const key = statusKey(account, installId);
    if (linkProbedFor === key) {
      // Already asked. Auto-link only where a link is actually missing.
      if (linkStatus === "unlinked" || linkStatus === "error") {
        maybeAutoClaim(
          endpoint,
          account,
          installId,
          deleteToken,
          fullTelemetryEffective,
        );
      }
      return;
    }
    linkProbedFor = key;
    linkProbing = true;
    void (async () => {
      try {
        const status = await getInstallTelemetryStatus(
          endpoint,
          account.token,
          installId,
        );
        if (status.linked) {
          linkStatus = "linked";
          // Already reachable under this account: that IS the connected proof
          // the terms gate waits for, and the telemetry switch may be shown.
          teleOn =
            status.fullTelemetry === null
              ? fullTelemetryEffective
              : status.fullTelemetry;
          teleKnown = true;
          claimMsg = null;
          emitConnected();
        } else {
          linkStatus = "unlinked";
        }
      } catch (err) {
        if (err instanceof AccountError && err.code === "not_found") {
          linkStatus = "missing";
          claimMsg = { text: err.message, ok: false };
        } else {
          // A dead session must return the card to the sign-in form; anything
          // else is simply "could not tell" and behaves exactly as before.
          dropSessionIfUnauthorized(err);
          linkStatus = "error";
        }
      } finally {
        linkProbing = false;
        if (!destroyed) render();
      }
    })();
  }

  /** Fire the claim automatically, exactly once per (session, install) pair.
   *  A failure leaves the manual Link button in place carrying the same
   *  code-mapped message — no silent retry loop. */
  function maybeAutoClaim(
    endpoint: string,
    account: StoredAccount,
    installId: string,
    deleteToken: string | null,
    fullTelemetryEffective: boolean,
  ): void {
    if (teleKnown || claiming) return;
    if (autoClaimedFor === statusKey(account, installId)) return;
    autoClaimedFor = statusKey(account, installId);
    void (async () => {
      const result = await runClaim(
        endpoint,
        account,
        installId,
        deleteToken,
        fullTelemetryEffective,
      );
      claimMsg = result;
      if (!destroyed) render();
    })();
  }

  /** Build the "Full telemetry" toggle row. Guest-safe: the click handler is
   *  wrapped so a failure can never break the host page. On 200 it applies the
   *  new mode in-session via applyServerFullTelemetry (no relaunch). */
  function renderTelemetryToggle(
    endpoint: string,
    installId: string,
    account: StoredAccount,
  ): void {
    const wrap = doc.createElement("div");
    wrap.className = "tele";

    const row = doc.createElement("div");
    row.className = "tele-row";

    const lw = doc.createElement("div");
    lw.className = "tele-lw";
    const lb = doc.createElement("div");
    lb.className = "tele-lb";
    lb.textContent = "Full telemetry";
    const sub = doc.createElement("div");
    sub.className = "tele-sub";
    sub.textContent =
      "Upload the complete meter picture so your dashboard and AI can see performance. Off = private mode.";
    lw.appendChild(lb);
    lw.appendChild(sub);

    const sw = doc.createElement("button");
    sw.type = "button";
    sw.className = teleOn ? "tele-sw on" : "tele-sw";
    sw.setAttribute("role", "switch");
    const knob = doc.createElement("span");
    knob.className = "tele-knob";
    sw.appendChild(knob);

    const tmsg = doc.createElement("div");
    tmsg.className = "msg";

    const reflect = () => {
      sw.className = teleOn ? "tele-sw on" : "tele-sw";
      sw.setAttribute("aria-checked", teleOn ? "true" : "false");
      sw.setAttribute(
        "aria-label",
        teleOn ? "Full telemetry on" : "Full telemetry off",
      );
    };
    reflect();

    row.appendChild(lw);
    row.appendChild(sw);
    wrap.appendChild(row);
    wrap.appendChild(tmsg);
    card.appendChild(wrap);

    sw.addEventListener(
      "click",
      guard(async () => {
        if (teleBusy) return;
        const next = !teleOn;
        teleBusy = true;
        sw.disabled = true;
        setMsg(tmsg, "", MUTED);
        try {
          // Read the credential LIVE rather than trusting the one this row was
          // drawn with: an earlier action in this same panel may have renewed
          // it, and the row is not redrawn for that. Null stays null — that is
          // the deliberate session-only mode, where the header is omitted.
          const held = authContext().deleteToken;
          let note = "";
          let applied: boolean;
          try {
            applied = await setInstallTelemetry(
              endpoint,
              account.token,
              installId,
              held,
              next,
            );
          } catch (err) {
            if (!credentialRefused(err, held)) throw err;
            const fresh = await renewCredentialOnce();
            if (!fresh) {
              setMsg(tmsg, CREDENTIAL_LOST_LINE, ERR);
              return;
            }
            note = `${CREDENTIAL_RENEWED_NOTE} `;
            applied = await setInstallTelemetry(
              endpoint,
              account.token,
              installId,
              fresh,
              next,
            );
          }
          teleOn = applied;
          reflect();
          // Apply IMMEDIATELY in-session via the kit's existing directive-apply
          // path — no relaunch, no code change.
          applyServerFullTelemetry(applied);
          setMsg(
            tmsg,
            note +
              (applied
                ? "Full telemetry on. Your dashboard and AI now see the complete picture."
                : "Private mode. Only issue-level signals are shared."),
            OK,
          );
        } catch (err) {
          setMsg(tmsg, messageFor(err), ERR);
          dropSessionIfUnauthorized(err);
        } finally {
          teleBusy = false;
          if (!destroyed) sw.disabled = false;
        }
      }),
    );
  }

  // ---- Auth-context auto-refresh ------------------------------------------
  // A FRESH install registers with the server SECONDS after the page loaded,
  // so reading the context once leaves installId/deleteToken null forever and
  // the developer is stuck on "reload after telemetry registers" — a dead end.
  // While the context is incomplete, re-read it every 2s for up to 2 minutes;
  // each observed change re-renders the card and tells the bubble to re-run
  // gateSync(). A change that delivers the credentials while the developer is
  // ALREADY signed in auto-links right there (render → maybeAutoClaim).

  /** Identity of the current context, so a poll only acts on real changes. */
  const contextKey = (): string => {
    const { endpoint, installId, deleteToken } = authContext();
    return `${endpoint ?? ""}\u0001${installId ?? ""}\u0001${deleteToken ? "1" : "0"}`;
  };

  const contextComplete = (): boolean => {
    const { installId, deleteToken } = authContext();
    return !!installId && !!deleteToken;
  };

  const stopContextPoll = (): void => {
    try {
      if (contextTimer != null) clearInterval(contextTimer);
    } catch {
      // ignore
    }
    contextTimer = null;
  };

  const startContextPoll = (): void => {
    try {
      if (contextTimer != null || destroyed || contextComplete()) return;
      contextDeadline = Date.now() + CONTEXT_POLL_WINDOW_MS;
      contextTimer = setInterval(() => {
        try {
          if (destroyed || Date.now() > contextDeadline) {
            stopContextPoll();
            return;
          }
          const key = contextKey();
          if (key !== lastContextKey) {
            lastContextKey = key;
            // Re-render (which auto-links when both proofs just arrived and a
            // session is held) and let the bubble re-resolve the gate.
            render();
            emitContextChange();
          }
          if (contextComplete()) stopContextPoll();
        } catch {
          // a poll tick must never throw into the host
        }
      }, CONTEXT_POLL_MS);
    } catch {
      contextTimer = null;
    }
  };

  lastContextKey = contextKey();
  render();
  startContextPoll();

  return {
    refresh(): void {
      try {
        // Re-derive from the in-memory session in case another surface changed it.
        if (view.kind === "signed-in") {
          const acct = getStoredAccount();
          if (!acct) view = { kind: "signed-out" };
          else view = { kind: "signed-in", account: acct };
        } else if (view.kind === "signed-out") {
          const acct = getStoredAccount();
          if (acct) view = { kind: "signed-in", account: acct };
        }
        lastContextKey = contextKey();
        render();
        // A refresh (panel opened) restarts the window if the context is still
        // incomplete — the developer is looking at the card right now.
        startContextPoll();
      } catch {
        // refresh must never throw into the host
      }
    },
    destroy(): void {
      destroyed = true;
      stopContextPoll();
      try {
        card.remove();
        css.remove();
      } catch {
        // ignore
      }
    },
  };
}

/** Key identifying one (account, install) pair for the once-per-pair auto-link,
 *  so a sign-out/sign-in or an install change re-attempts. */
function statusKey(account: StoredAccount, installId: string): string {
  return `${account.email}\u0001${installId}`;
}

/** Friendly one-line message for a thrown error (AccountError or otherwise). */
function messageFor(err: unknown): string {
  if (err instanceof AccountError) return err.message;
  return "Something went wrong. Please try again.";
}

/**
 * When a call failed because the persisted session is dead server-side
 * (revoked / expired), drop the stored copy so the next card refresh shows
 * the sign-in form again instead of a signed-in state whose actions all 401.
 */
function dropSessionIfUnauthorized(err: unknown): void {
  if (err instanceof AccountError && err.code === "unauthorized") {
    try {
      clearStoredAccount();
    } catch {
      // never throw into the host
    }
  }
}
