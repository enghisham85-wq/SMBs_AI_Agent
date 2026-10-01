/** Resolve the one project key used by this browser page. */

import { warnProjectKeyRefused } from "./startAnnounce";

export type ProjectKeySource = "host-override" | "code" | "none" | "declined";

export interface ResolvedProjectKey {
  key: string | null;
  source: ProjectKeySource;
  overrodeShared: boolean;
  display: string | null;
  /** True when running keyless was ASKED FOR (the deliberate switch below),
   *  false when the key is simply absent. Both look identical from the outside
   *  — the page measures itself and appears nowhere — which is exactly why the
   *  kit must never present one as the other. */
  declined: boolean;
}

/** The deliberate "run with no project key" switch for the browser: the global
 *  `__BOOSTHIS_NO_PROJECT_KEY__` set to true. A key ALWAYS wins over it, so a
 *  stale switch left in a build can never silently mute a working install. */
function noKeyRequested(): boolean {
  try {
    return (
      (globalThis as { __BOOSTHIS_NO_PROJECT_KEY__?: unknown })
        .__BOOSTHIS_NO_PROJECT_KEY__ === true
    );
  } catch {
    // A hostile global must not break key resolution.
    return false;
  }
}

function trimmed(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/** Trim a supplied value, and REFUSE (never silently repair) one that cannot
 *  be a project key — naming, once, the attribute or setting it came from.
 *  A refused value resolves to "no key", exactly as if none had been given. */
function clean(value: unknown, source: string): string | null {
  const value2 = trimmed(value);
  if (value2 === null) return null;
  if (warnProjectKeyRefused(value2, source)) return null;
  return value2;
}

export function maskProjectKey(value: unknown): string | null {
  const key = trimmed(value);
  if (!key) return null;
  return key.length >= 8 ? `…${key.slice(-4)}` : "set";
}

export function resolveProjectKey(explicit?: string | null): ResolvedProjectKey {
  let host: string | null = null;
  try {
    host = clean(
      (globalThis as { __BOOSTHIS_PROJECT_KEY_WEB__?: unknown })
        .__BOOSTHIS_PROJECT_KEY_WEB__,
      "__BOOSTHIS_PROJECT_KEY_WEB__",
    );
  } catch {
    /* a hostile global must not break registration */
  }
  const code = clean(explicit, "the key passed in code");
  if (host) {
    return {
      key: host,
      source: "host-override",
      overrodeShared: !!code && code !== host,
      display: maskProjectKey(host),
      declined: false,
    };
  }
  if (code)
    return {
      key: code,
      source: "code",
      overrodeShared: false,
      display: maskProjectKey(code),
      declined: false,
    };
  const declined = noKeyRequested();
  return {
    key: null,
    source: declined ? "declined" : "none",
    overrodeShared: false,
    display: null,
    declined,
  };
}

export function describeProjectKeySource(source: ProjectKeySource): string {
  switch (source) {
    case "host-override": return "Web key set by this page at runtime";
    case "code": return "key passed in code";
    case "none": return "no project key configured";
    case "declined": return "running with no project key on purpose";
  }
}
