/** Global runtime kill-switch — mirrors the RN + Python runtimes.
 *
 * If the environment variable `BOOSTHIS_DISABLED` (legacy: `BOOSTEN_DISABLED`)
 * is set to a truthy value ("1", "true", "yes", "on") at runtime, every
 * Boosthis hot path becomes a no-op:
 *
 *   - `safeTransmit()` short-circuits and returns a synthetic 204 Response
 *     without ever touching the network (the PII guard still runs first).
 *   - `enableTelemetry()` returns a client whose methods all no-op, including
 *     the otherwise always-on issue + fix reporting.
 *
 * This is the "I'm running in CI" / "kill it from outside the process" escape
 * hatch. It works without any code change in the consumer app — set the env
 * var and Boosthis goes silent. Re-evaluated on every call so it can be
 * toggled at runtime (useful for tests).
 *
 * A BROWSER PAGE HAS NO ENVIRONMENT VARIABLES, so every flag this kit reads
 * also answers to two globals — `globalThis.BOOSTHIS_DISABLED` and the dunder
 * spelling the install guide teaches, `globalThis.__BOOSTHIS_DISABLED__`. See
 * `readFlagValue` below: it is the ONE place any flag is read, precisely so a
 * flag can never again be readable only from a path a shipped page cannot
 * take.
 */

const TRUE_VALUES = new Set(["1", "true", "yes", "on"]);

/** Read one named flag from every spelling this kit honours.
 *
 *  THE BROWSER CANNOT REACH AN ENVIRONMENT VARIABLE. This kit's tests run
 *  under vitest's `node` environment, where `process.env` is real, so an
 *  env-only flag reads perfectly in every test and is unreachable in every
 *  shipped page — the same shape as the Node kit's `require("node:…")` meters,
 *  which passed every test and were dead in every real install. So the order
 *  here always ends on something a page can actually set:
 *
 *    1. `process.env.NAME`  — Node, SSR, or a bundler that truly defines it.
 *    2. `globalThis.NAME`   — a browser page.
 *    3. `globalThis.__NAME__` — the dunder spelling the install guide teaches.
 *
 *  An empty string is treated as absent so a blank value never shadows a
 *  later spelling. Never throws.
 */
export function readFlagValue(name: string): string | boolean | undefined {
  if (typeof process !== "undefined" && process.env) {
    const v = (process.env as Record<string, string | undefined>)[name];
    if (typeof v === "string" && v.length > 0) return v;
  }
  const g = globalThis as unknown as Record<string, unknown>;
  for (const key of [name, `__${name}__`]) {
    const v = g[key];
    if (typeof v === "boolean") return v;
    if (typeof v === "string" && v.length > 0) return v;
  }
  return undefined;
}

function readEnv(): string | undefined {
  // Loop (not `||`) so a set-but-empty new var still falls back to the legacy
  // name — keeps apps wired up before the rename working.
  for (const name of ["BOOSTHIS_DISABLED", "BOOSTEN_DISABLED"]) {
    const v = readFlagValue(name);
    if (typeof v === "boolean") {
      if (v) return "1";
      continue;
    }
    if (typeof v === "string" && v.length > 0) return v;
  }
  return undefined;
}

/** Returns true if Boosthis should be entirely silent. Cheap; safe to call
 *  from any hot path. */
export function isBoosthisDisabled(): boolean {
  const v = readEnv();
  if (!v) return false;
  return TRUE_VALUES.has(v.toLowerCase());
}

/** Test helper — set the flag programmatically. Returns a cleanup function
 *  that restores the previous value. */
export function _setBoosthisDisabledForTests(
  value: boolean | string | undefined,
): () => void {
  const g = globalThis as unknown as { BOOSTHIS_DISABLED?: string | boolean };
  const prev = g.BOOSTHIS_DISABLED;
  if (value === undefined) delete g.BOOSTHIS_DISABLED;
  else g.BOOSTHIS_DISABLED = value;
  return () => {
    if (prev === undefined) delete g.BOOSTHIS_DISABLED;
    else g.BOOSTHIS_DISABLED = prev;
  };
}
