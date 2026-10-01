// Thin client over the REST API. Sends the session cookie and the CSRF header on writes.
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

// Typed client generated from the backend OpenAPI document (npm run gen:api). The backend returns
// JSON built by its own serializer, so most calls go through `request()` below.
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

export async function request<T>(method: string, path: string, body?: unknown, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const isForm = body instanceof FormData;
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(`/api/v1${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
    ...init,
  });
  if (!res.ok) throw await parseError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  upload: <T>(path: string, form: FormData) => request<T>("POST", path, form),
};
