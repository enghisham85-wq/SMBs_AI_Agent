/** The pre-built, hosted one-line install — WRITTEN DOWN ONCE.
 *
 * WHY THIS FILE EXISTS
 * The web runtime ships as TypeScript modules, which need a bundler. Projects
 * without one (a single `index.html`, a page produced in a chat, a hosted
 * no-code site) install the SAME runtime as a pre-built script tag instead.
 * That address used to be written out by hand in every place that mentions it:
 * the AI-facing install guide, the dashboard's setup page, the scan script, a
 * comment in `tag.ts`. An installer who never reached the guide could not find
 * it at all, invented `https://cdn.boosthis.com/boosthis.js`, and the page
 * measured nothing — the browser answered ERR_NAME_NOT_RESOLVED and no startup
 * line was ever printed.
 *
 * So the address, and the `onerror` handler that has to travel with it, are
 * stated here and nowhere else. Every copy that reaches a reader is either
 * built from these constants or checked against them
 * (`artifacts/api-server/src/routes/__tests__/kitTag.test.ts`), so the shipped
 * copy and the served guide cannot drift into two different addresses.
 *
 * Dependency-free and DOM-free on purpose: this module is imported by the
 * server-only kit builder (`integrationKit.ts`), which is typechecked with no
 * `dom` lib inside the api-server, and it ships with the kit so the address
 * arrives with the bytes.
 */

/** The pre-built browser runtime, served by the Boosthis API server at
 *  `GET /kit.js`. This is the ONLY address a page with no build step needs. */
export const HOSTED_KIT_SCRIPT_URL = "https://www.boosthis.com/kit.js";

/** The `onerror` handler pasted onto the script tag. It is the only piece of
 *  Boosthis that lives in the developer's own page, so it is the only thing
 *  that can speak when the kit file never arrives — without it, a blocked or
 *  mistyped address looks exactly like a page with nothing installed. */
export const HOSTED_KIT_TAG_ONERROR =
  "console.error('[boosthis] Boosthis did not load: '+this.src+' never " +
  "arrived, so nothing is measuring. Check the address, the network, and any " +
  "Content-Security-Policy.')";

/** The whole line, with the three values a developer fills in left as
 *  placeholders. Used verbatim by the served install guide and by the
 *  quickstart that ships inside the kit archive. */
export const HOSTED_KIT_SCRIPT_TAG =
  `<script defer src="${HOSTED_KIT_SCRIPT_URL}" ` +
  `data-key="<the project key>" ` +
  `data-install="<a UUID v4 you generate ONCE>" ` +
  `data-project="<friendly name>" ` +
  `onerror="${HOSTED_KIT_TAG_ONERROR}"></script>`;
