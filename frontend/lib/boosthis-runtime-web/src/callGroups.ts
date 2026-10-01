/** Safe grouping labels for the app's own outbound calls.
 *
 * WHY THIS EXISTS
 * A large share of AI-built apps have no backend of their own: the browser
 * talks straight to a hosted database (Supabase, Firebase, …). For those apps
 * the browser kit is the only place we can stand, and a single "network" total
 * tells the developer nothing they can act on. This module turns a destination
 * into a group label so the Network axis can say WHICH part of the backend is
 * slow — without ever reading the address itself.
 *
 * PRIVACY CONTRACT (the whole point of this file)
 * Nothing derived here comes from the CONTENT of a URL. Every label is a
 * constant defined in the tables below — the destination is matched against a
 * closed host table and the first path segment is matched against a closed
 * per-provider service table; the matched INPUT is thrown away and OUR OWN
 * token is emitted. A host we do not recognise becomes `third-party`; anything
 * we cannot resolve safely becomes `unnamed`. There is no code path that copies
 * a byte of the address, a query string, a header or a payload into a label.
 *
 * Because both halves come from closed vocabularies, the composed key can be
 * validated by the server against the SAME vocabularies (mirrored in
 * artifacts/api-server/src/lib/pii.ts, locked by a parity test) — an open
 * regex would happily wave `api.example.com` through, a closed set cannot.
 *
 * Every key is additionally run through the kit's own transmit-label guard
 * before it is used, so the labels ride the existing allowlists rather than
 * inventing a second, weaker rule.
 */

import { transmitLabelHasPII } from "./no-pii";

/** Every destination token this kit can ever emit. Closed set: mirrored in
 *  the server's ingest guard, which rejects anything outside it. */
export const CALL_GROUP_DESTINATIONS = [
  // The app's own origin (an app WITH a backend, or its own static assets).
  "same-origin",
  // Supabase — the default backend of Lovable/Bolt-style generated apps. The
  // service half comes from Supabase's OWN fixed API surface names, never from
  // a customer table or path.
  "supabase",
  "supabase-rest",
  "supabase-auth",
  "supabase-storage",
  "supabase-functions",
  "supabase-realtime",
  // Firebase — each product has its own Google-owned hostname, so the service
  // is read off the host, never off the path.
  "firebase-firestore",
  "firebase-auth",
  "firebase-storage",
  "firebase-database",
  "firebase-functions",
  // Other hosted backends / backend-as-a-service platforms.
  "appwrite",
  "pocketbase",
  "nhost",
  "hasura",
  "directus",
  "strapi",
  // Hosted databases with an HTTP data API.
  "neon",
  "planetscale",
  "turso",
  "xata",
  "upstash",
  "mongodb-atlas",
  // Hosted services these apps routinely call straight from the browser.
  "clerk",
  "auth0",
  "stripe",
  "algolia",
  "sanity",
  "contentful",
  "cloudinary",
  "openai",
  "anthropic",
  // Serverless edges an app's own functions are commonly deployed behind.
  "aws-api-gateway",
  "aws-s3",
  "azure-functions",
  "netlify-functions",
  "vercel-functions",
  "cloudflare-workers",
  // A cross-origin host we do not recognise. Deliberately NOT the hostname.
  "third-party",
  // The safe form could not be produced at all (unparseable input, a
  // non-HTTP scheme, a blob/data URL). Never a guess.
  "unnamed",
] as const;

export type CallGroupDestination = (typeof CALL_GROUP_DESTINATIONS)[number];

/** Every method token this kit can ever emit. Closed set; anything else — an
 *  unknown verb, a WebDAV method, a malformed string — becomes `other`. */
export const CALL_GROUP_METHODS = [
  "get",
  "post",
  "put",
  "patch",
  "delete",
  "head",
  "options",
  "other",
] as const;

export type CallGroupMethod = (typeof CALL_GROUP_METHODS)[number];

/** The visible "everything else" row: the groups that fell outside the cap.
 *  Reserved — it is a whole key, not a destination.method pair. */
export const CALL_GROUP_OVERFLOW_KEY = "everything-else";

/** The bucket for a destination we could not resolve safely. */
export const CALL_GROUP_UNNAMED_KEY = "unnamed.other";

/** How many distinct groups a project may report. Everything after this folds
 *  into the one visible "everything else" row — never dropped silently. Lives
 *  beside the vocabulary because the server mirrors both together. */
export const MAX_TRACKED_GROUPS = 8;

const DESTINATION_SET: ReadonlySet<string> = new Set(CALL_GROUP_DESTINATIONS);
const METHOD_SET: ReadonlySet<string> = new Set(CALL_GROUP_METHODS);

interface ProviderRule {
  /** Host suffixes. Matched as whole labels (`h === s || h.endsWith("." + s)`)
   *  so `notsupabase.co` can never match `supabase.co`. */
  readonly hosts: readonly string[];
  /** The token emitted when only the host matched. */
  readonly token: CallGroupDestination;
  /** Optional refinement by the provider's OWN first path segment. The segment
   *  is used as a LOOKUP KEY only; an unlisted segment falls back to `token`,
   *  so a customer table or route name can never become a label. */
  readonly services?: Readonly<Record<string, CallGroupDestination>>;
}

const PROVIDER_RULES: readonly ProviderRule[] = [
  {
    hosts: ["supabase.co", "supabase.in", "supabase.net"],
    token: "supabase",
    services: {
      rest: "supabase-rest",
      graphql: "supabase-rest",
      auth: "supabase-auth",
      storage: "supabase-storage",
      functions: "supabase-functions",
      realtime: "supabase-realtime",
    },
  },
  { hosts: ["firestore.googleapis.com"], token: "firebase-firestore" },
  {
    hosts: ["identitytoolkit.googleapis.com", "securetoken.googleapis.com"],
    token: "firebase-auth",
  },
  {
    hosts: ["firebasestorage.googleapis.com", "firebasestorage.app"],
    token: "firebase-storage",
  },
  {
    hosts: ["firebaseio.com", "firebasedatabase.app"],
    token: "firebase-database",
  },
  { hosts: ["cloudfunctions.net"], token: "firebase-functions" },
  { hosts: ["appwrite.io", "appwrite.network"], token: "appwrite" },
  { hosts: ["pockethost.io"], token: "pocketbase" },
  { hosts: ["nhost.run", "nhost.io"], token: "nhost" },
  { hosts: ["hasura.app", "hasura.io"], token: "hasura" },
  { hosts: ["directus.app"], token: "directus" },
  { hosts: ["strapiapp.com"], token: "strapi" },
  { hosts: ["neon.tech"], token: "neon" },
  { hosts: ["psdb.cloud", "planetscale.com"], token: "planetscale" },
  { hosts: ["turso.io"], token: "turso" },
  { hosts: ["xata.sh", "xata.io"], token: "xata" },
  { hosts: ["upstash.io"], token: "upstash" },
  {
    hosts: ["mongodb-api.com", "mongodb.net"],
    token: "mongodb-atlas",
  },
  {
    hosts: ["clerk.accounts.dev", "clerk.com", "clerk.dev"],
    token: "clerk",
  },
  { hosts: ["auth0.com"], token: "auth0" },
  { hosts: ["stripe.com"], token: "stripe" },
  { hosts: ["algolia.net", "algolianet.com"], token: "algolia" },
  { hosts: ["sanity.io"], token: "sanity" },
  { hosts: ["contentful.com"], token: "contentful" },
  { hosts: ["cloudinary.com"], token: "cloudinary" },
  { hosts: ["openai.com"], token: "openai" },
  { hosts: ["anthropic.com"], token: "anthropic" },
  { hosts: ["execute-api.amazonaws.com"], token: "aws-api-gateway" },
  { hosts: ["s3.amazonaws.com"], token: "aws-s3" },
  { hosts: ["azurewebsites.net"], token: "azure-functions" },
  { hosts: ["netlify.app"], token: "netlify-functions" },
  { hosts: ["vercel.app"], token: "vercel-functions" },
  { hosts: ["workers.dev"], token: "cloudflare-workers" },
];

/** Lowercase the method and pin it to the closed set. Anything unexpected —
 *  including a non-string — becomes `other` rather than riding the wire. */
export function callGroupMethod(rawMethod: unknown): CallGroupMethod {
  if (typeof rawMethod !== "string") return "other";
  const m = rawMethod.trim().toLowerCase();
  return METHOD_SET.has(m) ? (m as CallGroupMethod) : "other";
}

/** The href behind whatever fetch was handed: a string, a URL, or a Request.
 *  Returns null for anything else — we never guess. */
function hrefOf(rawUrl: unknown): string | null {
  if (typeof rawUrl === "string") return rawUrl;
  if (rawUrl !== null && typeof rawUrl === "object") {
    const candidate = rawUrl as { url?: unknown; href?: unknown };
    if (typeof candidate.url === "string") return candidate.url;
    if (typeof candidate.href === "string") return candidate.href;
    try {
      const s = String(rawUrl);
      return s.length > 0 && s !== "[object Object]" ? s : null;
    } catch {
      return null;
    }
  }
  return null;
}

/** First path segment, lowercased, ONLY when it is plain letters. Used purely
 *  as a lookup key into a provider's closed service table — never emitted. */
function firstSegment(pathname: string): string | null {
  const parts = pathname.split("/");
  for (const p of parts) {
    if (p.length === 0) continue;
    const lower = p.toLowerCase();
    return /^[a-z]+$/.test(lower) ? lower : null;
  }
  return null;
}

/** The page's own address, read through `globalThis` rather than the bare
 *  `location` global: this source is also typechecked by the server build,
 *  where DOM globals do not exist. Only two fields are ever read — one to
 *  resolve a relative address, one to recognise our own origin — and neither
 *  is ever copied into a label. */
function pageLocation(): { href?: string; origin?: string } | undefined {
  try {
    return (globalThis as { location?: { href?: string; origin?: string } })
      .location;
  } catch {
    return undefined;
  }
}

/** Resolve a destination token. Never throws; every unknown resolves to a
 *  constant, never to anything read off the address. */
export function callGroupDestination(rawUrl: unknown): CallGroupDestination {
  try {
    const href = hrefOf(rawUrl);
    if (href === null) return "unnamed";
    const loc = pageLocation();
    const base =
      loc && typeof loc.href === "string" && loc.href ? loc.href : undefined;
    let u: URL;
    try {
      u = base ? new URL(href, base) : new URL(href);
    } catch {
      return "unnamed";
    }
    if (u.protocol !== "http:" && u.protocol !== "https:") return "unnamed";
    if (
      loc &&
      typeof loc.origin === "string" &&
      loc.origin.length > 0 &&
      u.origin === loc.origin
    ) {
      return "same-origin";
    }
    const host = u.hostname.toLowerCase();
    for (const rule of PROVIDER_RULES) {
      const hit = rule.hosts.some(
        (s) => host === s || host.endsWith(`.${s}`),
      );
      if (!hit) continue;
      if (rule.services) {
        const seg = firstSegment(u.pathname);
        const svc = seg === null ? undefined : rule.services[seg];
        if (svc !== undefined) return svc;
      }
      return rule.token;
    }
    return "third-party";
  } catch {
    return "unnamed";
  }
}

/** True when `key` is a shape this kit could legitimately have produced AND it
 *  passes the kit's own transmit-label guard. The server runs the identical
 *  test against its mirrored vocabulary before storing anything. */
export function isSafeCallGroupKey(key: unknown): boolean {
  if (typeof key !== "string" || key.length === 0 || key.length > 48) {
    return false;
  }
  if (transmitLabelHasPII(key) !== null) return false;
  if (key === CALL_GROUP_OVERFLOW_KEY) return true;
  const dot = key.indexOf(".");
  if (dot <= 0 || dot !== key.lastIndexOf(".")) return false;
  return (
    DESTINATION_SET.has(key.slice(0, dot)) && METHOD_SET.has(key.slice(dot + 1))
  );
}

/** The group label for one outbound call: `<destination>.<method>`.
 *
 *  Both halves are constants from the tables above, so no part of the caller's
 *  URL survives into the return value. The composed key is still pushed
 *  through the kit's transmit-label guard — if a future edit to the tables ever
 *  produced something the guard dislikes, this falls back to the unnamed
 *  bucket rather than letting a new token ride the wire unchecked. */
export function deriveCallGroup(rawUrl: unknown, rawMethod: unknown): string {
  let key: string;
  try {
    key = `${callGroupDestination(rawUrl)}.${callGroupMethod(rawMethod)}`;
  } catch {
    return CALL_GROUP_UNNAMED_KEY;
  }
  return isSafeCallGroupKey(key) ? key : CALL_GROUP_UNNAMED_KEY;
}
