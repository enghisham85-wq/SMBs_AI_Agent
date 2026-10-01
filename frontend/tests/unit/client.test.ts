import {
  api,
  ApiError,
  DEFAULT_TIMEOUT_MS,
  errorKind,
  isTransient,
  LONG_TIMEOUT_MS,
  request,
  RequestError,
} from "../../src/api/client";
import { errorText } from "../../src/api/errorText";

// A fetch that never answers on its own: it settles only when its signal aborts, like a hung server.
function hangingFetch() {
  return vi.fn(
    (_url: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        const abort = () => reject(new DOMException("aborted", "AbortError"));
        if (init.signal?.aborted) abort();
        else init.signal?.addEventListener("abort", abort);
      }),
  );
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("request deadline", () => {
  it("turns a hung call into a timeout error after the default deadline", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", hangingFetch());
    let done = false;
    const call = api.get("/home").finally(() => {
      done = true;
    });
    const settled = expect(call).rejects.toBeInstanceOf(RequestError);
    await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS - 1);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await settled;
    await expect(call).rejects.toMatchObject({ kind: "timeout" });
  });

  it("gives uploads the long deadline", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", hangingFetch());
    let done = false;
    const call = api.upload("/documents", new FormData()).finally(() => {
      done = true;
    });
    const settled = expect(call).rejects.toMatchObject({ kind: "timeout" });
    await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS * 2);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(LONG_TIMEOUT_MS);
    await settled;
  });

  it("passes a caller's cancellation through untouched", async () => {
    vi.stubGlobal("fetch", hangingFetch());
    const ctrl = new AbortController();
    const call = api.get("/home", ctrl.signal);
    ctrl.abort();
    await expect(call).rejects.toMatchObject({ name: "AbortError" });
  });

  it("fails at once when the caller cancelled before the call", async () => {
    const fetch = hangingFetch();
    vi.stubGlobal("fetch", fetch);
    const ctrl = new AbortController();
    ctrl.abort();
    await expect(request("GET", "/home", undefined, { signal: ctrl.signal })).rejects.toMatchObject({
      name: "AbortError",
    });
    expect((fetch.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true);
  });
});

describe("request errors", () => {
  it("maps a failed connection to a network error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(api.get("/home")).rejects.toMatchObject({ kind: "network" });
  });

  it("maps a 200 with a non-JSON body to a bad response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<html>proxy</html>", { status: 200 })));
    await expect(api.get("/home")).rejects.toMatchObject({ kind: "bad_response" });
  });

  it("keeps the server's own error code and bilingual message", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          json(409, { error: { code: "chaos_busy", message_en: "Busy.", message_ar: "مشغول." } }),
        ),
    );
    const err = (await api.post("/chaos/inject", {}).catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: "chaos_busy" });
    expect(err.message_for("ar")).toBe("مشغول.");
  });

  it("classifies failures for the error screens", () => {
    expect(errorKind(new ApiError(401, "http_401", "", ""))).toBe("auth");
    expect(errorKind(new ApiError(403, "permission_denied", "", ""))).toBe("forbidden");
    expect(errorKind(new ApiError(503, "http_503", "", ""))).toBe("server");
    expect(errorKind(new ApiError(422, "invalid", "", ""))).toBe("other");
    expect(errorKind(new RequestError("timeout"))).toBe("timeout");
    expect(errorKind(new RequestError("network"))).toBe("network");
    expect(errorKind(new RequestError("bad_response"))).toBe("server");
    expect(errorKind(new Error("render bug"))).toBe("other");
  });

  it("retries only failures that may pass on a second try", () => {
    expect(isTransient(new ApiError(502, "http_502", "", ""))).toBe(true);
    expect(isTransient(new RequestError("network"))).toBe(true);
    expect(isTransient(new RequestError("timeout"))).toBe(false);
    expect(isTransient(new ApiError(401, "http_401", "", ""))).toBe(false);
    expect(isTransient(new ApiError(404, "not_found", "", ""))).toBe(false);
  });

  it("words errors by cause unless the server sent its own message", () => {
    expect(errorText(new RequestError("timeout"))).toMatch(/too long/);
    expect(errorText(new ApiError(502, "http_502", "Bad Gateway", "Bad Gateway"))).toMatch(/server/i);
    expect(errorText(new ApiError(409, "chaos_busy", "Another scenario is still running.", ""))).toBe(
      "Another scenario is still running.",
    );
  });
});
