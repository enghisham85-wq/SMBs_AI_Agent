import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { EventStreamProvider, useEventStream } from "../../src/hooks/useEventStream";

class FakeEventSource extends EventTarget {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static all: FakeEventSource[] = [];
  readyState = FakeEventSource.CONNECTING;
  onerror: ((e: Event) => void) | null = null;
  constructor(public url: string) {
    super();
    FakeEventSource.all.push(this);
  }
  close() {
    this.readyState = FakeEventSource.CLOSED;
  }
  emit(name: string, data: unknown) {
    this.dispatchEvent(new MessageEvent(name, { data: JSON.stringify(data) }));
  }
  fail(state: number) {
    this.readyState = state;
    this.onerror?.(new Event("error"));
  }
}

const live = () => FakeEventSource.all.filter((s) => s.readyState !== FakeEventSource.CLOSED);

function setup() {
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries").mockResolvedValue();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <EventStreamProvider>{children}</EventStreamProvider>
    </QueryClientProvider>
  );
  return { wrapper, invalidate };
}

beforeEach(() => {
  FakeEventSource.all = [];
  vi.useFakeTimers();
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useEventStream", () => {
  it("shares one connection per stream and closes it with the last subscriber", () => {
    const { wrapper } = setup();
    const a = vi.fn();
    const b = vi.fn();
    const view = renderHook(
      ({ withA, withB }) => {
        useEventStream(withA ? "/harness/stream" : null, a, ["stage"]);
        useEventStream(withB ? "/harness/stream" : null, b, ["stage", "rule.proposed"]);
      },
      { wrapper, initialProps: { withA: true, withB: true } },
    );
    expect(live()).toHaveLength(1);

    live()[0].emit("stage", { stage: "verified" });
    live()[0].emit("rule.proposed", { text: "x" });
    expect(a).toHaveBeenCalledTimes(1);
    expect(a).toHaveBeenCalledWith("stage", { stage: "verified" });
    expect(b).toHaveBeenCalledTimes(2);

    view.rerender({ withA: false, withB: true });
    expect(live()).toHaveLength(1);
    view.rerender({ withA: false, withB: false });
    expect(live()).toHaveLength(0);
  });

  it("reopens a stream that has gone silent past the watchdog", () => {
    const { wrapper } = setup();
    renderHook(() => useEventStream("/chat/stream", vi.fn(), ["created"]), { wrapper });
    const original = live()[0];

    // Pings keep it alive.
    for (let i = 0; i < 4; i++) {
      vi.advanceTimersByTime(15_000);
      original.emit("ping", {});
    }
    expect(live()).toEqual([original]);

    vi.advanceTimersByTime(60_000);
    vi.advanceTimersByTime(2_000);
    expect(original.readyState).toBe(FakeEventSource.CLOSED);
    expect(live()).toHaveLength(1);
    expect(live()[0]).not.toBe(original);
  });

  it("reconnects with backoff when the browser gives up, and re-checks the session once", () => {
    const { wrapper, invalidate } = setup();
    renderHook(() => useEventStream("/chat/stream", vi.fn(), ["created"]), { wrapper });

    live()[0].fail(FakeEventSource.CLOSED);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["me"] });
    expect(live()).toHaveLength(0);
    vi.advanceTimersByTime(1_250);
    expect(live()).toHaveLength(1);

    live()[0].fail(FakeEventSource.CLOSED);
    vi.advanceTimersByTime(1_250);
    expect(live()).toHaveLength(0); // backoff is ~2 s now
    vi.advanceTimersByTime(1_500);
    expect(live()).toHaveLength(1);
    expect(invalidate).toHaveBeenCalledTimes(1);
  });

  it("leaves transient errors to the browser's own retry", () => {
    const { wrapper, invalidate } = setup();
    renderHook(() => useEventStream("/chat/stream", vi.fn(), ["created"]), { wrapper });
    live()[0].fail(FakeEventSource.CONNECTING);
    expect(live()).toHaveLength(1);
    expect(invalidate).not.toHaveBeenCalled();
  });
});
