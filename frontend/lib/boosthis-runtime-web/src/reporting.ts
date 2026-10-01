/* ─── Boosthis: problem reporting (browser) ──────────────────────────
 *
 * Bridges the browser kit's EXISTING live detectors to the shared
 * candidate/resolution engine in `./candidateRules`, so what a real visitor
 * hits on a real page becomes a reported problem instead of a number that dies
 * with the tab.
 *
 * This module adds NO new detection. Every finding below is read back from a
 * reading the kit already computes, against a threshold the kit already
 * applies, so the problem-reporting channel and the meter tiles can never
 * disagree about what was wrong:
 *
 *   • slow-nav              ← the per-page-view sample summary's own rating
 *   • slow-api / failing-api ← the network reading's own p75 + drop-rate
 *                              thresholds (NETWORK_THRESHOLDS)
 *   • api-thundering-herd   ← the circuit fan-out counter (6 starts in 1s,
 *                              byte-parity with the server runtimes)
 *   • reconnect-storm       ← the realtime reading's own storm counter
 *   • connection-stalled    ← the realtime reading's own quiet/dead verdict
 *
 * The last four are spelled exactly as the back-end kits spell them, and
 * `slow-api` is the spelling the shared vocabulary folds a back end's
 * `slow-route` onto — so one problem seen from the visitor's side and from the
 * server's side meets in one place instead of looking like two unrelated
 * things.
 *
 * PRIVACY. A finding's `name` (a page label, a fixed word) is local only: it
 * feeds dedupe and a generic hint and is dropped by `signatureFor` before
 * anything is uploaded. What travels is `<kind>:<severityBucket>:<countBucket>`
 * and an occurrence count. No URL, no query string, no value, no code.
 *
 * GATES. This channel is always-on for a REGISTERED install — it is not part of
 * the optional screen-bearing detail channel and is not gated on the developer
 * enable/disable toggle. The kill switch and `forget()` are what stop it, and
 * both clear the submitters so nothing can be sent afterwards. See
 * `docs/kit-problem-reporting-contract.md`.
 *
 * PAGE CLOSE. A browser session ends without warning, so the tick cadence alone
 * would lose the report of every short visit — the common case on a plain
 * multi-page site. The loop therefore also flushes on `pagehide` with
 * `keepalive`, exactly like the sample queue and the snapshot mirror.
 */

import { summary } from "./samples";
import { getCircuitStats, FANOUT_WINDOW_MS } from "./vitals";
import { readNetwork } from "./networkSampler";
import { NETWORK_THRESHOLDS } from "./meterAxes";
import {
  readLiveConnections,
  LIVECONN_STORM_WINDOW_MS,
  LIVECONN_QUIET_MS,
} from "./liveConnections";
import { isBoosthisDisabled } from "./runtimeFlags";
import { ingestFindings, type CrossCuttingFinding } from "./candidateRules";

/** Build the current finding set from the kit's own live readings.
 *
 *  Every branch is wrapped: a reading that throws must never stop the others
 *  being reported, and none of this may reach a visitor's page. Empty under the
 *  kill switch — under it we do not even look. */
export function collectFindings(): CrossCuttingFinding[] {
  if (isBoosthisDisabled()) return [];
  const out: CrossCuttingFinding[] = [];

  // ── Page views the kit already rated needs-work / poor ──────────────────
  try {
    const sum = summary();
    for (const [route, r] of Object.entries(sum.byRoute)) {
      if (r.worst_rating === "good") continue;
      out.push({
        kind: "slow-nav",
        name: route,
        p95: r.max_ms,
        count: r.count,
        hint: "A page view is rated needs-work or poor against its load budget.",
      });
    }
  } catch {
    // a broken sample buffer must not silence the rest
  }

  // ── Outbound calls, judged by the network reading's OWN thresholds ───────
  try {
    const net = readNetwork();
    // `pending` means fewer attempts than the reading needs to say anything.
    // Silence, not a zero — the same posture the tile takes.
    if (net.rating !== "pending" && net.attemptCount > 0) {
      if (net.p75Ms !== null && net.p75Ms >= NETWORK_THRESHOLDS.p75PoorMs) {
        out.push({
          kind: "slow-api",
          name: "outbound-calls",
          p95: Math.round(net.worstMs),
          count: net.attemptCount,
          hint: "Calls out to other services keep taking a long time to answer.",
        });
      }
      // Same drop definition the score uses, so the two can never disagree.
      const drops = net.timeoutCount + net.stallCount + net.failedCount;
      if (drops / net.attemptCount >= NETWORK_THRESHOLDS.stallRatePoor) {
        out.push({
          kind: "failing-api",
          name: "outbound-calls",
          p95: Math.round(net.worstMs),
          count: drops,
          hint: "Calls out to other services keep coming back as errors.",
        });
      }
    }
  } catch {
    // no network reading — nothing to say
  }

  // ── Fan-out burst (the circuit detector already counts it) ──────────────
  try {
    const circuit = getCircuitStats();
    if (circuit.requestBurstDetected) {
      out.push({
        kind: "api-thundering-herd",
        // Fixed non-route label. The circuit counter never reads a URL, so
        // there is nothing here that could carry customer data.
        name: "outbound-fan-out",
        // Honest measurement: the window the burst was counted in, and how many
        // starts landed inside it. Mirrors the server kits, which report the
        // burst span and the distinct-destination count.
        p95: FANOUT_WINDOW_MS,
        count: circuit.requestBurstMax1s,
        hint: "A burst of requests started inside a single second.",
      });
    }
  } catch {
    // circuit stats unavailable — fine
  }

  // ── Long-lived connections (the realtime reading's own verdicts) ────────
  try {
    const rt = readLiveConnections();
    if (rt) {
      if (rt.stormCount > 0) {
        out.push({
          kind: "reconnect-storm",
          name: "realtime",
          p95: LIVECONN_STORM_WINDOW_MS,
          count: rt.stormCount,
          hint: "A connection was re-opened repeatedly with no widening gap between attempts.",
        });
      }
      if (rt.stalled > 0) {
        out.push({
          kind: "connection-stalled",
          name: "realtime",
          p95: LIVECONN_QUIET_MS,
          count: rt.stalled,
          hint: "A connection is open but has gone silent far past its own rhythm.",
        });
      }
    }
  } catch {
    // no realtime reading — absence is the honest answer, never a row of zeros
  }

  return out;
}

/* ── Tick loop ───────────────────────────────────────────────────────────
 *
 * Deliberately slow. Accumulation is cheap but it is still work on somebody
 * else's page, and the local store persists across page loads, so there is
 * nothing to gain from looking often. The first pass waits for the vitals to
 * land; after that the cadence matches the snapshot mirror's.
 */

const REPORT_FIRST_MS = 5_000;
const REPORT_TICK_MS = 60_000;

let timer: ReturnType<typeof setTimeout> | null = null;
let started = false;
let running = false;

function unrefIfPossible(t: unknown): void {
  // In a Node context (SSR, tests) a live timer must never hold the process up.
  const h = t as { unref?: () => void } | null;
  if (h && typeof h.unref === "function") h.unref();
}

function arm(delayMs: number): void {
  if (!started || timer !== null) return;
  try {
    timer = setTimeout(() => {
      timer = null;
      void (async () => {
        await tickOnce();
        arm(REPORT_TICK_MS);
      })();
    }, delayMs);
    unrefIfPossible(timer);
  } catch {
    timer = null;
  }
}

async function tickOnce(opts?: { keepalive?: boolean }): Promise<void> {
  if (running) return;
  running = true;
  try {
    await ingestFindings(collectFindings(), opts);
  } catch {
    // best-effort: bookkeeping must never disturb the host page
  } finally {
    running = false;
  }
}

/** Exit flush. `pagehide` is the only ending a browser reliably gives us, and
 *  `keepalive` is what lets the request outlive the dying document. */
const pagehideFlush = (): void => {
  void tickOnce({ keepalive: true });
};

/** Start the reporting loop. Idempotent. Called once the install is registered
 *  and the submitters are wired; a no-op under the kill switch. */
export function startReporting(): void {
  if (started || isBoosthisDisabled()) return;
  started = true;
  try {
    if (typeof addEventListener === "function") {
      // Idempotent per the DOM contract: re-adding the same reference is a
      // no-op, so repeated sync calls are safe.
      addEventListener("pagehide", pagehideFlush);
    }
  } catch {
    // non-browser context — the cadence still ticks
  }
  arm(REPORT_FIRST_MS);
}

/** Stop the loop and unwire the exit flush. Leaves the accumulated store alone
 *  — stopping is not erasure; `clearAllCandidates()` is. */
export function stopReporting(): void {
  started = false;
  if (timer !== null) {
    try {
      clearTimeout(timer);
    } catch {
      // ignore
    }
    timer = null;
  }
  try {
    if (typeof removeEventListener === "function") {
      removeEventListener("pagehide", pagehideFlush);
    }
  } catch {
    // ignore
  }
}

/** Whether the loop is currently armed. */
export function isReportingStarted(): boolean {
  return started;
}

/** Force one report pass now, awaiting the uploads. Used by the exit flush, by
 *  the live proof rig, and by deterministic tests. */
export async function reportNow(opts?: {
  keepalive?: boolean;
}): Promise<void> {
  await tickOnce(opts);
}

/** @internal test helper — reset the scheduler without touching the store. */
export function _resetReportingForTests(): void {
  stopReporting();
  running = false;
}

/** @internal test hook — the cadence constants, so a test asserts against the
 *  shipped values rather than restating them. */
export const _reportingInternals = {
  REPORT_FIRST_MS,
  REPORT_TICK_MS,
};
