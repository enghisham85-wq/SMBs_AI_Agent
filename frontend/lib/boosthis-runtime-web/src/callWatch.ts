/** Watch the app's outbound calls where they START.
 *
 * The browser kit used to learn about a call only when Resource Timing said it
 * had finished. That is fine for latency and useless for the failure a
 * front-end-only app actually suffers: a call to a hosted database that hangs
 * and never comes back. It produces no resource entry, so it produced no
 * reading — the kit's stall and timeout counts were hard-coded to zero.
 *
 * This module wraps `fetch` and `XMLHttpRequest` so a call is registered the
 * moment it is made and reconciled when (or if) it ends. That makes four
 * things visible that were not: a hang, a timeout, an abort, and a transport
 * failure — each told apart from the others.
 *
 * GUEST-SAFETY (this code runs inside someone else's app)
 * - Every wrapper is a thin pass-through. The original is called with the
 *   original arguments and its return value is handed back untouched.
 * - Every observation is inside its own try/catch. If anything here throws,
 *   the host's call still happens and still resolves — the reading is what is
 *   lost, never the app's behaviour.
 * - The promise the app receives is not delayed by us. The opt-in body check
 *   runs on a CLONE, after the app already has its response.
 * - Installation is reversible, and refuses to double-wrap.
 *
 * PRIVACY
 * The wrapper sees the URL because it has to — it is the app's own call. It
 * turns that URL into a group token from a closed vocabulary (callGroups.ts)
 * and forgets it. No address, path, query, header or payload is stored, and
 * none can reach the sampler: `noteCallStart` accepts a group key and nothing
 * else, and rejects any key outside the vocabulary.
 */

import { deriveCallGroup, CALL_GROUP_UNNAMED_KEY } from "./callGroups";
import {
  noteRouteOutcome as noteRouteFailureOutcome,
  noteRouteOutcomeByLabel,
  noteRouteBodyFailure,
} from "./routeOutcomes";
import { spanLabel } from "./spanEmitter";
import {
  noteBodyFailure,
  noteCallSettled,
  noteCallStart,
  setCallWatchActive,
  type CallHandle,
  type CallOutcome,
} from "./networkSampler";
import {
  clearEdgeCache,
  isOwnEndpointReply,
  noteEdgeCacheReply,
  isOwnHostingReply,
  pageOriginOf,
} from "./edgeCache";
import {
  aiFetchFailed,
  aiFetchSettled,
  aiFetchStarted,
  aiXhrFailed,
  aiXhrSettled,
  aiXhrStarted,
  armAiCalls,
  clearAiCalls,
  isAiProviderUrl,
  noteAiShapedXhr,
  setWatchedTransports,
  unarmAiCalls,
} from "./aiCalls";

/** Largest JSON response the opt-in body-error check will read. A browser kit
 *  must never buffer an app's payloads; a body without a declared length, or a
 *  longer one, is left alone. */
export const MAX_BODY_ERROR_BYTES = 64 * 1024;

interface AnyRecord {
  [k: string]: unknown;
}

type FetchLike = (input: unknown, init?: unknown) => Promise<unknown>;

let installed = false;
let bodyErrorsEnabled = false;
let originalFetch: FetchLike | null = null;
let wrappedFetch: FetchLike | null = null;
let originalXhrOpen: ((...args: unknown[]) => unknown) | null = null;
let originalXhrSend: ((...args: unknown[]) => unknown) | null = null;
let wrappedXhrOpen: ((...args: unknown[]) => unknown) | null = null;
let wrappedXhrSend: ((...args: unknown[]) => unknown) | null = null;
let wrappedXhrProto: AnyRecord | null = null;
/** The object we actually wrapped, so teardown puts the originals back on the
 *  SAME object even if the caller passed a scope of its own. */
let wrappedScope: AnyRecord | null = null;

/** Private slot on an XHR instance holding the group derived at open() time. */
const XHR_GROUP = "__boosthisCallGroup";
/** Private slot holding the normalised route label built at open() time, where
 *  the method and the address are both in hand. Local to the page and never
 *  uploaded — only the counts it keys reach the wire (see routeOutcomes.ts). */
const XHR_ROUTE = "__boosthisRouteLabel";
/** Which send() the listeners now attached belong to. An XHR instance can be
 *  sent more than once, and its old terminal listeners stay attached. */
const XHR_SEND = "__boosthisSendToken";
/** Marked at open() time: this request is one of OUR sends, so its reply
 *  must not be read as the app's platform-cache verdict. */
const XHR_OWN = "__boosthisOwnEndpoint";
/** Marked at open() time: was this call addressed to the page's own site?
 *  Only those replies may carry a platform-cache verdict about this project. */
const XHR_OWN_HOSTING = "__boosthisOwnHosting";
/** Marked at open() time: the address, but ONLY when it is a known AI
 *  provider's. Everything else leaves no trace, and the AI-call meter reads
 *  the slot at send() time, where the body and the deadline are known. */
const XHR_AI_URL = "__boosthisAiUrl";
/** Marked at open() time, counted at send(): the method and address of a call
 *  that LOOKS like inference but matched no provider. The question can only be
 *  asked here, where both are in hand — but open() is not a request. A page
 *  may configure an XHR and never send it, and counting that would report AI
 *  traffic that never left. Nothing about it is ever sent: the slot is read to
 *  move a count by one and then cleared. */
const XHR_AI_SHAPE = "__boosthisAiShape";

/** Turn a rejection into an outcome. `AbortSignal.timeout()` rejects with a
 *  TimeoutError and a manual `controller.abort()` with an AbortError — the
 *  distinction is the whole reason these are separate buckets. */
export function outcomeForError(err: unknown): CallOutcome {
  try {
    const name =
      err !== null && typeof err === "object"
        ? String((err as AnyRecord).name ?? "")
        : "";
    if (name === "TimeoutError") return "timed-out";
    if (name === "AbortError") return "aborted";
    return "failed";
  } catch {
    return "failed";
  }
}

/** A response is "completed" unless the server said otherwise. An HTTP error
 *  status is a failure the developer can act on, exactly as the resource-timing
 *  path has always treated it. */
export function outcomeForResponse(res: unknown): CallOutcome {
  try {
    const status = (res as AnyRecord | null)?.status;
    return typeof status === "number" && status >= 400 ? "failed" : "completed";
  } catch {
    return "completed";
  }
}

/** Does this parsed JSON body carry an error the transport never reported?
 *  Two shapes only, both conventions rather than guesses: a truthy top-level
 *  `error` (PostgREST/Supabase, most REST wrappers) and a non-empty top-level
 *  `errors` array (GraphQL). Nothing from the body is read out or stored — the
 *  answer is one boolean. */
export function jsonLooksLikeError(parsed: unknown): boolean {
  try {
    if (parsed === null || typeof parsed !== "object") return false;
    if (Array.isArray(parsed)) return false;
    const o = parsed as AnyRecord;
    if (o.error !== undefined && o.error !== null && o.error !== false) {
      return true;
    }
    return Array.isArray(o.errors) && o.errors.length > 0;
  } catch {
    return false;
  }
}

/** Opt-in only: read a clone of a normal-looking response and, if it carries
 *  an error inside the payload, reclassify the call as failed.
 *
 *  Deliberately conservative. It runs only when the app turned it on, only for
 *  JSON, only when the server declared a length we are willing to buffer, and
 *  only on a clone — the app's own response object is never touched, never
 *  delayed and never consumed. */
function checkBodyForError(
  res: unknown,
  handle: CallHandle,
  input: unknown,
  method: unknown,
): void {
  try {
    const r = res as {
      ok?: boolean;
      clone?: () => { text?: () => Promise<string> };
      headers?: { get?: (n: string) => string | null };
      bodyUsed?: boolean;
    } | null;
    if (!r || typeof r.clone !== "function" || r.bodyUsed === true) return;
    const get = r.headers?.get;
    if (typeof get !== "function") return;
    const type = get.call(r.headers, "content-type") ?? "";
    if (!/json/i.test(type)) return;
    // A DECLARED length, or nothing happens. An absent header reads as `null`,
    // and `Number(null)` is 0 — finite, under the cap, and enough to let an
    // undeclared body of any size through the gate the published claim says
    // stops it. The header must be there, and it must be a real length.
    const header = get.call(r.headers, "content-length");
    if (typeof header !== "string" || header.trim() === "") return;
    const declared = Number(header);
    if (!Number.isFinite(declared) || declared <= 0) return;
    if (declared > MAX_BODY_ERROR_BYTES) return;
    const copy = r.clone();
    const text = copy?.text?.();
    if (!text || typeof text.then !== "function") return;
    void text.then(
      (body: string) => {
        try {
          // A second check, on what actually arrived. The gate above is the
          // server's own declared byte length; this one counts CHARACTERS of
          // the string the browser handed back, because a declaration can
          // understate the reply and the parse below must not run on a body
          // larger than the one we said we would read.
          if (typeof body !== "string" || body.length > MAX_BODY_ERROR_BYTES) {
            return;
          }
          if (jsonLooksLikeError(JSON.parse(body))) {
            // BOTH readings move, here, together. The aggregate one counts
            // the call; the route one counts the same answer against the
            // part that gave it. Split them and a page reports a failure in
            // one reading and "none of 1 answers failed" in the other.
            noteBodyFailure(handle);
            noteRouteBodyFailure(input, method);
          }
        } catch {
          // Not JSON after all, or the body vanished. Nothing to say.
        }
      },
      () => {
        // A clone that cannot be read tells us nothing. Never a failure.
      },
    );
  } catch {
    // never break the page
  }
}

/** File the hosting platform's own cache verdict for one reply, if it declared
 *  one. Only the fixed verdict header names are looked up; a reply that
 *  declares nothing (or whose headers the browser will not expose across
 *  origins) files nothing at all. The response object is not touched in any
 *  other way — no body, no clone, no consumption. */
function noteEdgeCacheFromResponse(res: unknown): void {
  try {
    // Our own upload replies carry our hosting's verdict, not the app's.
    const from = (res as { url?: unknown } | null)?.url;
    if (typeof from === "string" && isOwnEndpointReply(from)) return;
    // Someone else's cache verdict is not this project's hosting.
    if (!isOwnHostingReply(typeof from === "string" ? from : null, watchedOrigin)) {
      return;
    }
    const headers = (res as { headers?: { get?: unknown } } | null)?.headers;
    const get = headers?.get;
    if (typeof get !== "function") return;
    noteEdgeCacheReply((name) => {
      const v = (get as (n: string) => unknown).call(headers, name);
      return typeof v === "string" ? v : null;
    });
  } catch {
    // never break the page
  }
}

function methodOf(input: unknown, init: unknown): unknown {
  try {
    const fromInit = (init as AnyRecord | null)?.method;
    if (typeof fromInit === "string") return fromInit;
    const fromRequest = (input as AnyRecord | null)?.method;
    if (typeof fromRequest === "string") return fromRequest;
  } catch {
    // fall through
  }
  return "GET";
}

function wrapFetch(scope: AnyRecord): boolean {
  try {
    const original = scope.fetch;
    if (typeof original !== "function") return false;
    const call = original as FetchLike;
    const wrapper: FetchLike = function boosthisFetch(
      this: unknown,
      input: unknown,
      init?: unknown,
    ) {
      let handle: CallHandle | null = null;
      try {
        handle = noteCallStart(deriveCallGroup(input, methodOf(input, init)));
      } catch {
        handle = null;
      }
      // The AI-call meter rides the SAME wrapper: one classification by
      // hostname, and null for every call that is not a provider's — which is
      // every call in most apps.
      let ai: ReturnType<typeof aiFetchStarted> = null;
      try {
        ai = aiFetchStarted(input, init);
      } catch {
        ai = null;
      }
      let promise: Promise<unknown>;
      try {
        const ctx =
          this === undefined || this === null || this === wrapper ? scope : this;
        promise =
          init === undefined
            ? (call.call(ctx, input) as Promise<unknown>)
            : (call.call(ctx, input, init) as Promise<unknown>);
      } catch (err) {
        try {
          if (handle !== null) {
            const outcome = outcomeForError(err);
            noteCallSettled(handle, outcome);
            noteRouteFailureOutcome(input, methodOf(input, init), outcome);
          }
          aiFetchFailed(ai, err);
        } catch {
          // never break the page
        }
        throw err;
      }
      if (
        (handle === null && ai === null) ||
        !promise ||
        typeof promise.then !== "function"
      ) {
        return promise;
      }
      const observed = handle;
      const aiCall = ai;
      return promise.then(
        (res: unknown) => {
          try {
            if (observed !== null) {
              const outcome = outcomeForResponse(res);
              noteCallSettled(observed, outcome);
              // The SAME outcome, kept beside the route label built from this
              // same call. One decision, two uses — no second observation.
              noteRouteFailureOutcome(input, methodOf(input, init), outcome);
              noteEdgeCacheFromResponse(res);
              if (bodyErrorsEnabled && outcome === "completed") {
                checkBodyForError(res, observed, input, methodOf(input, init));
              }
            }
          } catch {
            // never break the page
          }
          try {
            aiFetchSettled(aiCall, res);
          } catch {
            // never break the page
          }
          return res;
        },
        (err: unknown) => {
          try {
            if (observed !== null) {
              const outcome = outcomeForError(err);
              noteCallSettled(observed, outcome);
              noteRouteFailureOutcome(input, methodOf(input, init), outcome);
            }
          } catch {
            // never break the page
          }
          try {
            aiFetchFailed(aiCall, err);
          } catch {
            // never break the page
          }
          throw err;
        },
      );
    };
    originalFetch = call;
    wrappedFetch = wrapper;
    scope.fetch = wrapper;
    // A scope that silently refuses the assignment (frozen global, locked-down
    // embedding) must not be reported as watched, or the resource-timing
    // fallback would be switched off for nothing.
    if (scope.fetch !== wrapper) {
      originalFetch = null;
      wrappedFetch = null;
      return false;
    }
    return true;
  } catch {
    originalFetch = null;
    wrappedFetch = null;
    return false;
  }
}

function wrapXhr(scope: AnyRecord): boolean {
  try {
    const ctor = scope.XMLHttpRequest as
      | { prototype?: AnyRecord }
      | undefined;
    const proto = ctor?.prototype;
    if (!proto) return false;
    const open = proto.open;
    const send = proto.send;
    if (typeof open !== "function" || typeof send !== "function") return false;
    const openFn = open as (...args: unknown[]) => unknown;
    const sendFn = send as (...args: unknown[]) => unknown;

    const openWrapper = function boosthisXhrOpen(
      this: AnyRecord,
      ...args: unknown[]
    ) {
      try {
        const target =
          typeof args[1] === "string" ? args[1] : String(args[1] ?? "");
        this[XHR_OWN] = isOwnEndpointReply(target);
        this[XHR_OWN_HOSTING] = isOwnHostingReply(target, watchedOrigin);
        this[XHR_AI_URL] = isAiProviderUrl(target) ? target : null;
        // An address the classifier does not recognise still gets the shape
        // question asked of it, with the method open() was given — but the
        // answer is parked until send(), because an XHR that is opened and
        // never sent made no call at all.
        this[XHR_AI_SHAPE] = this[XHR_AI_URL]
          ? null
          : { target, method: args[0] };
      } catch {
        // never break the page
      }
      try {
        // Built HERE, where the method and address are both in hand — an XHR
        // settles on an event long after this, with neither still in scope.
        // Stays on the instance; only the counts it keys ever leave.
        this[XHR_ROUTE] = spanLabel(
          typeof args[0] === "string" && args[0] ? args[0] : "GET",
          typeof args[1] === "string" ? args[1] : String(args[1] ?? ""),
        );
      } catch {
        try {
          this[XHR_ROUTE] = null;
        } catch {
          // never break the page
        }
      }
      try {
        this[XHR_GROUP] = deriveCallGroup(args[1], args[0]);
      } catch {
        try {
          this[XHR_GROUP] = CALL_GROUP_UNNAMED_KEY;
        } catch {
          // never break the page
        }
      }
      return openFn.apply(this, args);
    };

    const sendWrapper = function boosthisXhrSend(
      this: AnyRecord,
      ...args: unknown[]
    ) {
      let started: CallHandle | null = null;
      let settleThisSend: ((outcome: CallOutcome) => void) | null = null;
      try {
        // The call is happening NOW, so the shape question parked at open()
        // is answered now — once. Cleared either way: a reused instance is
        // re-opened before it can send again, and that open() parks afresh.
        const shaped = this[XHR_AI_SHAPE] as
          | { target: unknown; method: unknown }
          | null
          | undefined;
        this[XHR_AI_SHAPE] = null;
        if (shaped) noteAiShapedXhr(shaped.target, shaped.method);
      } catch {
        // never break the page
      }
      // ONE SETTLEMENT PER SEND, and never an earlier send's. Terminal
      // listeners live on the XHR INSTANCE, and an instance can be reused:
      // open()/send() a second time and the first send's `load` listener is
      // still attached, still holding the first send's route label. Without a
      // guard it would count the second call's status against the first
      // call's part. The sampler's own handle bookkeeping already refuses a
      // second settlement; the route counts need the same refusal, or a reused
      // instance inflates them without limit. Two checks, because they catch
      // different things: the flag stops two terminal events from ONE send
      // (`error` after a partial `load`), the token stops a listener left over
      // from a PREVIOUS send.
      const sendToken: Record<string, never> = {};
      try {
        this[XHR_SEND] = sendToken;
      } catch {
        // never break the page — without the token the flag still holds
      }
      try {
        const group =
          typeof this[XHR_GROUP] === "string"
            ? (this[XHR_GROUP] as string)
            : CALL_GROUP_UNNAMED_KEY;
        const handle = noteCallStart(group);
        started = handle;
        if (handle !== null) {
          const routeLabel =
            typeof this[XHR_ROUTE] === "string"
              ? (this[XHR_ROUTE] as string)
              : null;
          let settled = false;
          const settle = (outcome: CallOutcome) => {
            try {
              if (settled) return;
              if (this[XHR_SEND] !== sendToken) return;
              settled = true;
              noteCallSettled(handle, outcome);
              // Same outcome, against the label open() already built.
              noteRouteOutcomeByLabel(routeLabel, outcome);
            } catch {
              // never break the page
            }
          };
          settleThisSend = settle;
          const on = this.addEventListener;
          if (typeof on === "function") {
            const listen = on as (n: string, f: () => void) => void;
            listen.call(this, "load", () => {
              settle(
                typeof this.status === "number" && this.status >= 400
                  ? "failed"
                  : "completed",
              );
              try {
                const readHeader = this.getResponseHeader;
                if (
                  this[XHR_OWN] !== true &&
                  this[XHR_OWN_HOSTING] === true &&
                  typeof readHeader === "function"
                ) {
                  noteEdgeCacheReply((name) => {
                    const v = (readHeader as (n: string) => unknown).call(
                      this,
                      name,
                    );
                    return typeof v === "string" ? v : null;
                  });
                }
              } catch {
                // never break the page
              }
            });
            listen.call(this, "error", () => settle("failed"));
            listen.call(this, "abort", () => settle("aborted"));
            listen.call(this, "timeout", () => settle("timed-out"));
          }
        }
      } catch {
        // never break the page
      }
      // The AI-call meter, on the same send. Registered independently of the
      // sampler's own handle: a call the sampler declined to track is still an
      // AI call, and losing it would understate the app's model traffic.
      try {
        const aiUrl = this[XHR_AI_URL];
        if (typeof aiUrl === "string" && aiUrl) {
          const timeout =
            typeof this.timeout === "number" ? this.timeout : undefined;
          const ai = aiXhrStarted(aiUrl, args[0], timeout);
          const on = this.addEventListener;
          if (ai !== null && typeof on === "function") {
            const listen = on as (n: string, f: () => void) => void;
            listen.call(this, "load", () => {
              try {
                aiXhrSettled(ai, this);
              } catch {
                // never break the page
              }
            });
            for (const [evt, kind] of [
              ["error", "error"],
              ["abort", "abort"],
              ["timeout", "timeout"],
            ] as const) {
              listen.call(this, evt, () => {
                try {
                  aiXhrFailed(ai, kind);
                } catch {
                  // never break the page
                }
              });
            }
          }
        }
      } catch {
        // never break the page
      }
      try {
        return sendFn.apply(this, args);
      } catch (err) {
        // The browser refused the send outright (wrong state, blocked
        // address). No load, error, abort or timeout event will ever fire, so
        // a call registered a moment ago would sit in flight until the window
        // closed and then be reported as a hang that never happened.
        //
        // Through the SAME settlement as the events above, so the aggregate
        // and the per-part counts settle once, together, over exactly the
        // calls the sampler agreed to track. A call it declined is settled
        // for the aggregate alone, as it always was.
        try {
          const outcome = outcomeForError(err);
          if (settleThisSend !== null) settleThisSend(outcome);
          else noteCallSettled(started, outcome);
        } catch {
          // never break the page
        }
        throw err;
      }
    };

    // One method at a time, each checked: a prototype that refuses the
    // assignment either throws (non-writable, strict mode) or silently keeps
    // the old value. Remember which wrapper actually landed, so a half-done
    // wrapping can be undone precisely instead of guessed at.
    originalXhrOpen = openFn;
    originalXhrSend = sendFn;
    wrappedXhrProto = proto;
    try {
      proto.open = openWrapper;
      if (proto.open !== openWrapper) throw new Error("open refused");
      wrappedXhrOpen = openWrapper;
      proto.send = sendWrapper;
      if (proto.send !== sendWrapper) throw new Error("send refused");
      wrappedXhrSend = sendWrapper;
    } catch {
      restoreXhr();
      return false;
    }
    return true;
  } catch {
    restoreXhr();
    return false;
  }
}

/** Put back only what is still OURS. An app, a framework or another monitoring
 *  tool is free to wrap XHR after we did; restoring blindly on teardown would
 *  throw their wrapper away, and a kit must never take something off a page it
 *  did not put there. Safe to call when nothing was ever wrapped. */
function restoreXhr(): void {
  try {
    const proto = wrappedXhrProto;
    if (proto) {
      if (
        originalXhrOpen !== null &&
        wrappedXhrOpen !== null &&
        proto.open === wrappedXhrOpen
      ) {
        proto.open = originalXhrOpen;
      }
      if (
        originalXhrSend !== null &&
        wrappedXhrSend !== null &&
        proto.send === wrappedXhrSend
      ) {
        proto.send = originalXhrSend;
      }
    }
  } catch {
    // teardown must never throw
  }
  originalXhrOpen = null;
  originalXhrSend = null;
  wrappedXhrOpen = null;
  wrappedXhrSend = null;
  wrappedXhrProto = null;
}

export interface CallWatchOptions {
  /** Opt in to counting an error returned INSIDE a normal-looking response
   *  (a 200 whose JSON body carries `error`, or a GraphQL `errors` array) as a
   *  failure. Off by default: it costs a response clone, and only the app
   *  knows whether its backend answers that way. */
  bodyErrors?: boolean;
  /** The object carrying `fetch` / `XMLHttpRequest`. Defaults to the page's own
   *  global scope, which is what production always uses. Tests pass a plain
   *  object so a suite can exercise the wrappers without mutating the shared
   *  runner global — patching the real one leaks across files and is how a
   *  passing suite starts lying about which calls it saw. */
  scope?: object;
}

/** The origin of the page whose calls we are watching, read once at install
 *  time from the scope actually wrapped. Null when there is none, in which
 *  case no platform-cache verdict can be attributed to anyone. */
let watchedOrigin: string | null = null;

/** Install the call watch. Returns true when at least one client was wrapped —
 *  which is also when the sampler switches off its resource-timing fallback.
 *  Idempotent and never throws. */
export function installCallWatch(options: CallWatchOptions = {}): boolean {
  try {
    bodyErrorsEnabled = options.bodyErrors === true;
    if (installed) return true;
    const scope = (options.scope ?? globalThis) as AnyRecord;
    watchedOrigin = pageOriginOf(scope) ?? pageOriginOf(globalThis);
    const fetchOk = wrapFetch(scope);
    const xhrOk = wrapXhr(scope);
    if (!fetchOk && !xhrOk) return false;
    wrappedScope = scope;
    installed = true;
    setCallWatchActive(true);
    // The AI-call reading rides these wrappers, so it begins and ends with
    // them — and is told which transports actually landed, because one that
    // refused to be wrapped is a blind spot rather than a silence.
    try {
      setWatchedTransports(fetchOk, xhrOk);
      armAiCalls();
    } catch {
      // the outbound watch stands on its own; the AI reading simply stays away
    }
    return true;
  } catch {
    return false;
  }
}

/** Remove the wrappers and hand the sampler back to Resource Timing. */
export function uninstallCallWatch(): void {
  try {
    const scope = (wrappedScope ?? globalThis) as AnyRecord;
    if (originalFetch !== null && scope.fetch === wrappedFetch) {
      scope.fetch = originalFetch;
    }
  } catch {
    // teardown must never throw either
  }
  restoreXhr();
  originalFetch = null;
  wrappedFetch = null;
  wrappedScope = null;
  bodyErrorsEnabled = false;
  installed = false;
  setCallWatchActive(false);
  // The platform's cache verdict is fed EXCLUSIVELY from these wrappers, so
  // teardown clears it too — nothing Boosthis-shaped keeps counting after the
  // kit lets go of the page.
  clearEdgeCache();
  // Same for the AI reading: it stops feeding, while the readings already
  // taken stay readable until the snapshot that carries them.
  try {
    unarmAiCalls();
  } catch {
    // teardown must never throw
  }
}

/** Whether the wrappers are currently in place. */
export function isCallWatchInstalled(): boolean {
  return installed;
}

/**
 * The page's `fetch` as it was BEFORE we wrapped it — the one BOOSTHIS'S OWN
 * requests must use.
 *
 * Our wrapper sits on the page's global fetch, so without this every
 * registration, snapshot upload, check-in and rule lookup we make would be
 * counted as one of the app's own backend calls. The app this whole reading
 * exists for — a front end with no backend of its own — would be told its
 * busiest, and possibly slowest, destination is us. That is worse than no
 * reading: it is a confident wrong answer.
 *
 * Resolved at CALL time, never captured at import, so it cannot go stale
 * across an uninstall/reinstall. Returns null when we never wrapped anything,
 * and the caller simply keeps using the page's own fetch.
 */
export function unwatchedFetch(): typeof fetch | null {
  try {
    const scope = wrappedScope;
    const original = originalFetch;
    if (!installed || scope === null || original === null) return null;
    const call = (input: unknown, init?: unknown): Promise<unknown> =>
      init === undefined
        ? original.call(scope, input)
        : original.call(scope, input, init);
    return call as unknown as typeof fetch;
  } catch {
    return null;
  }
}

/** Whether the opt-in body-error check is on. */
export function isBodyErrorCheckEnabled(): boolean {
  return bodyErrorsEnabled;
}

/** @internal test hook. */
export function _resetCallWatchForTests(): void {
  uninstallCallWatch();
  // Durable AI totals would otherwise leak from one test file into the next.
  try {
    clearAiCalls();
  } catch {
    // nothing to clear
  }
}
