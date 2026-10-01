/**
 * bubblePanel — pure, display-only helpers for the web bubble's full dashboard
 * panel. Kept separate from `bubble.ts` (the DOM/shadow-root plumbing) so the
 * meter-row derivation, rating labels, and bar widths are unit-testable without
 * a browser.
 *
 * The web analogue of the RN dashboard's MetricRow logic: it turns the axes the
 * web kit already computes (`capturePerfSnapshot().axes`) into labeled rows with
 * a numeric caption, a rating color, and a 0–100 bar width. Axes that cannot be
 * computed honestly are ABSENT from the snapshot; this module renders those as
 * pending "measuring…" rows (never a fake zero). Nothing here transmits.
 */

import type { WebAxisResult, WebAxisValue, WebSnapshotPayload } from "./snapshot";
import {
  RESILIENCE_MIN_MEDIAN_MS,
  RESILIENCE_MIN_TAIL_SAMPLES,
  RESILIENCE_TAIL_FLOOR_MS,
  type Rating,
} from "./thresholds";
// The press→screen floor is owned by the module that enforces it. Imported
// rather than re-typed so the panel's wording can never quote a number the
// reading no longer uses (a caption quoting a threshold reads it live).
// pressToScreen imports only thresholds + buildIdentity, so this edge adds no
// cycle to the kit's module graph.
import { MIN_JOINED_NAVS } from "./pressToScreen";

/**
 * A rating that can also carry one of the three silences. "pending" is the
 * only one that means a score is still coming; "not-available" (this page's
 * host cannot take the reading) and "not-scored" (a real reading that is
 * deliberately never graded) both mean it is NOT coming, and a reader has to
 * be able to tell those apart from the rating alone. An unrecognised word
 * from a newer kit degrades to the pending styling, which is safe.
 */
export type PanelRating = Rating | "pending" | "not-available" | "not-scored";

/** One rendered meter row. `score`/`rating` are null while pending so the DOM
 *  layer can render a "measuring…" state with a zero-width bar. */
export interface PanelMeterRow {
  /** Stable axis key (the snapshot axis name). */
  key: string;
  /** Human label shown on the left. */
  label: string;
  /** Right-hand numeric caption, or "measuring…" when pending. */
  caption: string;
  /** 0–100 composite score for this axis, or null while pending. */
  score: number | null;
  /** Rating bucket, or "pending". */
  rating: PanelRating;
}

/** The meter axes in display order. Labels use "project"/plain-English web
 *  wording; every axis is display-only and omit-when-unavailable. The order
 *  mirrors the snapshot's `buildAxes` insertion order. */
export const PANEL_AXES: ReadonlyArray<{ key: string; label: string }> = [
  { key: "responsiveness", label: "Responsiveness" },
  { key: "frustration", label: "Frustration" },
  { key: "resilience", label: "Resilience" },
  { key: "stability", label: "Stability" },
  { key: "layoutStability", label: "Layout stability" },
  { key: "memory", label: "Memory" },
  { key: "resourceEfficiency", label: "Resource efficiency" },
  { key: "paintReadiness", label: "Paint readiness" },
  { key: "interactionToNextPaint", label: "Interaction to Next Paint" },
  { key: "animationSmoothness", label: "Animation smoothness" },
  { key: "crashFree", label: "Crash-free" },
  // New RN-parity axes (shared labels from the axis contract).
  { key: "confidence", label: "Confidence" },
  { key: "budget", label: "On budget" },
  { key: "baseline", label: "Baseline" },
  { key: "network", label: "Network" },
  { key: "idle", label: "Idle efficiency" },
  { key: "scroll", label: "Scroll" },
  { key: "frameFloor", label: "Frame floor" },
  { key: "timerHealth", label: "Timer Health" },
  { key: "blockingAsync", label: "Blocking async" },
  { key: "asyncSlowCallbacks", label: "Slow async callbacks" },
  { key: "eventLoopLag", label: "Event loop lag" },
  { key: "swallowedErrors", label: "Near-miss rate" },
  { key: "leakWatch", label: "Leak Watch" },
  { key: "devPosture", label: "Dev Posture" },
  // Additive web-meter batch (shared bubble labels from the axis contract).
  { key: "frameCause", label: "Slow frame cause" },
  { key: "thirdPartyCost", label: "Third-party cost" },
  { key: "bfcache", label: "Back/forward cache" },
  // NOTE: emitted as "tapReadiness" (not "inputReadiness") — the shared PII
  // guard denies the fragment "input". Same numbers, PII-clean name.
  { key: "tapReadiness", label: "Input readiness" },
  { key: "softNav", label: "Soft navigation" },
  { key: "pressToScreen", label: "Press to screen" },
  { key: "blockingTime", label: "Blocking time" },
  { key: "memoryTrend", label: "Memory trend" },
  { key: "resourceBloat", label: "Resource weight" },
  { key: "rageClicks", label: "Rage clicks" },
  // Platform / delivery batch (Aug 2026). Same labels the /app dashboard uses
  // — the bubble is a pixel-parity mirror, so any label change must move in
  // lockstep with AXIS_LABELS on the server.
  { key: "navReadiness", label: "Page readiness" },
  { key: "redirectOverhead", label: "Redirect overhead" },
  { key: "serverTiming", label: "Server processing" },
  { key: "handshakeCost", label: "Handshake cost" },
  { key: "protocolMix", label: "Protocol mix" },
  { key: "renderBlocking", label: "Render-blocking" },
  { key: "transferWaste", label: "Transfer waste" },
  { key: "cacheRevalidation", label: "Cache revalidation" },
  { key: "cacheReuse", label: "Cache reuse" },
  { key: "edgeCache", label: "This site's platform cache" },
  { key: "initiatorBalance", label: "Request mix" },
  { key: "storageHeadroom", label: "Storage headroom" },
  { key: "swControl", label: "Service worker" },
  { key: "connectionQuality", label: "Connection quality" },
  { key: "deviceCapacity", label: "Device capacity" },
  { key: "prerender", label: "Prerender" },
  { key: "fontReadiness", label: "Font readiness" },
  { key: "assetFootprint", label: "Script & style count" },
  { key: "crossOriginIsolation", label: "Cross-origin isolation" },
  { key: "reportPressure", label: "Deprecation reports" },
  { key: "unhandledFailures", label: "Unhandled failures" },
  { key: "promiseRejections", label: "Promise rejections" },
  { key: "rejectionPressure", label: "Rejection pressure" },
  { key: "backgroundWork", label: "Background work" },
  { key: "interactionVolume", label: "Interaction volume" },
  { key: "shiftSources", label: "Layout shift sources" },
  { key: "mediaPlayback", label: "Media playback" },
  { key: "domFootprint", label: "DOM footprint" },
  { key: "idleOpportunity", label: "Idle opportunity" },
  // Patch lag (build-identity / exposure window) — how stale the deployed
  // build is. ONLY-IF-PRESENT (see ONLY_IF_PRESENT_AXES): it never renders a
  // pending "measuring…" row, because "no build time known" is honest absence,
  // not a warming meter. HONESTY: reports lag only — never implies "patched".
  { key: "patchLag", label: "Patch lag" },
  // Live Connections — open sockets and streams. ONLY-IF-PRESENT: a page with
  // no long-lived connection has nothing to warm up, so a "measuring…" row
  // would be an invented promise.
  { key: "liveConnections", label: "Live connections" },
  // Data distance — how far the app is from its own backend, judged on how
  // long a NEW connection takes to open (never on the round trip, which
  // would report a slow backend as a distant one). ONLY-IF-PRESENT: a page
  // whose calls all reused one warm connection has nothing to time, and
  // that is honest absence rather than a warming meter.
  { key: "dependencyDistance", label: "Data distance" },
  // Failing Routes — of the calls this page made, how many came back failed.
  // Worded exactly as the dashboard words it, because it is the same reading.
  // ONLY-IF-PRESENT: a page that has made no call the watcher could see has
  // nothing to warm up, and "no answers seen" is honest absence. A pending row
  // here would be a promise that a failure count is on its way when it is not.
  { key: "routeFailures", label: "Failing Routes" },
  // The page's own local store (Web Storage) — the two readings a browser app
  // can honestly have where there is no database to wait on. ONLY-IF-PRESENT:
  // both are behind the BOOSTHIS_STORAGE_METER opt-in, so a page that never
  // switched it on, or one whose storage is walled off, has nothing to warm
  // up. Labelled exactly as the dashboard labels them — same reading, same
  // words. IndexedDB is deliberately not among them; see
  // docs/decisions/browser-local-store-observation.md.
  { key: "storageLatency", label: "Storage Speed" },
  { key: "storageFailures", label: "Storage Failures" },
];

/** Axes that must render ONLY when actually present in the snapshot (never a
 *  pending "measuring…" row). For these, absence is HONEST — it means the
 *  signal genuinely is not known this session, not that it is still warming up.
 *  Mirrors how an optional/only-if-present axis (e.g. the Node runtime's
 *  `mcpTools`) is surfaced elsewhere. */
export const ONLY_IF_PRESENT_AXES: ReadonlySet<string> = new Set([
  // Browser/API/content-dependent axes. The server's WEB_EXPECTED_AXES and
  // WEB_DEVICE_AXES omit the same keys, so an empty snapshot cannot make the
  // panel promise a meter which the web page correctly does not promise.
  "frameCause",
  "thirdPartyCost",
  "bfcache",
  "softNav",
  "blockingTime",
  "memoryTrend",
  "serverTiming",
  "protocolMix",
  "renderBlocking",
  "transferWaste",
  "cacheRevalidation",
  "cacheReuse",
  "edgeCache",
  "initiatorBalance",
  "storageHeadroom",
  "swControl",
  "connectionQuality",
  "prerender",
  "crossOriginIsolation",
  "reportPressure",
  "interactionVolume",
  "shiftSources",
  "mediaPlayback",
  "domFootprint",
  "idleOpportunity",
  "patchLag",
  "liveConnections",
  "dependencyDistance",
  "routeFailures",
  "storageLatency",
  "storageFailures",
  "backgroundWork",
  // Processor, thread and scheduling family. Present-only for the same reason
  // the server keeps them out of WEB_EXPECTED_AXES: a page whose tab is
  // backgrounded before the first window closes has nothing to report, and
  // long-task reporting is not in every engine. A "measuring…" row would
  // promise a reading that engine can never send.
  "eventLoopLag",
  "blockingAsync",
  "asyncSlowCallbacks",
]);

/** No-score axes that word themselves from their own numbers instead of
 *  shipping a caption string. The failure reading is here because its wire
 *  shape is deliberately closed to prose — a string field is how such a shape
 *  grows to hold an address — so the panel does the wording locally. */
export const SELF_WORDED_NO_SCORE_AXES: ReadonlySet<string> = new Set([
  "routeFailures",
]);

/** Round a number for display, keeping a stable, non-noisy caption. */
function n(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** PRESSES THIS READING COULD NOT TURN INTO A NUMBER: past the join bound, no
 *  view at all, or a view with no reading of its own.
 *
 *  They travel with the reading wherever it is shown — scored, warming or
 *  answered — because a number published without them is the old defect
 *  wearing a longer window: it would describe only the navigations quick
 *  enough to survive (docs/press-to-screen-contract.md). */
function unjoinedPresses(axis: WebAxisResult): number {
  return (
    (n(axis.unjoinedPastBound) ?? 0) +
    (n(axis.unjoinedNoScreen) ?? 0) +
    (n(axis.unjoinedNoTiming) ?? 0)
  );
}

/**
 * Build the honest numeric caption for one axis from its raw fields. Uses the
 * SAME numbers the snapshot ships (no re-derivation), so the panel can never
 * disagree with the uploaded meter picture. Falls back to the axis's own
 * `caption` string when present, then to a bare score.
 */
export function axisCaption(key: string, axis: WebAxisResult): string {
  switch (key) {
    case "responsiveness": {
      const p75 = n(axis.p75Ms);
      const count = n(axis.count);
      return p75 == null
        ? `${axis.score}/100`
        : `${Math.round(p75)} ms p75${count != null ? ` · ${count} views` : ""}`;
    }
    case "frustration": {
      const w = n(axis.worstDelayMs);
      return w == null ? `${axis.score}/100` : `${Math.round(w)} ms worst input`;
    }
    case "resilience": {
      // Four visibly distinct readings, never a blank or a bare zero: a ratio ·
      // a tail too small to have one · a median too small to take one from ·
      // not enough traffic yet.
      const tail = n(axis.tailRatio);
      const count = n(axis.sampleCount);
      const samples = count != null ? ` · ${count} samples` : "";
      if (tail != null) return `${tail}× tail${samples}`;
      if (typeof axis.score === "number") {
        return `every request under ${RESILIENCE_TAIL_FLOOR_MS}ms${samples}`;
      }
      if (count != null && count >= RESILIENCE_MIN_TAIL_SAMPLES) {
        return `tail not judged — median under ${RESILIENCE_MIN_MEDIAN_MS}ms${samples}`;
      }
      return `warming up${samples}`;
    }
    case "stability": {
      const perMin = n(axis.longTasksPerMin);
      const count = n(axis.longTaskCount);
      const worst = n(axis.worstBlockMs);
      return perMin == null
        ? `${axis.score}/100`
        : `${Math.round(perMin * 10) / 10}/min stalls${
            count != null ? ` · ${count} total` : ""
          }${worst != null && worst > 0 ? ` · worst ${Math.round(worst)}ms` : ""}`;
    }
    case "layoutStability": {
      const cls = n(axis.cls);
      return cls == null ? `${axis.score}/100` : `${cls.toFixed(3)} CLS`;
    }
    case "memory": {
      const pct = n(axis.pct);
      const used = n(axis.usedMb);
      const limit = n(axis.limitMb);
      if (used != null && limit != null) return `${used} / ${limit} MB`;
      return pct == null ? `${axis.score}/100` : `${pct}% heap`;
    }
    case "resourceEfficiency": {
      const pct = n(axis.efficientPct);
      return pct == null ? `${axis.score}/100` : `${pct}% cached/compressed`;
    }
    case "paintReadiness": {
      const fcp = n(axis.fcpMs);
      return fcp == null ? `${axis.score}/100` : `${Math.round(fcp)} ms FCP`;
    }
    case "interactionToNextPaint": {
      const inp = n(axis.inpMs);
      const count = n(axis.interactionCount);
      return inp == null
        ? `${axis.score}/100`
        : `${Math.round(inp)} ms INP${count != null ? ` · ${count} interactions` : ""}`;
    }
    case "animationSmoothness": {
      const perMin = n(axis.loafPerMin);
      return perMin == null
        ? `${axis.score}/100`
        : `${Math.round(perMin * 10) / 10}/min long frames`;
    }
    case "crashFree": {
      const crashes = n(axis.crashes);
      return crashes == null
        ? `${axis.score}/100`
        : crashes === 0
          ? "no crashes"
          : `${crashes} crash${crashes === 1 ? "" : "es"}`;
    }
    case "budget": {
      const onBudget = n(axis.onBudget);
      const total = n(axis.total);
      if (onBudget != null && total != null) {
        return `${onBudget}/${total} routes on budget`;
      }
      const pct = n(axis.pct);
      return pct == null ? `${axis.score}/100` : `${pct}% on budget`;
    }
    case "baseline": {
      const anomalies = n(axis.anomalyCount);
      const scored = n(axis.scoredRoutes);
      const ratio = n(axis.worstRatio);
      // A route whose earlier window is all sub-millisecond has no denominator
      // to divide by, so it was NOT examined: an abstention must never read as
      // steadiness, and the count beside the verdict names only the routes
      // actually compared.
      const skipped = n(axis.unscoredRoutes) ?? 0;
      const skippedTail = skipped > 0 ? ` · ${skipped} too fast to compare` : "";
      if (scored === 0) return baselineNotJudged(skipped);
      if (anomalies != null && anomalies > 0 && ratio != null && ratio > 0) {
        return `${ratio}× slower · ${anomalies} regressed${skippedTail}`;
      }
      if (scored != null) return `${scored} routes steady${skippedTail}`;
      return `${axis.score}/100`;
    }
    case "network": {
      const p75 = n(axis.p75Ms);
      const stall = n(axis.stallPct);
      if (p75 != null) {
        return `${Math.round(p75)} ms p75${stall != null ? ` · ${stall}% dropped` : ""}`;
      }
      return `${axis.score}/100`;
    }
    case "idle": {
      const pct = n(axis.idleBusyPct);
      return pct == null ? `${axis.score}/100` : `${pct}% busy while idle`;
    }
    case "scroll": {
      const jank = n(axis.jankPct);
      const frames = n(axis.frameCount);
      return jank == null
        ? `${axis.score}/100`
        : `${jank}% janky${frames != null ? ` · ${frames} frames` : ""}`;
    }
    case "frameFloor": {
      const fps = n(axis.floorFps);
      return fps == null ? `${axis.score}/100` : `${Math.round(fps)} fps floor`;
    }
    case "timerHealth": {
      const growth = n(axis.growthPerMin);
      const timers = n(axis.timers);
      if (growth != null) {
        return `${growth}/min growth${timers != null ? ` · ${timers} live` : ""}`;
      }
      return `${axis.score}/100`;
    }
    case "blockingAsync":
    case "asyncSlowCallbacks": {
      const perMin = n(axis.perMin);
      const count = n(axis.count);
      return perMin == null
        ? `${axis.score}/100`
        : `${perMin}/min${count != null ? ` · ${count} observed` : ""}`;
    }
    case "eventLoopLag": {
      const p95 = n(axis.p95Ms);
      const count = n(axis.sampleCount);
      return p95 == null
        ? `${axis.score}/100`
        : `${Math.round(p95)} ms p95${count != null ? ` · ${count} samples` : ""}`;
    }
    case "swallowedErrors": {
      const count = n(axis.count);
      if (count == null) return `${axis.score}/100`;
      return count === 0
        ? "no logged errors"
        : `${count} error${count === 1 ? "" : "s"} logged, app survived`;
    }
    case "leakWatch": {
      // COUNTS + category words ONLY — never any matched content, and the
      // clean caption reads as absence-of-evidence ("we did not observe this"),
      // never "you are protected".
      const count = n(axis.count);
      if (count == null) return `${axis.score}/100`;
      if (count === 0) return "no leaks observed in this window";
      const secret = n(axis.secretCount) ?? 0;
      const pii = n(axis.piiCount) ?? 0;
      const stack = n(axis.stackCount) ?? 0;
      const parts: string[] = [];
      if (secret > 0) parts.push(`${secret} secret-like`);
      if (pii > 0) parts.push(`${pii} personal-detail`);
      if (stack > 0) parts.push(`${stack} stack trace`);
      const detail = parts.length ? ` (${parts.join(", ")})` : "";
      return `${count} possible leak${count === 1 ? "" : "s"} observed${detail}`;
    }
    case "devPosture": {
      // COUNTS + setting NAMES only — the snapshot ships the caption verbatim
      // (counts + the fixed setting-name phrases), so restate it. Reads as
      // EXPOSURE ("none of the N development settings we can read were on"),
      // never "hardened".
      return typeof axis.caption === "string" ? axis.caption : `${axis.score}/100`;
    }
    case "frameCause": {
      const blocking = n(axis.blockingMsPerMin);
      const thirdParty = n(axis.thirdPartyPct);
      const script = n(axis.scriptPct);
      const styleLayout = n(axis.styleLayoutPct);
      if (blocking == null) return `${axis.score}/100`;
      const tp = thirdParty ?? 0;
      const sc = script ?? 0;
      const sl = styleLayout ?? 0;
      if (tp === 0 && sc === 0 && sl === 0) return "no slow frames";
      // Dominant = the largest of thirdPartyPct / scriptPct / styleLayoutPct.
      let dominant = "style/layout";
      if (tp >= sc && tp >= sl) dominant = "3rd-party scripts";
      else if (sc >= tp && sc >= sl) dominant = "scripts";
      return `${blocking}ms/min blocked · ${dominant} heavy`;
    }
    case "thirdPartyCost": {
      const tpMin = n(axis.thirdPartyMsPerMin);
      const tpPct = n(axis.thirdPartyPct);
      if (tpMin == null) return `${axis.score}/100`;
      return `${tpMin}ms/min 3rd-party${
        tpPct == null ? "" : ` · ${tpPct}% of script time`
      }`;
    }
    case "bfcache": {
      const restored = n(axis.restoredCount);
      const navs = n(axis.bfNavCount);
      const blocked = n(axis.blockedCount);
      if (navs == null) return `${axis.score}/100`;
      if (restored != null && restored === navs) {
        return `${restored}/${navs} back navs instant`;
      }
      let cap = `${blocked ?? 0}/${navs} back navs re-ran`;
      // WHY. "Other blocker" is the answer that tells a developer nothing —
      // the reason list now names the common causes, and where it cannot it
      // points at where the browser will say so itself.
      const blockers: Array<[number, string]> = [
        [n(axis.blockerUnload) ?? 0, "unload handler"],
        [n(axis.blockerCacheControl) ?? 0, "no-store header"],
        [n(axis.blockerInFlight) ?? 0, "request still in flight"],
        [n(axis.blockerHeldConnection) ?? 0, "connection held open"],
        [n(axis.blockerBrowser) ?? 0, "browser's own decision"],
        [n(axis.blockerOther) ?? 0, "see notRestoredReasons in DevTools"],
        [n(axis.blockerUnnamed) ?? 0, "browser named no reason"],
      ];
      let top: [number, string] | null = null;
      for (const entry of blockers) {
        if (entry[0] > 0 && (top === null || entry[0] > top[0])) top = entry;
      }
      if (top !== null) cap += ` · ${top[1]}`;
      return cap;
    }
    case "tapReadiness": {
      const early = n(axis.earlyTapCount);
      if (early == null) return `${axis.score}/100`;
      if (early === 0) return "no early taps dropped";
      return `${early} tap(s) before ready`;
    }
    case "softNav": {
      const p75 = n(axis.p75Ms);
      const navCount = n(axis.navCount);
      if (p75 == null) return `${axis.score}/100`;
      return `${p75}ms p75 route change · ${navCount ?? 0} soft navs`;
    }
    case "pressToScreen": {
      // The whole wait, plus every press that never became one. A count of
      // presses that reached no timed screen is the thing a silent discard
      // used to hide, so it is said here even when there is no number yet.
      const p75 = n(axis.p75Ms);
      const navCount = n(axis.navCount) ?? 0;
      const unjoined = unjoinedPresses(axis);
      if (p75 == null) {
        // "This browser can never take the reading" is NOT worded here. That
        // state has one sentence for the whole product — the dashboard's —
        // and the row builder renders it from the reason code the kit sent,
        // with these same counts appended. A second wording invented here
        // would give one reading two answers on two surfaces.
        //
        // PRESSES THAT DID JOIN, but fewer than the reading needs, are their
        // own state. "No press followed to a screen yet" over four joined
        // navigations is simply false — and it is the sentence a developer
        // would act on, by going looking for a join that is working. The
        // floor is read from the module that enforces it, never typed here.
        if (navCount > 0) {
          const sofar = `${navCount} of ${MIN_JOINED_NAVS} navs joined`;
          return unjoined === 0 ? sofar : `${sofar} · ${unjoined} not joined`;
        }
        return unjoined === 0
          ? "no press followed to a screen yet"
          : `${unjoined} press(es) reached no timed screen`;
      }
      const base = `${p75}ms p75 press→usable · ${navCount} nav${
        navCount === 1 ? "" : "s"
      }`;
      return unjoined === 0 ? base : `${base} · ${unjoined} not joined`;
    }
    case "blockingTime": {
      const blocking = n(axis.blockingMsPerMin);
      const longTasks = n(axis.longTaskCount);
      if (blocking == null) return `${axis.score}/100`;
      return `${blocking}ms/min blocking · ${longTasks ?? 0} long tasks`;
    }
    case "memoryTrend": {
      const used = n(axis.usedMb);
      const growth = n(axis.growthMbPerMin);
      if (used == null) return `${axis.score}/100`;
      const g = growth ?? 0;
      const signed = g >= 0 ? `+${g}` : `${g}`;
      return `${used} MB · ${signed} MB/min`;
    }
    case "resourceBloat": {
      const totalKb = n(axis.totalKb);
      const count = n(axis.resourceCount);
      if (totalKb == null) return `${axis.score}/100`;
      const weight =
        totalKb >= 1024
          ? `${Math.round((totalKb / 1024) * 10) / 10} MB`
          : `${totalKb} KB`;
      return `${weight} transferred · ${count ?? 0} resources`;
    }
    case "rageClicks": {
      const bursts = n(axis.burstCount);
      const worst = n(axis.worstDelayMs);
      if (bursts == null) return `${axis.score}/100`;
      if (bursts === 0) return "no rage clicks";
      return `${bursts} burst(s)${
        worst == null ? "" : ` · worst ${worst}ms response`
      }`;
    }
    /* ── Platform / delivery batch (Aug 2026) ──────────────────────────────
     * Each caption restates the SAME numbers the snapshot ships (never a
     * re-derivation), so the bubble can never disagree with the dashboard. */
    case "navReadiness": {
      const i = n(axis.domInteractiveMs);
      const c = n(axis.domCompleteMs);
      if (i == null) return `${axis.score}/100`;
      return `${Math.round(i)} ms interactive${c != null ? ` · ${Math.round(c)} ms complete` : ""}`;
    }
    case "redirectOverhead": {
      const ms = n(axis.redirectMs);
      const count = n(axis.redirectCount);
      if (ms == null) return `${axis.score}/100`;
      return count === 0 ? "no redirects" : `${Math.round(ms)} ms · ${count} redirect(s)`;
    }
    case "serverTiming": {
      const ms = n(axis.serverMs);
      return ms == null ? `${axis.score}/100` : `${Math.round(ms)} ms reported by the server`;
    }
    case "dependencyDistance": {
      // Own backend only — the browser cannot see these phases for anyone
      // else, and says nothing rather than showing a zero.
      if (n(axis.measurable) === 0) {
        const seen = n(axis.newConnections) ?? 0;
        const need = n(axis.minConnections) ?? 0;
        return `too few new connections to judge · ${seen} of ${need}`;
      }
      const setup = n(axis.worstSetupMs);
      if (setup == null) return `${axis.score}/100`;
      const share = n(axis.worstSharePct);
      const shareNote = share == null ? "" : ` · ${share}% of a typical call`;
      // A SLOWER READING WE DID NOT JUDGE. The connection floor is right, but
      // the number above is not the worst while a bigger one sits unmentioned
      // in the same reading.
      const unjudged = n(axis.unjudgedWorstSetupMs);
      const unjudgedNote =
        unjudged == null
          ? ""
          : ` · a slower one (${Math.round(unjudged)} ms) had too few connections to judge`;
      return `own backend ${setup < 10 ? setup : Math.round(setup)} ms away${shareNote}${unjudgedNote}`;
    }
    case "handshakeCost": {
      const ms = n(axis.handshakeMs);
      if (ms == null) return `${axis.score}/100`;
      return ms === 0 ? "connection reused" : `${Math.round(ms)} ms DNS+TCP+TLS`;
    }
    case "protocolMix": {
      const modern = n(axis.modernPct);
      const count = n(axis.protoResourceCount);
      if (modern == null) return `${axis.score}/100`;
      return `${modern}% modern${count != null ? ` · ${count} resources` : ""}`;
    }
    case "renderBlocking": {
      const blocking = n(axis.renderBlockingCount);
      const seen = n(axis.observedCount);
      if (blocking == null) return `${axis.score}/100`;
      return `${blocking} blocking${seen != null ? ` of ${seen}` : ""}`;
    }
    case "transferWaste": {
      const pct = n(axis.overheadPct);
      const kb = n(axis.wasteKb);
      if (pct == null) return `${axis.score}/100`;
      return `${pct}% over${kb != null ? ` · ${kb} KB` : ""}`;
    }
    case "cacheRevalidation": {
      const pct = n(axis.revalidatedPct);
      const cached = n(axis.cachedCount);
      if (pct == null) return `${axis.score}/100`;
      return `${pct}% revalidated${cached != null ? ` · ${cached} cached` : ""}`;
    }
    case "cacheReuse": {
      const pct = n(axis.hitPct);
      const kb = n(axis.networkKb);
      const ms = n(axis.networkMs);
      if (pct == null) return `${axis.score}/100`;
      const cost =
        kb != null && ms != null
          ? ` · ${kb} KB, ${Math.round(ms)} ms over the network`
          : "";
      return `${pct}% from cache${cost}`;
    }
    case "edgeCache": {
      const pct = n(axis.hitPct);
      const misses = n(axis.misses);
      if (pct == null) return `${axis.score}/100`;
      return `${pct}% served from cache${misses != null ? ` · ${misses} fetched fresh` : ""}`;
    }
    case "initiatorBalance": {
      const script = n(axis.scriptPct);
      const img = n(axis.imgPct);
      if (script == null) return `${axis.score}/100`;
      // Requests made after the load event are excluded from the mix (a
      // live-updating section is not page construction). Name the exclusion
      // so the percentages cannot be read as covering everything.
      const post = n(axis.postLoadCount);
      const postTxt = post == null || post <= 0 ? "" : ` · ${post} after load, not counted`;
      return `${script}% script${img == null ? "" : ` · ${img}% image`}${postTxt}`;
    }
    case "storageHeadroom": {
      const pct = n(axis.usedPct);
      const used = n(axis.usedMb);
      const quota = n(axis.quotaMb);
      if (pct == null) return `${axis.score}/100`;
      return `${pct}% used${used != null && quota != null ? ` · ${used}/${quota} MB` : ""}`;
    }
    // The page's OWN Web Storage, kept as TWO rows because a slow store and a
    // failing store are different problems. Both say which store the number is
    // about, so nobody reads the speed of saved sessions as the speed of an
    // IndexedDB this kit deliberately never watches
    // (docs/decisions/browser-local-store-observation.md).
    case "storageLatency": {
      const p75 = n(axis.p75Ms);
      const ops = n(axis.opCount);
      if (p75 == null) return `${axis.score}/100`;
      return `${p75} ms p75${ops == null ? "" : ` · ${ops} ops`}`;
    }
    case "storageFailures": {
      const pct = n(axis.failPct);
      const bad = n(axis.failCount);
      const ops = n(axis.opCount);
      if (pct == null) return `${axis.score}/100`;
      return `${pct}% failed${bad != null && ops != null ? ` · ${bad}/${ops} ops` : ""}`;
    }
    case "swControl": {
      const on = n(axis.controlled);
      if (on == null) return `${axis.score}/100`;
      if (on === 1) return "controlled";
      // "not controlled" was one phrase for two facts — an app with no
      // service worker at all (nothing to fix) and one whose worker is not
      // driving this page (a real finding).
      const reg = n(axis.registered);
      return reg === 1
        ? "registered, not controlling this page"
        : reg === 0
          ? "no service worker"
          : "registration unknown";
    }
    case "connectionQuality": {
      const rtt = n(axis.rttMs);
      const down = n(axis.downlinkMbps);
      if (rtt == null) return `${axis.score}/100`;
      // The visitor's network, not the app's doing — context beside a slow
      // result, never a grade.
      return `${Math.round(rtt)} ms RTT${down != null ? ` · ${down} Mbps` : ""} · visitor's connection`;
    }
    case "deviceCapacity": {
      const cores = n(axis.cpuCores);
      const gb = n(axis.memoryGb);
      if (cores == null && gb == null) return `${axis.score}/100`;
      return `${cores ?? "?"} cores · ${gb ?? "?"} GB`;
    }
    case "prerender": {
      const pre = n(axis.prerendered);
      const ms = n(axis.activationMs);
      if (pre == null) return `${axis.score}/100`;
      return pre === 1
        ? `prerendered${ms != null ? ` · ${Math.round(ms)} ms lead` : ""}`
        : "not prerendered";
    }
    case "fontReadiness": {
      const loaded = n(axis.fontLoaded);
      // The judged interval — fonts measured from the moment the HTML
      // arrived. `fontMs` (from navigation start) carries the server round
      // trip in front of it and is not what this verdict is about.
      const ms = n(axis.fontDelayMs) ?? n(axis.fontMs);
      if (loaded == null) return `${axis.score}/100`;
      return loaded === 1
        ? `fonts ready${ms != null ? ` ${Math.round(ms)} ms after the page arrived` : ""}`
        : "fonts still loading — not rated yet";
    }
    case "assetFootprint": {
      const scripts = n(axis.scriptCount);
      const styles = n(axis.styleCount);
      if (scripts == null) return `${axis.score}/100`;
      return `${scripts} scripts · ${styles ?? 0} stylesheets`;
    }
    case "crossOriginIsolation": {
      const iso = n(axis.isolated);
      return iso == null ? `${axis.score}/100` : iso === 1 ? "isolated" : "not isolated";
    }
    case "reportPressure": {
      const dep = n(axis.deprecationCount);
      const iv = n(axis.interventionCount);
      if (dep == null) return `${axis.score}/100`;
      return dep + (iv ?? 0) === 0
        ? "no browser reports"
        : `${dep} deprecation · ${iv ?? 0} intervention`;
    }
    case "unhandledFailures": {
      const errs = n(axis.errorCount);
      const rej = n(axis.rejectionCount);
      if (errs == null) return `${axis.score}/100`;
      return errs + (rej ?? 0) === 0
        ? "none this session"
        : `${errs} error(s) · ${rej ?? 0} rejection(s)`;
    }
    case "interactionVolume": {
      const count = n(axis.interactionCount);
      return count == null ? `${axis.score}/100` : `${count} interaction(s)`;
    }
    case "shiftSources": {
      const worst = n(axis.worstSourceCount);
      const shifts = n(axis.shiftCount);
      if (worst == null) return `${axis.score}/100`;
      return `${worst} source(s) in worst shift${shifts != null ? ` · ${shifts} shifts` : ""}`;
    }
    case "mediaPlayback": {
      const pct = n(axis.droppedPct);
      const dropped = n(axis.droppedFrames);
      if (pct == null) return `${axis.score}/100`;
      return `${pct}% dropped${dropped != null ? ` · ${dropped} frames` : ""}`;
    }
    case "domFootprint": {
      const count = n(axis.elementCount);
      return count == null ? `${axis.score}/100` : `${count} elements`;
    }
    case "idleOpportunity": {
      const pct = n(axis.missedPct);
      const calls = n(axis.idleCalls);
      if (pct == null) return `${axis.score}/100`;
      return `${pct}% starved${calls != null ? ` · ${calls} probes` : ""}`;
    }
    case "patchLag": {
      // Build AGE only — the meter reports lag, never patched/safe.
      const ageMs = n(axis.buildAgeMs);
      if (ageMs == null) return `${axis.score}/100`;
      const days = Math.round(ageMs / 86_400_000);
      return `build ~${days}d old`;
    }
    case "liveConnections": {
      // The kit already worded this one when it took the reading, because only
      // the kit knows which of the four problems it saw. Fall back to the bare
      // open count rather than a score, which says nothing about a socket.
      if (typeof axis.caption === "string") return axis.caption;
      const open = n(axis.open);
      return open == null ? `${axis.score}/100` : `${open} open`;
    }
    case "routeFailures": {
      // WHETHER THE CALLS WORKED, worded here rather than shipped: the wire
      // shape for this reading is closed to prose, because a string field is
      // how a closed shape grows to hold an address. Counts only — the share
      // is a derived verdict, withheld until enough calls have been seen, and
      // decided once on the server.
      const observed = n(axis.observed) ?? 0;
      const failed = n(axis.failed) ?? 0;
      const untracked = n(axis.untracked) ?? 0;
      const over = untracked > 0 ? ` \u00b7 ${untracked} over the cap` : "";
      // Seen nothing is NOT "nothing failed"; the axis is absent entirely when
      // nothing at all was seen, so this is the cap-only case.
      if (observed === 0) return `no calls counted yet${over}`;
      return failed === 0
        ? `none of ${observed} calls failed${over}`
        : `${failed} of ${observed} calls failed${over}`;
    }
    default:
      return typeof axis.caption === "string"
        ? axis.caption
        : `${axis.score}/100`;
  }
}

/**
 * Turn the snapshot's axes into ordered display rows. Every axis in
 * {@link PANEL_AXES} produces a row; axes ABSENT from the snapshot render as
 * pending "measuring…" (never a fake zero). Pure.
 */
/** Type guard — a SCORED object axis carries a numeric `score` field. An axis
 *  present with a null score is still warming up and takes the pending path
 *  below. The confidence axis is emitted as scalar sibling keys instead, so it
 *  is handled separately. */
function isObjectAxis(v: WebAxisValue | undefined): v is WebAxisResult & { score: number } {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as WebAxisResult).score === "number"
  );
}

/**
 * Wording for a baseline reading that compared nothing: every route with
 * enough samples had an earlier window too fast to divide by. One phrase, used
 * by both the scored and the pending caption paths, so the two can never drift.
 */
function baselineNotJudged(skipped: number): string {
  return `not judged · ${skipped} route${skipped === 1 ? "" : "s"} too fast to compare`;
}
/**
 * Caption for an axis that is present but not yet scored. Nearly always plain
 * "measuring…", but a network reading can arrive unscored while already
 * holding the one thing worth saying out loud: calls that hung, timed out or
 * failed. Saying "measuring…" over a hanging call would hide it on the very
 * surface a developer is looking at.
 */
function pendingAxisCaption(key: string, v: WebAxisValue | undefined): string {
  if (typeof v !== "object" || v === null) return "measuring…";
  // A baseline reading that is PRESENT but unscored is an abstention, not a
  // warm-up: every route's earlier window was too fast to divide by. Saying
  // "measuring…" would promise a reading that is never coming.
  if (key === "baseline") {
    const skipped = n((v as WebAxisResult).unscoredRoutes) ?? 0;
    return skipped > 0 ? baselineNotJudged(skipped) : "measuring…";
  }
  // The press→screen reading words its own unscored state, from the counts it
  // already holds: "nothing joined yet, and here is how many presses went
  // nowhere" is an answer, and "measuring…" would delete the half of it that
  // is already known (docs/press-to-screen-contract.md). Worded by the SAME
  // function that words the scored row, so the two can never drift apart.
  if (key === "pressToScreen") return axisCaption(key, v as WebAxisResult);
  if (key !== "network") return "measuring…";
  const axis = v as WebAxisResult;
  const watching = n(axis.watching) === 1;
  const parts: string[] = [];
  const hung = n(axis.stallCount) ?? 0;
  const timedOut = n(axis.timeoutCount) ?? 0;
  const failed = n(axis.failedCount) ?? 0;
  if (watching && hung > 0) parts.push(`${hung} still hanging`);
  if (watching && timedOut > 0) parts.push(`${timedOut} timed out`);
  if (failed > 0) parts.push(`${failed} failed`);
  return parts.length > 0 ? `${parts.join(" · ")} · measuring…` : "measuring…";
}

/**
 * Boosthis's closed vocabulary for "this reading cannot be taken here", in the
 * words the dashboard uses. A kit may never send prose (it runs inside a
 * customer's app), so it sends a code and BOTH surfaces render the same
 * sentence from it. An unrecognised code still returns the lead alone — the
 * kit's claim that the reading is impossible stands even when the reason does
 * not, and "warming up" would be a promise nobody can keep.
 */
const NOT_AVAILABLE_WORDING: Readonly<Record<number, string>> = {
  1: "the app has not switched this on",
  2: "this app runs on an older version that cannot report it",
  3: "the platform does not share this number with apps",
  4: "the app has not been given permission to read it",
  5: "it is switched off in this build",
  6: "there is nothing here to compare it against",
  7: "it needs a profiler this app does not run",
  8: "where this app runs blocks the reading",
};

/** The row caption for an axis that says it cannot be measured here, or null
 *  when the axis said no such thing (absent, or a real reading).
 *
 *  `measurable: 0` ALONE is not that claim. Several axes in this kit ship a
 *  zero while still gathering — a baseline whose routes were all too fast to
 *  divide by, an AI reading below its minimum watched turns — and reading
 *  those as "not available here" tells a customer to stop waiting for a score
 *  that is genuinely on its way. The zero means the host cannot take the
 *  reading only with positive evidence beside it: a `reasonCode` on the
 *  reading, which is what patchLag sends. An unrecognised code still returns
 *  the lead alone — the kit's claim that the reading is impossible stands
 *  even when the reason does not. */
function notAvailableRowCaption(v: WebAxisValue | undefined): string | null {
  if (typeof v !== "object" || v === null) return null;
  if (n((v as WebAxisResult).measurable) !== 0) return null;
  const code = n((v as WebAxisResult).reasonCode);
  if (code === null) return null;
  const why = NOT_AVAILABLE_WORDING[code];
  return why ? `not available here · ${why}` : "not available here";
}

/** WHICH silence a no-score row is. One decision for every row this panel
 *  builds, in one order of trust:
 *
 *    1. the reading's OWN word, when it declared one of the two final states
 *       — a kit that says "not-scored" is not guessing, and the panel may not
 *       overrule it;
 *    2. anything still measurable is warming up;
 *    3. a zero WITH positive evidence beside it (a `reasonCode`) is a reading
 *       this host cannot take;
 *    4. otherwise "pending" — the safe word, because it is the only one that
 *       promises nothing permanent.
 *
 *  Nothing in this kit sends "not-scored" today. It is honoured anyway so a
 *  later web axis that abstains by design renders as an abstention on its
 *  first release, rather than as a wait nobody ever ends. */
function noScoreRating(v: WebAxisValue | undefined): PanelRating {
  if (typeof v !== "object" || v === null) return "pending";
  const declared = (v as WebAxisResult).rating;
  if (declared === "not-scored" || declared === "not-available") {
    return declared;
  }
  if (n((v as WebAxisResult).measurable) !== 0) return "pending";
  return n((v as WebAxisResult).reasonCode) !== null ? "not-available" : "pending";
}

export function buildPanelRows(snap: WebSnapshotPayload): PanelMeterRow[] {
  const axes = snap.axes ?? {};
  // The confidence axis is scalar sibling keys, so read the map through the
  // widened value type for those keys.
  const scalar = axes as unknown as Record<string, WebAxisValue | undefined>;
  const rows: PanelMeterRow[] = [];
  for (const { key, label } of PANEL_AXES) {
    // Confidence is not an object axis — it is the four scalar sibling keys
    // `confidence` / `confidenceMounts` / `confidenceRating` / `confidenceCaption`.
    if (key === "confidence") {
      const level = scalar.confidence;
      const rating = scalar.confidenceRating;
      const caption = scalar.confidenceCaption;
      const mounts = scalar.confidenceMounts;
      if (typeof level !== "string" || typeof rating !== "string") {
        rows.push({ key, label, caption: "measuring…", score: null, rating: "pending" });
        continue;
      }
      // Map the confidence level onto a display score for the bar.
      const score =
        level === "high" ? 100 : level === "medium" ? 70 : level === "low" ? 30 : 0;
      const cap =
        typeof caption === "string"
          ? `${caption}${typeof mounts === "number" ? ` · ${mounts} samples` : ""}`
          : `${score}/100`;
      rows.push({
        key,
        label,
        caption: cap,
        score,
        rating: rating as PanelRating,
      });
      continue;
    }
    const axis = axes[key];
    if (!isObjectAxis(axis)) {
      const silence = noScoreRating(axis);
      // A reading the kit said OUT LOUD it cannot take here (`measurable: 0`)
      // is not warming up and is not absent — it is answered. Say so, with the
      // reason, on this surface too: the dashboard tile renders exactly this
      // state as "not available here", and a panel that stayed blank would tell
      // the developer a different story about the same meter.
      const said = notAvailableRowCaption(axis);
      if (said !== null) {
        // …and the counts ride with it. A reading that cannot be taken here
        // still knows how many presses it had to throw away, and the contract
        // is that wherever the reading is shown the counts are shown — an
        // answered row is the one place it is easiest to drop them, because
        // the sentence already reads as complete
        // (docs/press-to-screen-contract.md). Axes with none of those fields
        // add nothing.
        const unjoined = unjoinedPresses(axis as WebAxisResult);
        const caption = unjoined > 0 ? `${said} · ${unjoined} not joined` : said;
        // …and the RATING says so too. "pending" here would file a permanent
        // answer under "still measuring" for everything that sorts, colours
        // or counts these rows — the caption told the truth while the pill
        // promised a verdict that is never coming.
        rows.push({ key, label, caption, score: null, rating: silence });
        continue;
      }
      // Only-if-present axes (e.g. liveConnections) render NOTHING when absent
      // — their absence is honest ("not known"), not a warming meter — so skip
      // the row entirely instead of showing a pending "measuring…" state. A
      // reading that declared a FINAL silence is not an absence, though: it is
      // an answer, and hiding an answer is how the state gets lost.
      if (ONLY_IF_PRESENT_AXES.has(key) && silence === "pending") continue;
      const declaredCaption =
        typeof axis === "object" && axis !== null
          ? (axis as { caption?: unknown }).caption
          : undefined;
      // A no-score axis normally words itself by SHIPPING a caption. One may
      // not: the failure reading's wire shape is closed to prose, so it sends
      // counts and nothing else. Those axes are worded here from the numbers
      // they did send. Without this they would fall through to the pending
      // caption and a real, final answer would read as "measuring…".
      const selfWorded =
        silence !== "pending" && SELF_WORDED_NO_SCORE_AXES.has(key);
      rows.push({
        key,
        label,
        caption: selfWorded
          ? axisCaption(key, axis as WebAxisResult)
          : silence !== "pending" && typeof declaredCaption === "string"
            ? declaredCaption
            : pendingAxisCaption(key, axis),
        score: null,
        rating: silence,
      });
      continue;
    }
    const rating = (axis.rating as Rating) ?? "needs-work";
    rows.push({
      key,
      label,
      caption: axisCaption(key, axis),
      score: Math.round(axis.score),
      rating,
    });
  }
  return rows;
}

/** Bar width (0–100) for a row: the axis score, or 0 while pending. */
export function barWidth(row: PanelMeterRow): number {
  if (row.score == null) return 0;
  return Math.min(100, Math.max(0, row.score));
}

/** Uppercase, human rating label for the hero / rows. */
export function ratingLabel(rating: PanelRating): string {
  switch (rating) {
    case "good":
      return "GOOD";
    case "needs-work":
      return "NEEDS WORK";
    case "poor":
      return "POOR";
    // The two silences that are NOT progress toward a score. "MEASURING" for
    // either would be the wait that never ends.
    case "not-available":
      return "NOT AVAILABLE HERE";
    case "not-scored":
      return "NOT SCORED";
    default:
      return "MEASURING";
  }
}
