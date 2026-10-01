/* ─── Boosthis: what KIND of dependency a destination is (browser) ───────
 *
 * The SAME closed numeric vocabulary the server kits use, mirrored here so one
 * number means one thing on every surface. The browser can only ever emit
 * `own-backend`: connection phases are hidden for anything that is not the
 * app's own origin unless the other side opts in, and almost nobody does — so
 * this kit speaks about the app's own backend and abstains for everything
 * else, rather than showing a zero it did not measure.
 */

/** Nothing decided (never emitted as a row). */
export const DEP_KIND_UNKNOWN = 0;
/** The app's own backend — the only kind a browser can honestly time. */
export const DEP_KIND_OWN_BACKEND = 1;
export const DEP_KIND_DATABASE = 2;
export const DEP_KIND_AI = 3;
export const DEP_KIND_STORAGE = 4;
export const DEP_KIND_PAYMENTS = 5;
export const DEP_KIND_OTHER = 6;

/** Highest code this vocabulary can ever emit (the server bounds-checks it). */
export const DEP_KIND_MAX = 6;
