import { sayAfterStartupLine } from "./startAnnounce";

export type SourceCauseName =
  | "core" | "longWork" | "heap" | "network" | "device" | "reports"
  | "idle" | "fonts" | "resourceTiming" | "prerender" | "isolation";
type Probe = () => string | null;
const overrides: Partial<Record<SourceCauseName, Probe>> = {};
let checked = false;

function forced(name: SourceCauseName): string | null | undefined {
  const probe = overrides[name];
  if (!probe) return undefined;
  try { return probe(); } catch { return `${name} capability probe failed`; }
}

function entryTypes(): string[] | null {
  try {
    if (typeof PerformanceObserver !== "function") return null;
    return Array.from(PerformanceObserver.supportedEntryTypes ?? []);
  } catch { return null; }
}

export function coreWebVitalsMissingCause(): string | null {
  const f = forced("core"); if (f !== undefined) return f;
  const types = entryTypes();
  if (!types) return "PerformanceObserver is missing";
  const missing = ["largest-contentful-paint", "layout-shift", "event"].filter((x) => !types.includes(x));
  return missing.length ? `PerformanceObserver lacks ${missing.join(", ")}` : null;
}
export function longWorkMissingCause(): string | null {
  const f = forced("longWork"); if (f !== undefined) return f;
  const types = entryTypes();
  if (!types) return "PerformanceObserver is missing";
  const missing = ["longtask", "long-animation-frame"].filter((x) => !types.includes(x));
  return missing.length ? `PerformanceObserver lacks ${missing.join(", ")}` : null;
}
export function heapMissingCause(): string | null {
  const f = forced("heap"); if (f !== undefined) return f;
  try { return typeof (performance as Performance & { memory?: unknown }).memory === "object" ? null : "performance.memory is missing"; }
  catch { return "performance.memory is unreadable"; }
}
export function networkMissingCause(): string | null {
  const f = forced("network"); if (f !== undefined) return f;
  try { return "connection" in navigator ? null : "navigator.connection is missing"; }
  catch { return "navigator.connection is unreadable"; }
}
export function deviceMissingCause(): string | null {
  const f = forced("device"); if (f !== undefined) return f;
  try {
    const n = navigator as Navigator & { deviceMemory?: number };
    return typeof n.hardwareConcurrency !== "number" && typeof n.deviceMemory !== "number"
      ? "navigator.hardwareConcurrency and navigator.deviceMemory are missing" : null;
  } catch { return "device capacity is unreadable"; }
}
export function reportsMissingCause(): string | null {
  const f = forced("reports"); if (f !== undefined) return f;
  return typeof (globalThis as { ReportingObserver?: unknown }).ReportingObserver === "function" ? null : "ReportingObserver is missing";
}
export function idleMissingCause(): string | null {
  const f = forced("idle"); if (f !== undefined) return f;
  return typeof (globalThis as { requestIdleCallback?: unknown }).requestIdleCallback === "function" ? null : "requestIdleCallback is missing";
}
export function fontsMissingCause(): string | null {
  const f = forced("fonts"); if (f !== undefined) return f;
  try { return "fonts" in document ? null : "document.fonts is missing"; } catch { return "document.fonts is unreadable"; }
}
export function resourceTimingMissingCause(): string | null {
  const f = forced("resourceTiming"); if (f !== undefined) return f;
  try {
    const p = (globalThis as { PerformanceResourceTiming?: { prototype?: object } }).PerformanceResourceTiming?.prototype;
    return p && "nextHopProtocol" in p && "renderBlockingStatus" in p
      ? null : "Resource Timing lacks nextHopProtocol or renderBlockingStatus";
  } catch { return "Resource Timing fields are unreadable"; }
}
export function prerenderMissingCause(): string | null {
  const f = forced("prerender"); if (f !== undefined) return f;
  try {
    const navProto = (globalThis as { PerformanceNavigationTiming?: { prototype?: object } }).PerformanceNavigationTiming?.prototype;
    return ("prerendering" in document) || !!(navProto && "activationStart" in navProto)
      ? null : "document.prerendering and activationStart are missing";
  } catch { return "prerender activation is unreadable"; }
}
export function isolationMissingCause(): string | null {
  const f = forced("isolation"); if (f !== undefined) return f;
  return "crossOriginIsolated" in globalThis ? null : "crossOriginIsolated is not exposed";
}

const PARTS: Array<[SourceCauseName, string, string]> = [
  ["core", "Core Web Vitals", "LCP, layout shift and interaction data come from PerformanceObserver"],
  ["longWork", "long tasks and long animation frames", "blocking work comes from performance entries"],
  ["heap", "JavaScript heap size", "heap usage comes from performance.memory"],
  ["network", "network quality", "connection quality comes from navigator.connection"],
  ["device", "device capacity", "CPU and memory capacity come from navigator"],
  ["reports", "browser reports", "interventions and deprecations come from ReportingObserver"],
  ["idle", "idle opportunity", "idle time comes from requestIdleCallback"],
  ["fonts", "font readiness", "font state comes from document.fonts"],
  ["resourceTiming", "protocol mix and render-blocking", "resource protocol and blocking state come from Resource Timing"],
  ["prerender", "prerender activation", "activation state comes from the document and Navigation Timing"],
  ["isolation", "cross-origin isolation", "isolation state comes from crossOriginIsolated"],
];

export function unreadableSourcesLine(causes: Partial<Record<SourceCauseName, string | null>>): string | null {
  const parts = PARTS.flatMap(([key, meter, clause]) => causes[key] ? [`${meter} (${causes[key]}): ${clause}`] : []);
  if (!parts.length) return null;
  const tail = `. ${parts.length === 1 ? "That one meter stays" : "Those meters stay"} absent for the life of this page; nothing else about Boosthis is affected.`;
  return parts.length === 1
    ? `[boosthis] This browser cannot report ${parts[0]}${tail}`
    : `[boosthis] This browser cannot report ${parts.length} of Boosthis's readings — ${parts.join("; ")}${tail}`;
}

export function announceUnreadableSources(): void {
  if (checked) return;
  // Claim the one process-wide judgement before scheduling it. The startup
  // line itself waits one turn for a key supplied on the next statement; this
  // notice must wait behind that turn so the startup line and any key refusal
  // remain first and second. A second start cannot schedule another judgement.
  checked = true;
  const judgeAndSay = (): void => {
    try {
      const probes: Record<SourceCauseName, Probe> = {
        core: coreWebVitalsMissingCause, longWork: longWorkMissingCause,
        heap: heapMissingCause, network: networkMissingCause, device: deviceMissingCause,
        reports: reportsMissingCause, idle: idleMissingCause, fonts: fontsMissingCause,
        resourceTiming: resourceTimingMissingCause, prerender: prerenderMissingCause,
        isolation: isolationMissingCause,
      };
      const causes: Partial<Record<SourceCauseName, string | null>> = {};
      for (const key of Object.keys(probes) as SourceCauseName[]) causes[key] = probes[key]();
      const line = unreadableSourcesLine(causes);
      if (line) { try { sayAfterStartupLine(line); } catch { /* never disturb host */ } }
    } catch { /* diagnostic only */ }
  };
  try {
    if (typeof setTimeout === "function") {
      setTimeout(judgeAndSay, 0);
      return;
    }
  } catch {
    // Fall through: an unusual host must still get the notice synchronously.
  }
  judgeAndSay();
}

export function _setSourceNoticeProbeForTests(name: SourceCauseName, probe: Probe | null): void {
  if (probe) overrides[name] = probe; else delete overrides[name];
}
export function _resetSourceNoticeForTests(): void {
  checked = false;
  for (const key of Object.keys(overrides) as SourceCauseName[]) delete overrides[key];
}