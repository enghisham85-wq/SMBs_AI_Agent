import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";

type Handler = (event: string, data: any) => void;

interface Subscriber {
  events: string[];
  handler: RefObject<Handler>;
}

// The backend sends a named "ping" after 15 s without traffic; three missed pings means the connection is
// dead even if the browser has not noticed (proxies and sleeping laptops hold half-open sockets).
const WATCHDOG_MS = 45_000;
const WATCHDOG_TICK_MS = 15_000;
const MAX_BACKOFF_MS = 30_000;

/** One EventSource per stream URL, shared by every component listening to it. */
class SharedStream {
  private source: EventSource | null = null;
  private subscribers = new Set<Subscriber>();
  private names = new Map<string, number>();
  private lastSeen = 0;
  private failures = 0;
  private watchdog: ReturnType<typeof setInterval> | null = null;
  private retry: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly path: string,
    private readonly onClosed: () => void,
  ) {}

  get idle(): boolean {
    return this.subscribers.size === 0;
  }

  add(sub: Subscriber): void {
    this.subscribers.add(sub);
    for (const name of sub.events) {
      const count = this.names.get(name) ?? 0;
      this.names.set(name, count + 1);
      if (count === 0) this.source?.addEventListener(name, this.dispatch as EventListener);
    }
    if (!this.source && !this.retry) this.open();
  }

  remove(sub: Subscriber): void {
    if (!this.subscribers.delete(sub)) return;
    for (const name of sub.events) {
      const count = (this.names.get(name) ?? 1) - 1;
      if (count > 0) {
        this.names.set(name, count);
      } else {
        this.names.delete(name);
        this.source?.removeEventListener(name, this.dispatch as EventListener);
      }
    }
    if (this.idle) this.close();
  }

  private alive = () => {
    this.lastSeen = Date.now();
    this.failures = 0;
  };

  private dispatch = (e: MessageEvent) => {
    this.alive();
    let data: unknown;
    try {
      data = JSON.parse(e.data);
    } catch {
      data = e.data;
    }
    for (const sub of this.subscribers) if (sub.events.includes(e.type)) sub.handler.current(e.type, data);
  };

  private open(): void {
    this.retry = null;
    const source = new EventSource(`/api/v1${this.path}`, { withCredentials: true });
    this.source = source;
    this.lastSeen = Date.now();
    source.addEventListener("open", this.alive);
    source.addEventListener("ping", this.alive);
    source.addEventListener("hello", this.alive);
    for (const name of this.names.keys()) source.addEventListener(name, this.dispatch as EventListener);
    // While CONNECTING the browser retries by itself. CLOSED means it gave up: the server answered with
    // an error status (an expired session is the usual one), so retry ourselves with backoff.
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) this.reconnect(true);
    };
    this.watchdog ??= setInterval(() => {
      if (this.source && Date.now() - this.lastSeen > WATCHDOG_MS) this.reconnect(false);
    }, WATCHDOG_TICK_MS);
  }

  private reconnect(rejected: boolean): void {
    this.dropSource();
    // Ask /me once per failure streak; a 401 there takes the user to the sign-in screen.
    if (rejected && this.failures === 0) this.onClosed();
    const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** this.failures) * (0.75 + Math.random() * 0.5);
    this.failures += 1;
    this.retry = setTimeout(() => this.open(), delay);
  }

  private dropSource(): void {
    if (!this.source) return;
    this.source.onerror = null;
    this.source.close();
    this.source = null;
  }

  private close(): void {
    this.dropSource();
    if (this.retry) clearTimeout(this.retry);
    if (this.watchdog) clearInterval(this.watchdog);
    this.retry = null;
    this.watchdog = null;
    this.failures = 0;
  }
}

class StreamHub {
  private streams = new Map<string, SharedStream>();

  constructor(private readonly onClosed: () => void) {}

  subscribe(path: string, sub: Subscriber): () => void {
    const stream = this.streams.get(path) ?? new SharedStream(path, this.onClosed);
    this.streams.set(path, stream);
    stream.add(sub);
    return () => {
      stream.remove(sub);
      if (stream.idle) this.streams.delete(path);
    };
  }
}

const HubContext = createContext<StreamHub | null>(null);

export function EventStreamProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [hub] = useState(() => new StreamHub(() => void qc.invalidateQueries({ queryKey: ["me"] })));
  return <HubContext.Provider value={hub}>{children}</HubContext.Provider>;
}

/** Subscribe to Server-Sent Events; components on the same path share one connection. */
export function useEventStream(path: string | null, onEvent: Handler, events: string[]) {
  const hub = useContext(HubContext);
  if (!hub) throw new Error("useEventStream outside EventStreamProvider");
  const handler = useRef(onEvent);
  useEffect(() => {
    handler.current = onEvent;
  }, [onEvent]);

  const key = events.join(",");
  useEffect(() => {
    if (!path) return;
    return hub.subscribe(path, { events: key.split(","), handler });
  }, [hub, path, key]);
}
