import { useEffect, useRef } from "react";

/** Subscribe to a Server-Sent Events endpoint; reconnects automatically (EventSource default). */
export function useEventStream(path: string | null, onEvent: (event: string, data: any) => void, events: string[]) {
  const handler = useRef(onEvent);
  useEffect(() => {
    handler.current = onEvent;
  }, [onEvent]);

  const key = events.join(",");
  useEffect(() => {
    if (!path) return;
    const source = new EventSource(`/api/v1${path}`, { withCredentials: true });
    const listeners = key.split(",").map((name) => {
      const fn = (e: MessageEvent) => {
        try {
          handler.current(name, JSON.parse(e.data));
        } catch {
          handler.current(name, e.data);
        }
      };
      source.addEventListener(name, fn as EventListener);
      return [name, fn] as const;
    });
    return () => {
      listeners.forEach(([name, fn]) => source.removeEventListener(name, fn as EventListener));
      source.close();
    };
  }, [path, key]);
}
