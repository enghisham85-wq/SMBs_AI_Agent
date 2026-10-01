/**
 * terms — gate-first Terms helper for the web Boosthis kit.
 *
 * The web bubble is GATE-FIRST: on first open the panel shows the full
 * connect + telemetry + terms gate, and the meters render only after the
 * developer signs in (when registered), chooses telemetry, and taps
 * "I Agree & Continue". The full legal document is served by the api-server
 * at `<endpoint origin>/terms`.
 *
 * Acceptance is recorded locally (same key/shape the RN kit uses) and is
 * keyed to BOTH the terms version AND the install identity:
 *   - a `TERMS_VERSION` bump re-prompts everyone, and
 *   - a NEW installId (fresh install/reinstall of the kit) re-prompts too,
 *     so a reinstalled kit always walks the whole sign-in → telemetry →
 *     terms flow again (owner requirement, Jul 2026).
 * Signing out of the kit clears the acceptance as well (see bubble.ts), so
 * the next open starts the whole flow from the top.
 */

import { storage } from "./storage";

/**
 * The accepted terms VERSION. Kept equal to the RN/Node `TERMS_VERSION`
 * constants so an app that already agreed elsewhere is consistent. Bump this
 * only when the material OBLIGATIONS in the served /terms page change.
 */
export const TERMS_VERSION = "2026-07-21";

/** Namespaced under `boosthis:` by `storage`; matches the RN key shape. */
const STORAGE_KEY = "terms-accepted:v1";

export interface TermsAcceptance {
  version: string;
  acceptedAt: number;
  /** The installId the acceptance was recorded under, or null when the page
   *  was unregistered at accept time. Absent on legacy records (treated as
   *  null). */
  installId?: string | null;
}

/**
 * Resolve the hosted Terms & Privacy page URL from the `/api` endpoint
 * (…/api → origin + /terms). Falls back to the default origin when no client
 * has run. Never throws.
 */
export function resolveTermsUrl(endpoint: string): string {
  try {
    const origin = endpoint.replace(/\/api\/?$/, "").replace(/\/+$/, "");
    return `${origin}/terms`;
  } catch {
    return "https://www.boosthis.com/terms";
  }
}

/** Read the stored acceptance, or null when absent/unreadable. Never throws. */
export function getTermsAcceptance(): TermsAcceptance | null {
  try {
    const raw = storage.get(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed.version === "string" &&
      typeof parsed.acceptedAt === "number"
    ) {
      return parsed as TermsAcceptance;
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * True only when the CURRENT terms version has been accepted FOR the current
 * install identity.
 *   - `currentInstallId` null/undefined (unregistered page): version-only
 *     check, so a dev testing the bubble without telemetry isn't re-gated on
 *     every reload.
 *   - `currentInstallId` present (registered): the stored record must carry
 *     the SAME installId. A legacy or foreign-install record does not count —
 *     a reinstall (fresh installId) therefore re-runs the full gate.
 */
export function hasAcceptedCurrentTerms(
  currentInstallId?: string | null,
): boolean {
  const a = getTermsAcceptance();
  if (!a || a.version !== TERMS_VERSION) return false;
  if (currentInstallId == null) return true;
  return (a.installId ?? null) === currentInstallId;
}

/**
 * Record acceptance of the current terms version for the current install
 * identity. Called on the Agree tap only. Best-effort — `storage.set`
 * degrades to memory / never throws.
 */
export function recordTermsAccepted(
  nowMs: number,
  installId?: string | null,
): void {
  try {
    const payload: TermsAcceptance = {
      version: TERMS_VERSION,
      acceptedAt: nowMs,
      installId: installId ?? null,
    };
    storage.set(STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // recording must never break the panel or the host page
  }
}

/**
 * Wipe the stored acceptance. Called on kit sign-out so the whole gate flow
 * (sign-in → telemetry → terms) runs again from the top. Never throws.
 */
export function clearTermsAcceptance(): void {
  try {
    storage.remove(STORAGE_KEY);
  } catch {
    // best-effort
  }
}
