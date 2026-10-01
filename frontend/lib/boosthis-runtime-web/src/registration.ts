/**
 * "Is THIS install on file with Boosthis?" — the one answer the in-app panel is
 * allowed to render a registration verdict from.
 *
 * WHY THIS MODULE EXISTS
 * ----------------------
 * The panel used to decide "registered" from browser-local state alone: an
 * endpoint + an installId + a delete token held by THIS browser. The web kit
 * deliberately never persists the delete token in the runtime (only the static
 * tag persists a copy, per-browser), so the same live install read as
 * "registered" in the browser that first registered it and "Not registered yet.
 * This app has not registered with Boosthis, so nothing is being sent." in every
 * other browser — a red banner that an assisting AI reads as proof the install
 * failed. The AI would then change the install line's identity value, producing
 * a second install under the same project, and the banner would say the same
 * thing again.
 *
 * A verdict about the SERVER's records has to come from the server. This module
 * holds that answer, in FIVE internal states so the panel and the consent gate
 * can tell a real negative apart from an unanswered question:
 *
 *   pending      no answer yet (the check has not run, or is in flight)
 *   registered   we asked (or registered) and the install IS on file
 *   unregistered we asked and it is NOT on file / the key was refused
 *   unreachable  we asked and could not get an answer at all
 *   not-sent     we never asked: nothing left this page at all
 *
 * The public verdict collapses `pending`, `unreachable` and `not-sent` into
 * `"unknown"`: three outcomes, never two, so an unanswered check can never be
 * shown as a failed install and can never carry the claim that nothing is
 * being sent.
 *
 * `not-sent` exists because the kill-switch and the activation lock silence the
 * registration POST by handing the caller a locally-invented 204 — which is
 * `ok`, and was therefore read as a confirmed registration. A request that
 * never left the browser proves NOTHING about the server, so it gets a state
 * of its own rather than borrowing either answer.
 *
 * Never throws into the host: every entry point is self-guarded, and a failed
 * probe degrades to `unreachable`.
 */

import { unwatchedFetch } from "./callWatch";
import { markRegistrationOnFile } from "./inactiveNotice";

/** Internal, five-way. `unreachable` is a FAILED attempt, `pending` is no
 *  attempt yet, `not-sent` is an attempt that never left the page — the consent
 *  gate and the status readout treat them differently. */
export type RegistrationState =
  | "pending"
  | "registered"
  | "unregistered"
  | "unreachable"
  | "not-sent";

/** What a display surface is allowed to know: three outcomes, not two. */
export type RegistrationVerdict = "registered" | "unregistered" | "unknown";

let state: RegistrationState = "pending";
let inFlight = false;
let lastAttemptAt = 0;
const listeners = new Set<() => void>();

/** Don't re-ask more than this often while the answer is still missing. The
 *  panel's 2s repaint and every panel open would otherwise hammer the route. */
const RECHECK_MS = 30_000;

function emit(): void {
  for (const fn of [...listeners]) {
    try {
      fn();
    } catch {
      // a listener must never break the state machine
    }
  }
}

function setState(next: RegistrationState): void {
  if (next === state) return;
  state = next;
  // Tell the wording module what the server has said, so no console line or
  // held sentence can contradict a live answer. Done HERE rather than in each
  // setter so every path that reaches a verdict — consent, the probe, the
  // check route — is covered by construction, and a later definite negative
  // (a key revoked mid-session) gives the kit its voice back.
  try {
    markRegistrationOnFile(next === "registered");
  } catch {
    // never break the state machine over a display latch
  }
  emit();
}

/** The server has this install on file — proven by a consent call it accepted,
 *  or by the registration probe below. */
export function markRegistrationConfirmed(): void {
  setState("registered");
}

/** A definite negative: the server does not have this install on file, or it
 *  refused the project key outright (so this app cannot register with it). */
export function markRegistrationRefused(): void {
  setState("unregistered");
}

/** We asked and got no usable answer (network down, 5xx, a server too old to
 *  know the route). Never downgrades a definite answer we already hold: a
 *  registered install stays registered when the connection drops. */
export function markRegistrationUnreachable(): void {
  if (state === "registered" || state === "unregistered") return;
  setState("unreachable");
}

/**
 * Nothing was sent at all: the kill-switch, the activation lock or a throw
 * before the request meant no round trip happened on this page.
 *
 * This is NOT a registration and NOT a failure to reach us — it is the absence
 * of an attempt, and it is the state the false green used to hide. It never
 * overwrites an answer we actually have, including a failed attempt: a page
 * that asked and got nothing back knows strictly more than one that never
 * asked.
 */
export function markRegistrationNotSent(): void {
  if (state !== "pending") return;
  setState("not-sent");
}

export function getRegistrationState(): RegistrationState {
  return state;
}

/** The three-outcome verdict every display surface must use. `not-sent` is
 *  "unknown" for the same reason `unreachable` is: we hold no answer from the
 *  server, and inventing one in either direction is the bug. */
export function getRegistrationVerdict(): RegistrationVerdict {
  if (state === "registered") return "registered";
  if (state === "unregistered") return "unregistered";
  return "unknown";
}

/** Re-render hook: the panel/gate re-paint the moment an answer lands, instead
 *  of waiting for the next poll. Returns an unsubscribe function. */
export function subscribeRegistration(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** Test-only: forget the answer and every latch. */
export function _resetRegistrationForTests(): void {
  state = "pending";
  inFlight = false;
  lastAttemptAt = 0;
  listeners.clear();
  markRegistrationOnFile(false);
}

export interface RegistrationCheckContext {
  endpoint: string | null | undefined;
  installId: string | null | undefined;
  /** The project key this app registers with. Without one the app cannot
   *  register at all, and we cannot ask about it either. */
  projectKey: string | null | undefined;
  fetchImpl?: typeof fetch;
  /** Test seam only. */
  nowMs?: number;
}

/**
 * Ask the server whether this install is on file, and record the answer.
 *
 * `GET /installs/{installId}/registration` with the project key as the bearer —
 * the page already carries that key (it is what the kit registers with), so
 * asking leaks nothing new. The route answers `{ registered: boolean }`, and
 * only ever says `true` for an install on the presented key's own key line.
 *
 * Fire-and-forget: never rejects, never throws, and re-asks at most once every
 * 30s while the answer is still missing. Anything that is not a clean answer
 * (offline, 5xx, an older server that 404s the route itself) lands on
 * `unreachable` — "cannot tell", never "not registered".
 */
export async function checkRegistrationOnce(
  ctx: RegistrationCheckContext,
): Promise<void> {
  try {
    // A definite answer is never re-asked; local latching keeps the panel calm.
    if (state === "registered" || state === "unregistered") return;
    if (inFlight) return;
    const now = ctx.nowMs ?? Date.now();
    if (lastAttemptAt !== 0 && now - lastAttemptAt < RECHECK_MS) return;

    const endpoint = ctx.endpoint;
    const installId = ctx.installId;
    if (!endpoint || !installId) {
      // Nothing to ask about: the kit never started. That is a LOCAL fact, not
      // a guess about the server, so it is a definite negative.
      setState("unregistered");
      return;
    }
    if (!ctx.projectKey) {
      // No key means this app cannot register with us at all — equally a local
      // fact. (A host that supplied its own credentials instead is handled by
      // the caller, which weighs local credentials before asking.)
      setState("unregistered");
      return;
    }
    // Our own registration check is not one of the app's backend calls: when
    // the call watcher is installed, go under its wrapper.
    const f =
      ctx.fetchImpl ??
      unwatchedFetch() ??
      (typeof fetch === "function" ? fetch : null);
    if (!f) {
      // No way to ask at all — that is "cannot tell", never a failed install.
      setState("unreachable");
      return;
    }

    inFlight = true;
    lastAttemptAt = now;
    try {
      const url = `${endpoint}/installs/${encodeURIComponent(installId)}/registration`;
      const res = await f(url, {
        method: "GET",
        headers: {
          accept: "application/json",
          authorization: `Bearer ${ctx.projectKey}`,
        },
      });
      if (res.status === 200) {
        let registered: unknown = undefined;
        try {
          const body = (await res.json()) as { registered?: unknown };
          registered = body?.registered;
        } catch {
          // Unreadable body — we asked, but we did not get an answer.
        }
        if (registered === true) setState("registered");
        else if (registered === false) setState("unregistered");
        else setState("unreachable");
        return;
      }
      if (res.status === 401 || res.status === 403) {
        // The key was refused, so this app cannot be registered under it.
        setState("unregistered");
        return;
      }
      // 404 (a server too old to know this route), 429, 5xx, anything else:
      // we could not get an answer. Say exactly that.
      setState("unreachable");
    } finally {
      inFlight = false;
    }
  } catch {
    inFlight = false;
    try {
      markRegistrationUnreachable();
    } catch {
      // never throw into the host
    }
  }
}
