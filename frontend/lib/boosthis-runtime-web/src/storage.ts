/** Tiny persistent key/value store for the web runtime.
 *
 * Uses `localStorage` when available (a real browser) and falls back to an
 * in-memory Map everywhere else (SSR, tests, the Node-side MCP entrypoint),
 * so callers never have to feature-detect. Every access is wrapped in
 * try/catch because `localStorage` can THROW on access in some environments
 * (Safari private mode, sandboxed iframes, storage-disabled embeds) — a
 * blocked store must degrade to memory, never crash the host page.
 *
 * All keys are namespaced under `boosthis:` so the runtime never collides
 * with (or enumerates) the host app's own storage.
 */

const PREFIX = "boosthis:";

const mem = new Map<string, string>();

function ls(): Storage | null {
  try {
    if (typeof localStorage !== "undefined" && localStorage !== null) {
      return localStorage;
    }
  } catch {
    // Access itself can throw (storage disabled) — fall back to memory.
  }
  return null;
}

export const storage = {
  /** True when a real, durable store backs this wrapper (a browser with
   *  localStorage reachable). False means every write lands in the in-memory
   *  fallback and dies with the page — callers that accumulate state across
   *  reloads need to be able to SAY that rather than imply a saved map. */
  isPersistent(): boolean {
    return ls() !== null;
  },

  /** Read a value, or null when absent/unreadable. Never throws. */
  get(key: string): string | null {
    const k = PREFIX + key;
    const s = ls();
    if (s) {
      try {
        const v = s.getItem(k);
        if (v !== null) return v;
      } catch {
        // fall through to memory
      }
    }
    return mem.get(k) ?? null;
  },

  /** Persist a value. Best-effort — a full/blocked store degrades to the
   *  in-memory fallback (survives the session, not the reload).
   *
   *  Returns TRUE only when the value reached the durable store. Existing
   *  callers ignore the result; callers that accumulate state across reloads
   *  use it to tell a saved map from one that only looks saved. */
  set(key: string, value: string): boolean {
    const k = PREFIX + key;
    const s = ls();
    if (s) {
      try {
        s.setItem(k, value);
        return true;
      } catch {
        // Quota exceeded / blocked — degrade to memory.
      }
    }
    mem.set(k, value);
    return false;
  },

  /** Remove a value from BOTH layers (used by forget()). Never throws. */
  remove(key: string): void {
    const k = PREFIX + key;
    const s = ls();
    if (s) {
      try {
        s.removeItem(k);
      } catch {
        // ignore
      }
    }
    mem.delete(k);
  },
};

/** Test-only: clear the in-memory fallback so each test starts hermetic. */
export function _resetStorageForTests(): void {
  mem.clear();
}
