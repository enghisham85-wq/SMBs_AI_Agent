/** Active-span scope — the half of full-stack causality that answers "which
 *  call is open right NOW?".
 *
 *  The trace primitives in trace.ts correlate hops: everything that carries the
 *  same 32-hex trace id belongs to one user action. What they cannot say is
 *  which hop CAUSED which — a trace assembled from a shared id and a set of
 *  start offsets is a timeline, not a tree. These two ids are the difference:
 *
 *    • `spanId`       — this call's own identity, minted here.
 *    • `parentSpanId` — the identity of the call that caused it, taken from the
 *                       scope open at the call site, or adopted from the
 *                       `x-boosthis-trace-parent` header on the way in.
 *
 *  ADOPT-OR-DROP, never adopt-or-mint. `sanitizeTraceId` mints a fresh id when
 *  it is handed a bad one, because a trace with a re-minted id is still a valid
 *  trace. A re-minted PARENT would be a fabricated cause: it would point at a
 *  call that does not exist, and the reader has no way to tell that from a real
 *  one. So a malformed or missing parent becomes null and the span is simply
 *  top-level. Boosthis never guesses causality.
 *
 *  CONCURRENCY HONESTY. The ambient scope is SYNCHRONOUS only: `runInSpan`
 *  closes the scope the moment its callback returns, so an `await` inside the
 *  callback does not hold it open. JavaScript runs one stack at a time, so a
 *  synchronous scope can never attribute two calls that are genuinely in
 *  flight together to each other. Holding a scope across an await would look
 *  richer and be wrong — two parallel fetches would come back as parent and
 *  child. A wrong parent is worse than no parent, because a flat trace is
 *  visibly flat while a wrong tree reads as the truth.
 *
 *  PRIVACY CONTRACT (verified against no-pii.ts): a span id is exactly 16
 *  lowercase hex characters (64 random bits). Pure hex cannot match the guard's
 *  email/JWT/bearer/IP/phone value patterns, and the keys `spanId` /
 *  `parentSpanId` / the `x-boosthis-trace-parent` header tokenize to words that
 *  are not denied. It is random, ephemeral, and never derived from user data.
 *
 *  ⚠️  Like a trace id, a span id must NEVER appear in a route/screen label.
 *
 *  This module is intentionally PURE — no DOM imports — so any caller can
 *  import it freely.
 */

/** Companion header carrying the CALLER's span id to the next hop. Byte-
 *  identical across every runtime that carries parentage. */
export const PARENT_HEADER = "x-boosthis-trace-parent";

/** A valid span id is exactly 16 lowercase hex characters (64 bits). Half the
 *  width of a trace id on purpose: a span id only has to be unique inside one
 *  trace, and a shorter id keeps the per-span wire cost down when a batch
 *  carries 50 of them. */
export const SPAN_ID_RE = /^[0-9a-f]{16}$/;

/** Ceiling on how deep the ambient scope may nest. A caller that forgets to
 *  close a scope (only possible by misusing the internals — `runInSpan` always
 *  closes its own) can then never grow this list without bound. Past the
 *  ceiling the scope simply stops deepening; nothing throws and no span is
 *  lost, it just reports the outer parent. */
export const MAX_ACTIVE_SPANS = 32;

/** True if `id` matches the locked span-id format exactly. */
export function isValidSpanId(id: unknown): id is string {
  return typeof id === "string" && SPAN_ID_RE.test(id);
}

/** Mint a fresh 16-hex-char span id.
 *
 *  Uses the platform CSPRNG when available, falling back to `Math.random`. As
 *  with the trace id, the fallback is acceptable because this is a CORRELATION
 *  TAG, not an authenticator — weaker randomness only risks a collision inside
 *  a single trace, and a collision is REPORTED (as an ambiguous parent) rather
 *  than silently resolved. */
export function newSpanId(): string {
  const g = globalThis as unknown as {
    crypto?: { getRandomValues?: (a: Uint8Array) => Uint8Array };
  };
  const getRandomValues = g.crypto?.getRandomValues;
  if (typeof getRandomValues === "function") {
    try {
      const bytes = new Uint8Array(8);
      getRandomValues.call(g.crypto, bytes);
      let out = "";
      for (let i = 0; i < bytes.length; i++) {
        out += bytes[i]!.toString(16).padStart(2, "0");
      }
      return out;
    } catch {
      // fall through to Math.random below
    }
  }
  let out = "";
  for (let i = 0; i < 16; i++) {
    out += Math.floor(Math.random() * 16).toString(16);
  }
  return out;
}

/** Sanitize-on-receive for a span identity: return it unchanged if it is
 *  exactly the locked shape, otherwise NULL. Deliberately not adopt-or-mint —
 *  see the module header. Anything not matching `^[0-9a-f]{16}$` (including a
 *  CRLF header-injection payload) is discarded. */
export function sanitizeSpanId(id: unknown): string | null {
  return isValidSpanId(id) ? id : null;
}

/** Normalize a raw incoming header value (string | string[] | undefined) to the
 *  first string, then adopt-or-drop. Never throws. */
export function adoptParentSpanId(headerValue: unknown): string | null {
  try {
    const raw = Array.isArray(headerValue) ? headerValue[0] : headerValue;
    return sanitizeSpanId(raw);
  } catch {
    return null;
  }
}

/** One open call: its own identity plus the identity of whatever caused it.
 *
 *  `traceId` is the ACTION the call belongs to. It is optional because a span
 *  handle is often minted for a leg of work whose trace is resolved elsewhere.
 *  When it IS set, the handle can answer "which action is open right now?" —
 *  the question a crash asks. */
export interface SpanHandle {
  readonly spanId: string;
  readonly parentSpanId: string | null;
  readonly traceId?: string | null;
}

/** A 32-hex trace id, checked here rather than imported: trace.ts imports THIS
 *  module, so reaching back the other way would close a cycle. */
const TRACE_ID_SHAPE = /^[0-9a-f]{32}$/;

/** The action a call belongs to: the trace, and the leg of it that is open. */
export interface ActionContext {
  readonly traceId: string;
  readonly spanId: string;
}

/** The ambient stack of calls open at this synchronous moment. */
const active: { spanId: string; traceId: string | null }[] = [];

/** The innermost span open right now, or null when nothing is. */
export function currentSpanId(): string | null {
  return active.length === 0 ? null : active[active.length - 1]!.spanId;
}

/** The innermost open call that knows which ACTION it belongs to, or null.
 *
 *  Null is a real answer and the common one: the scope here is synchronous by
 *  design (see the module header), so work resumed after an `await` runs
 *  outside every scope. Naming an action then would mean guessing, and a crash
 *  filed against the wrong action reads exactly like the truth. Absence is
 *  reported as "no action recorded", never as "happened outside any action". */
export function currentAction(): ActionContext | null {
  for (let i = active.length - 1; i >= 0; i--) {
    const frame = active[i]!;
    if (frame.traceId !== null) {
      return { traceId: frame.traceId, spanId: frame.spanId };
    }
  }
  return null;
}

/** Mint an identity for a call that is about to happen, and resolve its caller.
 *
 *  Pass `parentSpanId` explicitly to name a caller from outside the ambient
 *  scope (for instance one adopted from an inbound header); pass `null` to say
 *  "this is a root, it has no caller". Omit it and the caller is whatever scope
 *  is open at this exact moment — which is null in the ordinary case of a fetch
 *  fired straight from a screen.
 *
 *  Beginning a span does NOT make it ambient. Wrap the work in `runInSpan` for
 *  that, so the scope's lifetime is written down at the call site instead of
 *  depending on when somebody remembers to close it. */
export function beginSpan(opts?: {
  parentSpanId?: string | null;
  traceId?: string | null;
}): SpanHandle {
  const explicit = opts && "parentSpanId" in opts;
  const traceId = opts?.traceId;
  return {
    spanId: newSpanId(),
    parentSpanId: explicit
      ? sanitizeSpanId(opts!.parentSpanId)
      : currentSpanId(),
    traceId:
      typeof traceId === "string" && TRACE_ID_SHAPE.test(traceId)
        ? traceId
        : null,
  };
}

/** Run `fn` with `span` as the ambient current span, so anything that begins a
 *  span inside the callback names it as its caller.
 *
 *  SYNCHRONOUS scope by design (see the module header): the scope closes when
 *  `fn` returns, so work resumed after an `await` inside `fn` runs outside it.
 *  The scope is always closed, including when `fn` throws. */
export function runInSpan<T>(span: SpanHandle, fn: () => T): T {
  const pushed = active.length < MAX_ACTIVE_SPANS;
  if (pushed) {
    active.push({
      spanId: span.spanId,
      traceId:
        typeof span.traceId === "string" && TRACE_ID_SHAPE.test(span.traceId)
          ? span.traceId
          : null,
    });
  }
  try {
    return fn();
  } finally {
    if (pushed) {
      // Remove by identity rather than popping: a caller who nests scopes
      // incorrectly then loses only their own frame instead of somebody else's.
      let at = -1;
      for (let i = active.length - 1; i >= 0; i--) {
        if (active[i]!.spanId === span.spanId) {
          at = i;
          break;
        }
      }
      if (at !== -1) active.splice(at, 1);
    }
  }
}

/** Test-only handles. */
export const _spanScopeInternals = {
  activeCount: () => active.length,
  reset: () => {
    active.length = 0;
  },
};
