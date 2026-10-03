import createClient from "openapi-fetch";
import type { paths } from "./schema";

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public messageEn: string,
    public messageAr: string,
    public extra: Record<string, unknown> = {},
  ) {
    super(messageEn);
  }
  message_for(lang: string): string {
    return lang === "ar" ? this.messageAr : this.messageEn;
  }
}

// Generated with `npm run gen:api`. Most calls still go through request() below.
export const typed = createClient<paths>({ baseUrl: "" });
typed.use({
  onRequest({ request }) {
    if (request.method !== "GET" && csrfToken) request.headers.set("X-CSRF-Token", csrfToken);
    return request;
  },
});

async function parseError(res: Response): Promise<ApiError> {
  let body: any = {};
  try {
    body = await res.json();
  } catch {
    /* not JSON */
  }
  const err = body?.error ?? {};
  return new ApiError(
    res.status,
    err.code ?? `http_${res.status}`,
    err.message_en ?? res.statusText,
    err.message_ar ?? err.message_en ?? res.statusText,
    err,
  );
}

/** A hung request should become a retryable error, not an endless spinner. */
export const DEFAULT_TIMEOUT_MS = 15_000;
/** For calls that do LLM work before they answer. */
export const LONG_TIMEOUT_MS = 150_000;
/** Advancing the clock runs a full agent day per step. */
export const DAY_RUN_TIMEOUT_MS = 15 * 60_000;

export interface RequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

/** Timed out, network down, or a body that wasn't JSON. */
export class RequestError extends Error {
  constructor(public kind: "timeout" | "network" | "bad_response") {
    super(`request failed: ${kind}`);
  }
}

export type ErrorKind = "auth" | "forbidden" | "server" | "timeout" | "network" | "other";

export function errorKind(e: unknown): ErrorKind {
  if (e instanceof ApiError) {
    if (e.status === 401) return "auth";
    if (e.status === 403) return "forbidden";
    if (e.status >= 500) return "server";
    return "other";
  }
  if (e instanceof RequestError) return e.kind === "bad_response" ? "server" : e.kind;
  return "other";
}

/** 4xx won't change on a second try. */
export function isTransient(e: unknown): boolean {
  const kind = errorKind(e);
  return kind === "server" || kind === "network";
}

export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  { signal, timeoutMs = DEFAULT_TIMEOUT_MS }: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const isForm = body instanceof FormData;
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;

  // Aborted by our deadline or by the caller (React Query cancels on unmount).
  const ctrl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    ctrl.abort();
  }, timeoutMs);
  const forward = () => ctrl.abort(signal?.reason);
  if (signal?.aborted) forward();
  else signal?.addEventListener("abort", forward, { once: true });
  const failure = (e: unknown, kind: "network" | "bad_response") => {
    if (timedOut) return new RequestError("timeout");
    if (signal?.aborted) return e; // cancelled; React Query wants the original abort error
    return new RequestError(kind);
  };

  try {
    let res: Response;
    try {
      res = await fetch(`/api/v1${path}`, {
        method,
        headers,
        credentials: "same-origin",
        body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
        signal: ctrl.signal,
      });
    } catch (e) {
      throw failure(e, "network");
    }
    if (!res.ok) throw await parseError(res);
    if (res.status === 204) return undefined as T;
    try {
      return (await res.json()) as T;
    } catch (e) {
      throw failure(e, "bad_response");
    }
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", forward);
  }
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>("GET", path, undefined, { signal }),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>("POST", path, body ?? {}, opts),
  patch: <T>(path: string, body: unknown, opts?: RequestOptions) => request<T>("PATCH", path, body, opts),
  // Uploads get OCR'd or imported before the response comes back.
  upload: <T>(path: string, form: FormData, opts?: RequestOptions) =>
    request<T>("POST", path, form, { timeoutMs: LONG_TIMEOUT_MS, ...opts }),
};
