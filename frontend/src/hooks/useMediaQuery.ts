import { useCallback, useSyncExternalStore } from "react";

/** Whether a CSS media query matches now; re-renders when it starts or stops matching. */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      if (typeof window.matchMedia !== "function") return () => undefined;
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    [query],
  );
  return useSyncExternalStore(subscribe, () =>
    typeof window.matchMedia === "function" ? window.matchMedia(query).matches : false,
  );
}
