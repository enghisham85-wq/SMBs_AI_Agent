/** PII denylist + guard — Web runtime copy.
 *
 * MUST stay in lock-step with `lib/boosthis-runtime-node/src/no-pii.ts` (its
 * byte-for-byte source), `lib/boosthis-runtime-rn/src/no-pii.ts`, and
 * `lib/boosthis-py/boosthis/pii.py` — same denylist, same allowlist, same
 * value patterns, same algorithm. The cross-runtime parity test
 * (`__tests__/no-pii-parity.test.ts`) enforces byte equality with the Node
 * source below this docblock.
 *
 * Duplicated rather than imported so this package has zero runtime deps and
 * runs in a plain browser. See threat_model.md for the Information
 * Disclosure guarantees this enforces.
 */

export const PII_DENYLIST = [
  // identity
  "email", "mail", "username", "userid", "user_id", "uid",
  "firstname", "first_name", "lastname", "last_name",
  "fullname", "full_name", "phone", "mobile", "tel",
  "ssn", "nationalid", "national_id", "taxid", "tax_id",
  "dob", "birthdate", "birthday",
  // account/auth secrets
  "password", "passwd", "secret",
  "apikey", "api_key", "token", "auth", "authorization",
  "session", "cookie", "bearer", "jwt",
  "creditcard", "credit_card", "cardnumber", "card_number", "cvv",
  "iban", "swift", "routingnumber", "routing_number",
  // device-as-identity
  "deviceid", "device_id", "advertisingid", "advertising_id",
  "idfa", "idfv", "macaddress", "mac_address", "imei",
  // network identity
  "ipaddress", "ip_address", "ip", "ipv4", "ipv6",
  "useragent", "user_agent",
  // location
  "latitude", "longitude", "lat", "lng", "lon", "geohash",
  "address", "street", "city", "zipcode", "zip_code",
  "postalcode", "postal_code",
  // free-form content (high-risk for incidental PII)
  "message", "content", "body", "text", "comment", "note",
  "value", "input", "query", "search",
] as const;

const ALLOWLIST = new Set([
  "screen", "screenname", "screen_name",
  "appversion", "app_version",
  "osversion", "os_version",
  "osname", "os_name",
  "findingid", "finding_id",
  "ruleid", "rule_id",
  "score", "rating", "perception",
  "sessionid", "session_id",
  // leakWatch (shared additive "Leak Watch" axis, Aug 2026) emits per-category
  // COUNTERS whose contract-mandated camelCase key names embed denied fragments
  // ("secret" in `secretCount`). These are integers — never any matched value —
  // so the field NAMES are allowlisted here. Kept in lock-step across runtimes.
  "secretcount", "stackcount", "piicount",
  // cookieExposure axis KEY itself collides with the denied cookie fragment
  // via the pass-2 substring check. Code-defined axis identifier whose sub-
  // object is counts-only (no cookie name, value or header text ever rides it).
  // Without this entry the guard drops the ENTIRE snapshot the moment the app
  // sets its first cookie. Kept in lock-step across every kit allowlist.
  "cookieexposure",
  // AI-call visibility (Aug 2026). A provider reports how much of the prompt
  // it read and how much answer it wrote in units it calls TOKENS, and the
  // headroom it publishes is counted in the same units. The field names below
  // therefore collide with the denied `token` fragment while carrying nothing
  // but integers and percentages — never a credential, never a prompt, never
  // an answer. Allowlisted by exact name so the collision cannot widen: any
  // other `*token*` field is still refused. Byte-identical to the same entry
  // in the server guard, so the two can never disagree about one payload.
  "tokensin", "tokensout", "worsttokenspct",
]);

const DENY_NORMALIZED: ReadonlyArray<{ denied: string; norm: string }> =
  PII_DENYLIST.map((d) => ({ denied: d, norm: normalize(d) }));

/** Exported so leakWatch (extraMeters.ts) reuses the SAME email pattern rather
 *  than duplicating one — the pii category of the leak scan. Kept in lock-step
 *  with the cross-runtime denylist (see the file header). */
export const EMAIL_RE = /[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}/;
const IPV4_RE = /\b(?:\d{1,3}\.){3}\d{1,3}\b/;
const IPV6_FULL_RE = /^[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){7}$/i;
const IPV6_HEX_ONLY_RE = /^[0-9a-f:]+$/i;
const IPV6_CANDIDATE_RE = /[0-9a-f:]{4,45}/gi;
const JWT_RE = /eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/;
const BEARER_RE = /bearer\s+\S{8,}/i;
const PHONE_RE =
  /(?<![0-9a-fA-F])(?:\+\d{1,3}[\s\-.])?\(?\d{3}\)?[\s\-.]\d{3}[\s\-.]\d{4}(?![0-9a-fA-F])/;

function normalize(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function tokenize(name: string): string[] {
  const s = name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2");
  return s.split(/[^a-zA-Z0-9]+/).filter(Boolean).map((t) => t.toLowerCase());
}

function looksLikeIPv6(val: string): boolean {
  if (val.length < 3) return false;
  if (IPV6_FULL_RE.test(val)) return true;
  if (!val.includes("::")) return false;
  if (!IPV6_HEX_ONLY_RE.test(val)) return false;
  if (val.indexOf("::") !== val.lastIndexOf("::")) return false;
  const groups = val.split(":").filter((g) => g.length > 0);
  return groups.length >= 1 && groups.length <= 7;
}

function containsEmbeddedIPv6(val: string): boolean {
  const matches = val.match(IPV6_CANDIDATE_RE);
  if (!matches) return false;
  for (const m of matches) if (looksLikeIPv6(m)) return true;
  return false;
}

function findDeniedFragment(name: string): string | null {
  const norm = normalize(name);
  if (ALLOWLIST.has(norm)) return null;
  const tokens = tokenize(name);
  // Pass 1: exact normalized match + per-token match (cheap, no false-positives).
  for (const { denied, norm: dNorm } of DENY_NORMALIZED) {
    if (norm === dNorm) return denied;
    for (const tok of tokens) if (tok === dNorm) return denied;
  }
  // Pass 2: concatenated-fragment containment. Catches `customertoken`,
  // `myemail`, `sessiontokenv2` etc. where casing/separators give us no
  // token boundaries to split on. Limited to denylist entries ≥ 5 chars
  // so short tokens (`ip`, `uid`, `dob`, `cvv`, `jwt`, `lat`, `lng`, `lon`,
  // `tel`, `ssn`) don't false-positive on innocuous identifiers.
  for (const { denied, norm: dNorm } of DENY_NORMALIZED) {
    if (dNorm.length < 5) continue;
    if (norm.includes(dNorm)) return denied;
  }
  return null;
}

function findDeniedValue(val: string): string | null {
  if (val.length < 5) return null;
  if (EMAIL_RE.test(val)) return "~email";
  if (JWT_RE.test(val)) return "~jwt";
  if (BEARER_RE.test(val)) return "~bearer";
  if (IPV4_RE.test(val)) return "~ipv4";
  if (containsEmbeddedIPv6(val)) return "~ipv6";
  if (PHONE_RE.test(val)) return "~phone";
  return null;
}

export class PIIDetectedError extends Error {
  readonly path: string;
  readonly fieldName: string;
  readonly matchedFragment: string;
  constructor(path: string, fieldName: string, matchedFragment: string) {
    super(
      `Boosthis no-PII guard refused outgoing payload: field "${path}" ` +
        `(name "${fieldName}") matches denied fragment "${matchedFragment}". ` +
        "Boosthis's privacy contract forbids transmitting personally identifying data. " +
        "If this field is genuinely non-PII, rename it; otherwise remove it.",
    );
    this.name = "PIIDetectedError";
    this.path = path;
    this.fieldName = fieldName;
    this.matchedFragment = matchedFragment;
  }
}

function walk(
  payload: unknown,
  path: string,
  seen: WeakSet<object>,
): PIIDetectedError | null {
  if (typeof payload === "string") {
    const denied = findDeniedValue(payload);
    if (denied !== null) {
      const fieldName = path.includes(".") ? path.slice(path.lastIndexOf(".") + 1) : path;
      return new PIIDetectedError(path, fieldName, denied);
    }
    return null;
  }
  if (payload === null || typeof payload !== "object") return null;
  if (seen.has(payload as object)) return null;
  seen.add(payload as object);
  if (Array.isArray(payload)) {
    for (let i = 0; i < payload.length; i++) {
      const inner = walk(payload[i], `${path}[${i}]`, seen);
      if (inner) return inner;
    }
    return null;
  }
  for (const [key, val] of Object.entries(payload as Record<string, unknown>)) {
    const childPath = `${path}.${key}`;
    const matched = findDeniedFragment(key);
    if (matched !== null) return new PIIDetectedError(childPath, key, matched);
    const inner = walk(val, childPath, seen);
    if (inner) return inner;
  }
  return null;
}

export function checkNoPII(payload: unknown): PIIDetectedError | null {
  return walk(payload, "$", new WeakSet<object>());
}

export function assertNoPII(payload: unknown): void {
  const hit = checkNoPII(payload);
  if (hit) throw hit;
}

// ── Route-label–specific PII checks ────────────────────────────────────────
// UUID v4/v5 and similar hyphen-grouped hex identifiers. Applied only to
// route labels (not the general walk) because install IDs are also
// UUID-shaped and would trigger a false positive there.
const ROUTE_LABEL_UUID_RE =
  /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
// Long unbroken numeric runs (6+ digits) — order numbers, customer refs, etc.
// Word-boundary anchors prevent false positives on short numbers in version strings.
const ROUTE_LABEL_NUMERIC_RE = /\b\d{6,}\b/;
// Legitimate route/screen labels use PascalCase/camelCase/snake_case/slash paths.
// Any label containing whitespace is user-derived content ("Jane Doe", "Order 873451").
const LABEL_HAS_SPACE_RE = /\s/;

/** Returns the matched ~tag if the route/screen label contains a high-risk
 *  PII pattern that the general assertNoPII cannot catch without false-positiving
 *  on UUID-shaped install identifiers. Includes a whitespace check.
 *
 *  Use this for MANUAL labels (`trackPerf` / `perf`) where the developer
 *  provides the string directly — legitimate code-defined labels never contain
 *  whitespace, so any space is a strong signal of user-derived content
 *  ("Jane Doe", "Order 873451").
 *
 *  Mirrors `routeLabelHasPII` in `lib/boosthis-runtime-rn/src/no-pii.ts`
 *  and `check_route_label` in `lib/boosthis-py/boosthis/pii.py`. */
export function routeLabelHasPII(label: string): string | null {
  if (LABEL_HAS_SPACE_RE.test(label)) return "~space-separated-label";
  return transmitLabelHasPII(label);
}

// Express middleware produces labels of the form "GET /users/:id" —
// a single HTTP method token followed by a space and a slash-prefixed path.
// The snapshot's per-route row keys additionally carry a "screen:" prefix
// ("screen:GET /users/:id") so the live-data MCP tools light up. These labels
// are already PII-redacted by normalizePath(); the space is structural, not
// user content. All other whitespace-containing labels ("Jane Doe",
// "Order 873451", "GET /jane smith") are user-derived and must be blocked.
// Fully anchored (closed method set, exactly one space, path has no further
// whitespace to end-of-string) — mirrors STRUCTURAL_METHOD_PATH_RE in
// artifacts/api-server/src/lib/pii.ts byte-for-byte.
const HTTP_METHOD_PATH_RE =
  /^(?:screen:)?(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|TRACE|CONNECT) \/\S*$/;

/** Returns the matched ~tag if the label contains a high-risk PII pattern.
 *  Use this at transmit / queue time where labels may originate from either
 *  the Express middleware (produces "GET /users/:id" — space is structural)
 *  or manual instrumentation. Whitespace is checked for ALL labels EXCEPT
 *  those that match the strict HTTP-method-plus-path pattern produced by the
 *  middleware, which is already PII-safe after normalizePath() processing. */
export function transmitLabelHasPII(label: string): string | null {
  // Whitespace check: allow "HTTP_METHOD /path" middleware labels only.
  if (LABEL_HAS_SPACE_RE.test(label) && !HTTP_METHOD_PATH_RE.test(label)) {
    return "~space-separated-label";
  }
  const existing = findDeniedValue(label);
  if (existing !== null) return existing;
  if (ROUTE_LABEL_UUID_RE.test(label)) return "~uuid";
  if (ROUTE_LABEL_NUMERIC_RE.test(label)) return "~numeric-id";
  return null;
}

// A name a person typed is not a route label, and one rule is the whole
// difference: whitespace. A route label is code-defined, so a space in one
// means a value got interpolated into a constant. A JOB name is written by
// hand beside a schedule, so a space in one means it is a name — "queue
// drain", "Send Weekly Report", "backup db" are entirely ordinary.
//
// Screening job names with the route-label guard therefore made a whole class
// of legitimate job vanish: the run was dropped here, before upload, with no
// counter and no notice, so the job was not late, not never-reported and not
// on the page — it simply never existed. The identical mistake on app names
// was measured on the hosted deployment (50 of 72 distinct names withheld,
// every one by the space rule, none by any value check); the reading and why
// the stored corpus cannot be asked about job names directly are written up
// in docs/job-name-screening.md.
const NAME_CONTROL_CHAR_RE = /[\u0000-\u001f\u007f]/;

/**
 * Returns the matched ~tag if a BACKGROUND JOB's name must not be sent, or
 * null when it may be.
 *
 * Every VALUE check a route label gets still applies — email, token, address,
 * phone, UUID, long numeric identifier — because a name built out of a value
 * ("sync-user-4482113", "invoice-run-jane@acme.com") is user data whatever
 * field it arrives in. What is gone is the space rule, and only that.
 *
 * Mirrors `checkJobName` in `artifacts/api-server/src/lib/pii.ts` and
 * `check_job_name` in `lib/boosthis-py/boosthis/pii.py`. The three are held to
 * each other by tests; a name this returns null for must be one the server
 * stores, or the kit reports a run into silence.
 */
export function jobNameHasPII(name: string): string | null {
  if (NAME_CONTROL_CHAR_RE.test(name)) return "~control-characters";
  const existing = findDeniedValue(name);
  if (existing !== null) return existing;
  if (ROUTE_LABEL_UUID_RE.test(name)) return "~uuid";
  if (ROUTE_LABEL_NUMERIC_RE.test(name)) return "~numeric-id";
  return null;
}
