/** Full-stack trace tag — the correlation id that stitches one user action
 * across all four Boosthis runtimes (Web / RN → Node → Python).
 *
 * This is the PROPAGATION + PROOF primitive: it does not store or upload
 * anything — it only mints/validates/forwards a privacy-safe id so a single
 * "click → API → worker" round-trip can be correlated later. The web layer is
 * a trace ROOT exactly like RN: a browser action initiates the trace, mints the
 * id, and forwards it to the Node/Python hops.
 *
 * PRIVACY CONTRACT (verified against no-pii.ts):
 *  - The id is exactly 32 lowercase hex chars (128 random bits). Pure hex
 *    cannot match the PII guard's email/JWT/bearer/IPv4/IPv6/phone value
 *    patterns in the general walk(), and the UUID / long-numeric checks apply
 *    ONLY to route labels — never to a general field. The field key `traceId`
 *    tokenizes to ["trace","id"] (neither denied), so `{ traceId }` and the
 *    `x-boosthis-trace` header are guard-clean.
 *  - It is random, ephemeral, and NEVER derived from user data.
 *
 * ⚠️  A trace id must NEVER be used as (or embedded in) a route/screen label.
 *     `routeLabelHasPII` rejects long-numeric runs, so a digit-edged trace id
 *     would be flagged as a URL identifier. Keep trace ids in their own
 *     header/field only.
 *
 * This module is intentionally PURE — no DOM/vitals imports — so the Node-side
 * MCP entrypoint (and any non-UI caller) can import it freely.
 */

/** Canonical lowercase header name carried on every instrumented request. */
export const TRACE_HEADER = "x-boosthis-trace";

/** A valid trace id is exactly 32 lowercase hex characters (128 bits). */
export const TRACE_ID_RE = /^[0-9a-f]{32}$/;

/** True if `id` matches the locked trace-id format exactly. */
export function isValidTraceId(id: unknown): id is string {
  return typeof id === "string" && TRACE_ID_RE.test(id);
}

/** Mint a fresh 32-hex-char trace id.
 *
 * Uses the platform CSPRNG (`globalThis.crypto.getRandomValues`, always present
 * in a real browser) when available, falling back to `Math.random`. The
 * fallback is acceptable here because the id is a CORRELATION TAG, not an
 * authenticator — it never gates access or proves identity, so
 * non-cryptographic randomness only risks a (vanishingly rare) collision
 * between two concurrent traces, never a security bypass. */
export function newTraceId(): string {
  const g = globalThis as unknown as {
    crypto?: { getRandomValues?: (a: Uint8Array) => Uint8Array };
  };
  const getRandomValues = g.crypto?.getRandomValues;
  if (typeof getRandomValues === "function") {
    try {
      const bytes = new Uint8Array(16);
      getRandomValues.call(g.crypto, bytes);
      let out = "";
      for (let i = 0; i < bytes.length; i++) {
        out += bytes[i].toString(16).padStart(2, "0");
      }
      return out;
    } catch {
      // fall through to Math.random below
    }
  }
  let out = "";
  for (let i = 0; i < 32; i++) {
    out += Math.floor(Math.random() * 16).toString(16);
  }
  return out;
}

/** Adopt-or-mint: return `id` unchanged if it is a valid trace id, otherwise
 * mint a fresh one. This is the sanitize-on-receive trust boundary — a caller
 * can never smuggle PII (or a CRLF header-injection payload) through the trace
 * header, because anything not matching `^[0-9a-f]{32}$` is discarded. */
export function sanitizeTraceId(id: unknown): string {
  return isValidTraceId(id) ? id : newTraceId();
}

/** Build the outbound header map for a request. Pass an existing id to
 * propagate it (validated first), or omit to mint a fresh trace. */
export function traceHeaders(id?: string): Record<string, string> {
  return { [TRACE_HEADER]: sanitizeTraceId(id) };
}

/* ─── Elapsed chain (waterfall offsets) ──────────────────────────────────────
 * A companion header carrying a RELATIVE elapsed-ms integer: how long after the
 * trace root STARTED this hop was reached, measured by chaining each hop's own
 * monotonic delta. This is what lets the server place each span in the
 * waterfall WITHOUT any absolute client timestamp and WITHOUT comparing clocks
 * across machines. A bounded non-negative integer carries no PII, so the
 * sanitize-on-receive rule is simply "parse a small int, else 0" — and, unlike
 * the id, a malformed elapsed only degrades the OFFSET (→ 0); it never re-mints
 * or drops the trace correlation. Byte-identical across all runtimes.
 */

/** Companion header carrying the relative elapsed-ms offset for the next hop. */
export const ELAPSED_HEADER = "x-boosthis-trace-elapsed";

/** A propagated elapsed value is 1–6 digits (0..999999); clamped to
 * MAX_TRACE_ELAPSED_MS on receive. Byte-identical across all runtimes. */
export const TRACE_ELAPSED_RE = /^\d{1,6}$/;

/** Upper clamp for a propagated elapsed value (mirrors the span duration bound
 * MAX_SPAN_DURATION_MS). */
export const MAX_TRACE_ELAPSED_MS = 600000;

/** Sanitize-on-receive for the elapsed chain: parse a bounded non-negative
 * integer from the incoming header (string | string[] | undefined), or 0 if it
 * is missing/malformed. Never throws; a bad value degrades the offset only. */
export function sanitizeTraceElapsed(value: unknown): number {
  const raw = Array.isArray(value) ? value[0] : value;
  if (typeof raw === "string" && TRACE_ELAPSED_RE.test(raw)) {
    return Math.min(MAX_TRACE_ELAPSED_MS, parseInt(raw, 10));
  }
  return 0;
}
