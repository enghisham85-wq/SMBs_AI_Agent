/** Outbound chokepoint — runs the PII guard before any network call.
 *
 * Mirrors `safe_transmit()` in the Python package and `safeTransmit()` in
 * the RN runtime. Always asserts no PII before transmitting; never
 * automatically wires itself to a remote endpoint.
 *
 * In addition to checking the JSON payload body, also checks caller-supplied
 * HTTP header values and URL query-parameter names/values so that sensitive
 * data cannot bypass the guard through those channels.
 */

import { assertNoPII, checkNoPII, PIIDetectedError } from "./no-pii";
import { isBoosthisDisabled } from "./runtimeFlags";
import { isRuntimeInert, _isRuntimeKilledInternal } from "./killSwitch";

export interface SafeTransmitOptions {
  url: string;
  payload: unknown;
  /** Extra HTTP headers forwarded to the remote endpoint. Header values are
   *  scanned for PII before transmission. */
  headers?: Record<string, string>;
  method?: "POST" | "PUT";
  fetchImpl?: typeof fetch;
  /** Abort the request after this many ms. Default 5000. Pass 0 to disable. */
  timeoutMs?: number;
  /** Caller-supplied abort signal (composed with the timeout). */
  signal?: AbortSignal;
  /** Let the request outlive the page (browser `fetch` keepalive). Required
   *  for anything sent while the page is going away — a normal fetch started
   *  in a `pagehide` handler is cancelled by the navigation and the data is
   *  simply lost. Bodies must stay small (browsers cap keepalive at 64 KB). */
  keepalive?: boolean;
}

/** Options for the internal POST helper used by the telemetry client. Like
 *  {@link SafeTransmitOptions} but without `url`/`payload` (passed positionally)
 *  and without `method` (always POST). */
export interface InternalPostOptions {
  headers?: Record<string, string>;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  signal?: AbortSignal;
  /** Let the request outlive the page — see {@link SafeTransmitOptions}. */
  keepalive?: boolean;
  /** @internal When true, the send is allowed while the kit is merely LOCKED
   *  (never activated) — used ONLY by the consent/registration path, which is
   *  the step that LEADS to activation and would otherwise deadlock every fresh
   *  install. A KILLED install (revoked / unpaid / grace-expired / env-kill) is
   *  still silenced even with this flag. Mirrors the RN runtime's
   *  `allowWhenLocked`. */
  allowWhenLocked?: boolean;
}

/** Scan caller-supplied header values for PII patterns.
 *
 * Header names are not checked against the JSON-payload denylist because HTTP
 * header names like "Content-Encoding" would produce false positives. Values
 * are checked using the same value-pattern scan applied to JSON string fields
 * (email, JWT, bearer, IPv4/v6, phone).
 */
function assertNoPIIInHeaders(
  headers: Record<string, string> | undefined,
): void {
  if (!headers) return;
  for (const [name, value] of Object.entries(headers)) {
    const hit = checkNoPII(value);
    if (hit !== null) {
      throw new PIIDetectedError(
        `$headers.${name}`,
        name,
        hit.matchedFragment,
      );
    }
  }
}

/** Scan URL query-parameter names and values for PII.
 *
 * Query-parameter names are developer-chosen keys and are checked the same way
 * as JSON field names (denylist + tokeniser). Values are checked with the
 * value-pattern scan (email, JWT, bearer, IPv4/v6, phone).
 *
 * A base-URL fallback is used so that relative paths (e.g. `/ingest?email=…`)
 * are always parsed and scanned — absolute URLs ignore the base entirely.
 * If the string is still unparseable the function fails closed: a URL we
 * cannot inspect cannot be certified PII-free.
 */
function assertNoPIIInUrl(url: string): void {
  let parsed: URL;
  try {
    // Fallback base ensures relative URLs are handled; absolute URLs ignore it.
    parsed = new URL(url, "https://boosthis.invalid");
  } catch {
    // Truly malformed URL string — fail closed.
    throw new PIIDetectedError("$url", "url", "~unparseable-url");
  }
  for (const [name, value] of parsed.searchParams) {
    // Check param name via the key-name guard (same as JSON field names).
    const nameHit = checkNoPII({ [name]: null });
    if (nameHit !== null) {
      throw new PIIDetectedError(
        `$url.query.${name}`,
        name,
        nameHit.matchedFragment,
      );
    }
    // Check param value via the value-pattern guard.
    const valueHit = checkNoPII(value);
    if (valueHit !== null) {
      throw new PIIDetectedError(
        `$url.query.${name}`,
        name,
        valueHit.matchedFragment,
      );
    }
  }
}

/** Shared network step: timeout + abort composition + the actual fetch.
 *  Callers must run the PII guard and the kill-switch BEFORE invoking this. */
async function doFetch(
  url: string,
  payload: unknown,
  method: "POST" | "PUT",
  headers: Record<string, string>,
  fetchImpl: typeof fetch | undefined,
  timeoutMs: number,
  signal: AbortSignal | undefined,
  keepalive: boolean | undefined,
): Promise<Response> {
  const f = fetchImpl ?? fetch;
  // Exit send (page is being torn down): deliberately NO abort signal and no
  // timeout. Verified in a real browser — a `keepalive` fetch that carries an
  // AbortSignal belonging to the dying document is cancelled by the browser
  // (net::ERR_ABORTED, nothing reaches the server), and a timeout is
  // meaningless once the page is gone. A caller-supplied signal still wins:
  // if the host asked to be able to cancel, honor that over delivery.
  if (keepalive && !signal) {
    return f(url, {
      method,
      headers: { "content-type": "application/json", ...headers },
      body: JSON.stringify(payload),
      keepalive: true,
    });
  }
  // Always attach an AbortController so a hung peer can't pin a request
  // forever. Compose with any caller-supplied signal so app-level cancel
  // semantics still apply.
  const ctrl = new AbortController();
  const timer =
    timeoutMs > 0
      ? setTimeout(
          () => ctrl.abort(new Error(`safeTransmit timeout after ${timeoutMs}ms`)),
          timeoutMs,
        )
      : null;
  if (timer && typeof (timer as { unref?: () => void }).unref === "function") {
    (timer as { unref: () => void }).unref();
  }
  if (signal) {
    if (signal.aborted) ctrl.abort(signal.reason);
    else
      signal.addEventListener("abort", () => ctrl.abort(signal.reason), {
        once: true,
      });
  }
  try {
    return await f(url, {
      method,
      headers: { "content-type": "application/json", ...headers },
      body: JSON.stringify(payload),
      signal: ctrl.signal,
      // Only set when asked: `keepalive` is unknown to some fetch polyfills
      // and non-browser runtimes, and an unknown init key must not appear in
      // the object they inspect.
      ...(keepalive ? { keepalive: true } : {}),
    });
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/* ── Answers the kit INVENTED because nothing was sent ────────────────────
 *
 * When the kill-switch or the activation lock silences an upload, these
 * helpers still have to return a `Response` — so they make one up. A 204 was
 * chosen because it carries no body, but a 204 is also `ok`, and every caller
 * that branches on `res.ok` therefore read "nothing was sent" as "the server
 * accepted it". For the registration POST that meant a page which never
 * spoke to Boosthis at all reported itself as REGISTERED.
 *
 * The status stays 204 (callers that only want "stop, quietly" are correct to
 * read it that way). What is added here is the ability to ASK: this answer was
 * manufactured locally, so it says nothing whatsoever about the server.
 *
 * Two channels, deliberately: the WeakSet is exact, and the status text is the
 * fallback for an answer that crossed a module boundary a bundler duplicated.
 */

/** Status text stamped on an answer produced without sending anything. */
export const NO_SEND_STATUS_TEXTS = [
  "Boosthis inert",
  "Boosthis disabled",
] as const;

const noSendAnswers = new WeakSet<object>();

/** Build the 204 that means "nothing left this page". */
function noSendAnswer(statusText: (typeof NO_SEND_STATUS_TEXTS)[number]): Response {
  const res = new Response(null, { status: 204, statusText });
  try {
    noSendAnswers.add(res as unknown as object);
  } catch {
    // A polyfilled Response that cannot be held weakly still carries the
    // status text below, so the caller can still tell.
  }
  return res;
}

/**
 * True when this answer was manufactured locally and NO request was made.
 *
 * Any caller that treats a response as evidence about the SERVER — a
 * registration confirmation above all — must consult this first. Never throws:
 * an exotic Response-like object simply reads as a real answer, which is the
 * conservative direction for every caller except a confirmation (and the
 * registration path additionally proves a send of its own).
 */
export function isNoSendResponse(res: unknown): boolean {
  try {
    if (!res || typeof res !== "object") return false;
    if (noSendAnswers.has(res as object)) return true;
    const statusText = (res as { statusText?: unknown }).statusText;
    if (typeof statusText !== "string") return false;
    return (NO_SEND_STATUS_TEXTS as readonly string[]).includes(statusText);
  } catch {
    return false;
  }
}

export async function safeTransmit(opts: SafeTransmitOptions): Promise<Response> {
  // PII guard runs unconditionally — the kill-switch silences outbound
  // traffic, it does NOT relax the privacy contract.
  assertNoPII(opts.payload);
  assertNoPIIInHeaders(opts.headers);
  assertNoPIIInUrl(opts.url);
  // NOTE: `safeTransmit` is the PUBLIC PII-guarded egress helper host apps may
  // use for their OWN traffic, so it is gated ONLY by the env kill-switch — the
  // server-authority entitlement gate applies to Boosthis's own telemetry
  // uploads (the `_safeTransmitInternal` path), not the developer's requests.
  if (isBoosthisDisabled()) {
    return noSendAnswer("Boosthis disabled");
  }
  return doFetch(
    opts.url,
    opts.payload,
    opts.method ?? "POST",
    opts.headers ?? {},
    opts.fetchImpl,
    opts.timeoutMs ?? 5000,
    opts.signal,
    opts.keepalive,
  );
}

/**
 * @internal
 *
 * Variant of {@link safeTransmit} for Boosthis-runtime internal use only.
 * Accepts a pre-validated `authHeader` value (e.g. `"Bearer <deleteToken>"`)
 * that is added to the outbound request WITHOUT passing through the PII guard.
 *
 * SECURITY RATIONALE: the auth credential supplied here is always a
 * Boosthis-server-issued token (invite key or delete token), never user data.
 * Placing it in `opts.headers` would cause the bearer/JWT value guard to block
 * it. A separate parameter keeps the bypass narrow, explicit, and invisible to
 * the public `SafeTransmitOptions` interface. NOT re-exported from index.ts.
 */
export async function _safeTransmitInternal(
  url: string,
  payload: unknown,
  opts: InternalPostOptions = {},
  authHeader?: string,
  internalHeaders?: Record<string, string>,
): Promise<Response> {
  // All caller-controlled inputs still go through the full PII guard.
  assertNoPII(payload);
  assertNoPIIInHeaders(opts.headers);
  assertNoPIIInUrl(url);
  // Kill-switch gate. The consent/registration path passes `allowWhenLocked`
  // (it must run while merely LOCKED so a fresh install can activate); every
  // other internal send is fully gated by `isRuntimeInert()`. A KILLED install
  // stays silenced in both cases.
  if (
    isBoosthisDisabled() ||
    (opts.allowWhenLocked ? _isRuntimeKilledInternal() : isRuntimeInert())
  ) {
    return noSendAnswer("Boosthis inert");
  }
  // `internalHeaders` are Boosthis-CONTROLLED headers applied AFTER the PII
  // guard — e.g. the read-token-backfill `X-Boosthis-Install-Token`, whose name
  // contains "token" and whose value is a secret credential. They are never
  // caller-supplied user data, so they legitimately bypass the header scan
  // exactly like the `authHeader`. (Mirrors the RN runtime's 5th param.)
  const headers: Record<string, string> = {
    ...(opts.headers ?? {}),
    ...(authHeader ? { authorization: authHeader } : {}),
    ...(internalHeaders ?? {}),
  };
  return doFetch(
    url,
    payload,
    "POST",
    headers,
    opts.fetchImpl,
    opts.timeoutMs ?? 5000,
    opts.signal,
    opts.keepalive,
  );
}

/**
 * @internal
 *
 * POST a Boosthis-CONTROLLED payload whose field NAMES legitimately match the
 * PII denylist (e.g. `/installs/forget` carries `deleteToken`, and "token" is a
 * denylisted name). The payload body therefore skips `assertNoPII`, but every
 * CALLER-controlled channel — URL and request headers — is still fully scanned,
 * and the kill-switch is still honored.
 *
 * SECURITY RATIONALE: the bypass is scoped to exactly one thing — the fixed,
 * Boosthis-built payload object — and never to caller-supplied headers. Callers
 * MUST pass only payloads they construct themselves from non-user data. NOT
 * re-exported from index.ts.
 */
export async function _safeTransmitTrustedPayload(
  url: string,
  payload: unknown,
  opts: InternalPostOptions = {},
): Promise<Response> {
  // Caller-controlled inputs are still guarded; only the fixed payload is not.
  assertNoPIIInHeaders(opts.headers);
  assertNoPIIInUrl(url);
  // NOTE: deliberately NOT gated by `isRuntimeInert()`. This helper carries the
  // right-to-erasure `/installs/forget` payload, which must still reach the
  // server for a REVOKED / unpaid install (the owner/user can always erase, and
  // erasure re-locks the kit). Only the env kill-switch silences the network
  // here (local state is cleared regardless by the caller).
  if (isBoosthisDisabled()) {
    return noSendAnswer("Boosthis disabled");
  }
  return doFetch(
    url,
    payload,
    "POST",
    opts.headers ?? {},
    opts.fetchImpl,
    opts.timeoutMs ?? 5000,
    opts.signal,
    opts.keepalive,
  );
}
