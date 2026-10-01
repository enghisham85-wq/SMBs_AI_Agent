/**
 * "It registered — but on a page load that has already gone."
 *
 * WHY THIS EXISTS
 * ---------------
 * Registration is DURABLE: once Boosthis has an install on file it stays on
 * file, across reloads, tabs and days. Everything the browser kit knew about
 * it was not. The watch, the console line and the status readout all describe
 * ONE page load, so a reload of a perfectly healthy install started again from
 * "announced, never asked" and presented a three-day-old success as a fresh
 * failure. A live storefront did exactly that: the kit said it had never
 * registered while the server had the same install id registered, checked in
 * and measuring.
 *
 * So the one fact that outlives the page is written down: this browser saw
 * Boosthis accept THIS install id, at this time. Nothing else — no token, no
 * key, no server text.
 *
 * WHAT IT IS NOT
 * --------------
 * It is not a verdict and it never becomes one. The server's answer always
 * wins; this may only fill a gap the server has left unanswered (see
 * .agents/memory/panel-registration-verdict.md). Its two uses are both
 * conservative:
 *
 *   - a page that reloaded and holds no credential is "cannot tell" rather
 *     than "it never registered";
 *   - the readout can say WHEN this browser last saw it registered, marked as
 *     this browser's own record rather than as Boosthis's answer.
 *
 * Never throws: storage is optional everywhere (private mode, blocked embeds,
 * SSR), and a kit that cannot remember simply forgets.
 */

import { storage } from "./storage";

/** One entry per install id: a registration is a fact about an INSTALL, and a
 *  browser can legitimately hold several (two apps, two keys, one machine). */
const PREFIX = "registeredAt:";

/** Ignore a stamp from the future beyond ordinary clock skew — a device whose
 *  clock jumped must not be able to file a date nobody can read. */
const FUTURE_SKEW_MS = 24 * 60 * 60 * 1000;

function keyFor(installId: string): string {
  return `${PREFIX}${installId}`;
}

/** Boosthis accepted this install. Write it down, best effort. */
export function rememberRegistered(
  installId: string | null | undefined,
  atMs: number = Date.now(),
): void {
  try {
    if (!installId) return;
    if (!Number.isFinite(atMs) || atMs <= 0) return;
    // Once written, never rewritten: the FIRST time this browser saw the
    // install accepted is the honest answer to "how long has this been
    // working?", and re-stamping it on every page load would turn a
    // three-day-old registration into a brand-new one.
    if (readRegisteredAt(installId) !== null) return;
    storage.set(keyFor(installId), String(Math.floor(atMs)));
  } catch {
    // A kit that cannot remember must still run.
  }
}

/** When this browser last saw Boosthis accept this install, or null when it
 *  never did (or the record cannot be read, which is the same answer here:
 *  this may only ever ADD evidence, never withhold any). */
export function readRegisteredAt(
  installId: string | null | undefined,
): number | null {
  try {
    if (!installId) return null;
    const raw = storage.get(keyFor(installId));
    if (raw === null) return null;
    const at = Number.parseInt(raw, 10);
    if (!Number.isFinite(at) || at <= 0) return null;
    if (at > Date.now() + FUTURE_SKEW_MS) return null;
    return at;
  } catch {
    return null;
  }
}

/** Did this browser ever see Boosthis accept this install? */
export function wasRegisteredBefore(
  installId: string | null | undefined,
): boolean {
  return readRegisteredAt(installId) !== null;
}

/** Erase the record. Called by forget(): erasure leaves nothing
 *  Boosthis-shaped in this browser, and that includes the memory that it was
 *  ever registered. */
export function forgetRegistered(installId: string | null | undefined): void {
  try {
    if (!installId) return;
    storage.remove(keyFor(installId));
  } catch {
    // never throw into the host
  }
}
