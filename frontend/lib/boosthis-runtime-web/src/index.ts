/** Public barrel for @workspace/boosthis-runtime-web. */

// Web Vitals meters (browser-side): first paint, LCP, CLS, INP-ish input
// delay, per-page samples. Call startWebVitals() once at page load.
export {
  startWebVitals,
  getVitals,
  flushPageSample,
  normalizeRouteLabel,
  _resetVitalsForTests,
} from "./vitals";
export type { WebVitals, StartWebVitalsOptions } from "./vitals";
export {
  AI_PROVIDERS,
  DECLARED_PROVIDER_CODE,
  MAX_DECLARED_AI_ENDPOINTS,
  aiProviderCode,
  aiProviderCodeOrDeclared,
  normaliseDeclaredEndpoint,
  setDeclaredAiEndpoints,
  declaredAiEndpointCount,
} from "./aiProviders";

// The ungated startup line, and the refusal of a value that cannot be a
// project key. See .agents/memory/kit-startup-announcement.md.
export {
  announceKitStart,
  announceProjectKey,
  findProjectKeyProblem,
  kitStartupLine,
  projectKeyRefusalLine,
  projectKeyTail,
  warnProjectKeyRefused,
  _resetStartAnnounceForTests,
} from "./startAnnounce";
export type { BadgeState, ProjectKeyProblem } from "./startAnnounce";

// Reconciling that startup line: a page that announced "Registering next." and
// then did not register says so on its own, within a bounded time.
export {
  getRegistrationPromiseState,
  noteRegistrationNeverAttempted,
  REGISTRATION_RECOVERED_LINE,
  _resetRegistrationWatchForTests,
  _setRegistrationVerdictMsForTests,
} from "./registrationWatch";

// Floating Boosthis bubble — display-only, visible by default everywhere
// (live/published sites included; set BOOSTHIS_BUBBLE=0 to hide it).
export {
  mountBubbleIfEnabled,
  unmountBubble,
  resolveBubbleVisibility,
  isDevHost,
  computePagePulse,
  _resetBubbleForTests,
} from "./bubble";

// Telemetry (registered projects only, privacy-safe issue/fix signals).
export {
  enableTelemetry,
  resolveAppName,
  getActiveTelemetryClient,
  getActiveProjectKey,
  getActiveProjectKeyStatus,
  getRegistrationRefusalKind,
  getRegistrationRefusalSentence,
  applyServerFullTelemetry,
  resolveDashboardWebUrl,
  isInstallIdRejected,
  DEFAULT_TELEMETRY_ENDPOINT,
} from "./telemetry";
export {
  resolveProjectKey,
  maskProjectKey,
  describeProjectKeySource,
} from "./projectKey";
export type { ProjectKeySource, ResolvedProjectKey } from "./projectKey";
export {
  collectWebStatusFacts,
  webStatusLines,
  printWebStatus,
} from "./statusPage";
export type { WebStatusFacts } from "./statusPage";
export {
  getKitProject,
  projectDisplay,
  PROJECT_LABEL,
  PROJECT_UNNAMED_TEXT,
  PROJECT_UNKNOWN_TEXT,
  SCORE_CAPTION,
} from "./projectIdentity";
export type { KitProject } from "./projectIdentity";
export type {
  TelemetryOptions,
  TelemetrySample,
  TelemetryClient,
} from "./telemetry";

// Perf snapshot (the full meter page, uploaded for the developer's own AI).
export {
  capturePerfSnapshot,
  uploadPerfSnapshotNow,
  startSnapshotAutoUpload,
  stopSnapshotAutoUpload,
  setSnapshotSubmitter,
  SNAPSHOT_FLUSH_MS,
  nextSnapshotDelayMs,
  MIN_SAMPLES_FOR_AXES,
  _snapshotInternals,
} from "./snapshot";
export type {
  WebSnapshotRow,
  WebAxisResult,
  WebAxisValue,
  WebSnapshotPayload,
  WebSnapshotBuild,
  SnapshotSubmitter,
} from "./snapshot";

// Build identity for the additive, display-only Patch Lag meter (build stamp
// ONLY on web — no dependency inventory, no env vars in a browser).
export {
  resolveBuildIdentity,
  setBuildIdentityOptions,
  normalizeCommit,
  _setBuildIdentityOptionsForTests,
  _resetBuildIdentityForTests,
} from "./buildIdentity";
export type { BuildIdentity, BuildIdentityOptions } from "./buildIdentity";

// Additive RN-parity meter axes (pure scorers + their browser samplers). All
// display-only; none feeds the composite Speed score.
export {
  computeBaselineScore,
  computeBudgetCompliance,
  computeNetworkScore,
  computeIdleEfficiency,
  computeScrollHealth,
  computeFrameFloor,
  computeTimerHealth,
  computeBlockingAsync,
  computeAsyncSlowCallbacks,
  computeEventLoopLag,
  confidenceLevel,
  confidenceRatingFor,
  confidenceCaptionFor,
} from "./meterAxes";
export type {
  AxisRating,
  ConfidenceLevel,
  ConfidenceCaption,
  BudgetCompliance,
  RouteBudgetVerdict,
  RouteSeriesLike,
  BaselineResult,
  NetworkStatsLike,
  NetworkResult,
  IdleStatsLike,
  IdleResult,
  ScrollStatsLike,
  ScrollHealthResult,
  FrameFloorStatsLike,
  FrameFloorResult,
  TimerStatsLike,
  TimerHealthResult,
  BlockingAsyncStatsLike,
  BlockingAsyncResult,
  AsyncSlowCallbackStatsLike,
  AsyncSlowCallbacksResult,
  EventLoopLagStatsLike,
  EventLoopLagResult,
} from "./meterAxes";
export {
  getNetworkStats,
  readNetwork,
  readCallGroups,
  getAbortedCount,
  getInFlightCount,
  getGroupOverflowCount,
  isCallWatchActive,
  HUNG_AFTER_MS,
  MAX_TRACKED_GROUPS,
  type CallGroupRow,
  type CallOutcome,
} from "./networkSampler";
export {
  installCallWatch,
  uninstallCallWatch,
  isCallWatchInstalled,
  isBodyErrorCheckEnabled,
  type CallWatchOptions,
} from "./callWatch";
export {
  deriveCallGroup,
  isSafeCallGroupKey,
  CALL_GROUP_DESTINATIONS,
  CALL_GROUP_METHODS,
  CALL_GROUP_OVERFLOW_KEY,
  CALL_GROUP_UNNAMED_KEY,
  type CallGroupDestination,
  type CallGroupMethod,
} from "./callGroups";
export { getIdleStats, readIdle } from "./idleTracker";
export { getScrollStats, getFrameFloorStats } from "./scrollSampler";
export {
  readTimerHealth,
  readAsyncSlowCallbacks,
  readEventLoopLag,
  isTimerTrackingInstalled,
} from "./timerHealth";
export {
  readWebStorageLatency,
  readWebStorageFailures,
  isLocalStoreTrackingInstalled,
  STORAGE_METER_OPT_IN,
} from "./localStore";
export {
  readSwallowedErrors,
  isSwallowedErrorsInstalled,
} from "./swallowedErrors";
export { readLeakWatch, isLeakWatchInstalled } from "./leakWatch";
export {
  readBackgroundWork,
  installBackgroundWorkTracking,
  uninstallBackgroundWorkTracking,
} from "./backgroundWork";

// In-process sample buffer.
export {
  record,
  recent,
  summary,
  clear,
  setSampleObserver,
} from "./samples";
export type {
  Sample,
  Summary,
  PerRouteSummary,
  SampleObserver,
} from "./samples";

// PII guard — the privacy chokepoint shared (byte-parity) with the Node runtime.
export {
  PII_DENYLIST,
  PIIDetectedError,
  checkNoPII,
  assertNoPII,
  routeLabelHasPII,
  transmitLabelHasPII,
} from "./no-pii";

// Guarded outbound transport.
export { safeTransmit } from "./transmit";
export type { SafeTransmitOptions } from "./transmit";

// Shared scoring model.
export {
  RUNTIME_VERSION,
  SCORE_THRESHOLDS,
  CLS_THRESHOLDS,
  rateValue,
  rateDuration,
  rateCls,
  linearScore,
  ratingFor,
  ratePatchLag,
  patchLagScore,
  PATCH_LAG_DAY_MS,
  PATCH_LAG_RATING_DAYS,
  PATCH_LAG_SCORE_FULL_DAYS,
} from "./thresholds";
export type { Rating, DurationMetric } from "./thresholds";

// Dashboard-first bubble panel helpers (pure, display-only meter rows).
export {
  buildPanelRows,
  barWidth,
  ratingLabel,
  axisCaption,
  PANEL_AXES,
  ONLY_IF_PRESENT_AXES,
} from "./bubblePanel";
export type { PanelMeterRow, PanelRating } from "./bubblePanel";

// In-page "Connect to your account" sign-in flow (hosted auth endpoints).
export {
  login,
  verifyLoginOtp,
  resendLoginOtp,
  claimInstall,
  claimInstallWithTelemetry,
  setInstallTelemetry,
  logout,
  getStoredAccount,
  saveStoredAccount,
  clearStoredAccount,
  AccountError,
} from "./account";
export type { StoredAccount, LoginResult, ClaimResult } from "./account";

// Dashboard-first Terms helper (hosted /terms link + fire-and-forget record).
export {
  TERMS_VERSION,
  resolveTermsUrl,
  hasAcceptedCurrentTerms,
  getTermsAcceptance,
  recordTermsAccepted,
} from "./terms";
export type { TermsAcceptance } from "./terms";

// The 58-rule web performance pack (detection metadata only — fixes are
// fetched per-rule from the server, invite-key gated).
export {
  BOOSTHIS_CHECKLIST,
  CHECKLIST_COUNT,
  getRule,
  listRules,
} from "./checklist";
export type { BoosthisChecklistEntry, Category } from "./checklist";

// Kill switch.
export { isBoosthisDisabled } from "./runtimeFlags";

// Server-authority entitlement kill-switch (VAULT contract): on-launch /
// on-open forced checks, sticky revoked/unpaid/paused, 7-day offline grace.
export {
  isRuntimeInert,
  isActivated,
  getEntitlementStatus,
  getEntitlementMessage,
  getEntitlementGateKind,
  subscribeEntitlement,
  checkEntitlementNow,
  forceEntitlementCheck,
  startEntitlementCheckin,
  stopEntitlementCheckin,
  type EntitlementStatus,
  type EntitlementGateKind,
  type EntitlementCheckinConfig,
} from "./killSwitch";
export { computePanelLock, computeInstallIdRejectedNotice } from "./bubble";
export type { PanelLock } from "./bubble";

// Live read config (Node-side MCP path).
export {
  configureLiveData,
  isLiveDataConfigured,
  getLiveSnapshot,
} from "./liveData";
export type { LiveCreds } from "./liveData";

// Full-stack trace tag (Stage 1: propagation). Adopt/mint a correlation id and
// forward it to the next hop so one browser action stitches RN → Node → Python.
export {
  TRACE_HEADER,
  TRACE_ID_RE,
  ELAPSED_HEADER,
  isValidTraceId,
  newTraceId,
  sanitizeTraceId,
  traceHeaders,
  sanitizeTraceElapsed,
} from "./trace";

// Full-stack trace causality (Stage 3: span parentage). A span carries its own
// identity and the identity of the call that caused it, so the waterfall nests
// and the view can name the hop RESPONSIBLE for the end-to-end time — not
// merely the longest one. `traceFetch` mints one per call; wrap
// several calls in `runInSpan` to name a common caller for all of them.
export {
  PARENT_HEADER,
  SPAN_ID_RE,
  isValidSpanId,
  newSpanId,
  sanitizeSpanId,
  adoptParentSpanId,
  currentSpanId,
  beginSpan,
  runInSpan,
} from "./spanScope";
export type { SpanHandle } from "./spanScope";

// Full-stack trace tag (Stage 2: span emission). Privacy-safe browser spans,
// gated behind the same meter-share allow as the snapshot mirror.
export {
  setSpanSubmitter,
  enqueueSpan,
  flushSpansNow,
  startSpanAutoFlush,
  stopSpanAutoFlush,
  clearBufferedSpans,
  spanLabel,
  rateSpanDuration,
  traceFetch,
  _spanInternals,
} from "./spanEmitter";
export type {
  TraceSpan,
  SpanLayer,
  SpanRating,
  SpanSubmitter,
} from "./spanEmitter";

// Crash reporter (privacy-safe, code-derived-only signatures). Feeds the
// "what might crash" read the same way the RN/Node/Python runtimes do.
export {
  setCrashSubmitter,
  reportRenderError,
  installCrashHandlers,
  uninstallCrashHandlers,
  crashCount,
  _crashInternals,
} from "./crashReporter";
export type {
  CrashReportPayload,
  CrashFrameData,
  CrashKind,
} from "./crashReporter";

// Long-task stats surface the "stability" axis (main-thread blocking) reads.
export { getLongTaskStats } from "./vitals";
export type { LongTaskStats } from "./vitals";
// Paint readiness (FCP) + animation smoothness (LoAF) axis inputs.
export { getFcpMs, getLoafStats } from "./vitals";
export type { LoafStats } from "./vitals";
export { getDeadClickStats } from "./vitals";
export type { DeadClickStats } from "./vitals";

// Route reachability — declare the site's routes and the runtime names any
// registered route never reached this session (the web analogue of the RN
// circuit map's unreachable-screen note). Router-agnostic + on-device only.
export {
  registerWebRoutes,
  getRegisteredWebRoutes,
  recordVisit,
  getUnreachableRoutes,
  getRouteReachabilityStats,
} from "./vitals";
export type { RouteReachabilityStats } from "./vitals";

// The whole route list — hand the kit your router and it lists the site's
// routes itself, so the map draws pages nobody has opened yet (marked "not
// seen", never "dead"). Vue Router and React Router are understood; anything
// else is reported as a router we could not ask. Declared routes and
// router-read routes merge, neither overwriting the other. Switchable off.
export {
  registerRouter,
  setRouteListEnabled,
  routeListEnabled,
  routeListReport,
  readRouterRoutes,
  safeRoutePattern,
  MAX_ROUTE_LIST_ENTRIES,
  WEB_ROUTE_SOURCE_WORDS,
  _resetRouteInventoryForTests,
} from "./routeInventory";
export type {
  RouteListEntry,
  RouteListOrigin,
  RouteListReport,
  RouteListStatus,
  RouterRead,
  WebRouteSource,
} from "./routeInventory";

// Circuit-lens stats — count-only navigation-loop + request-burst picture
// (the web analogue of the RN circuit map's live detectors). On-device only;
// read by the dev bubble, never uploaded.
export { getCircuitStats } from "./vitals";
export type { CircuitStats } from "./vitals";

// Control map — "press this, it opens that", built from the click watch that
// already runs. A control is named ONLY by a code-defined test id; its text,
// accessibility label, value and position are never read. On-device only:
// read by the dev bubble and the status readout, never uploaded.
export {
  getControlMap,
  controlMapPanelText,
  controlMapStatusLine,
  CONTROL_ID_ATTRIBUTES,
} from "./controlMap";
export type {
  ControlMapReport,
  ControlEdge,
  ControlDeadEnd,
} from "./controlMap";

// Control census — ask the RENDERED PAGE what controls it has, the same way
// the route list asks the router for its routes. Counts and STRUCTURAL
// handles only: a control is identified by its position in the tree, never by
// its text, label, value or anything a person typed. Rides the snapshot under
// the gates that already apply; switchable off, and the block still travels
// saying "off" so that can be told from a kit too old to have it.
export {
  controlCensusReport,
  controlCensusForSnapshot,
  controlCensusEnabled,
  setControlCensusEnabled,
  controlCensusPanelText,
  controlCensusStatusLine,
  censusBoundSentence,
  CONTROL_CENSUS_SELECTOR,
  CONTROL_KIND_WORDS,
  CONTROL_LEADS_WORDS,
  CONTROL_CENSUS_BOUND_WORDS,
  MAX_CENSUS_ENTRIES,
  MAX_CENSUS_SCAN,
  CENSUS_BUDGET_MS,
  _resetControlCensusForTests,
} from "./controlCensus";
export type {
  ControlCensusReport,
  ControlCensusEntry,
  ControlCensusStatus,
  ControlCensusBound,
  ControlKind,
  ControlLeads,
} from "./controlCensus";
export {
  controlHandle,
  controlStructuralPath,
  isControlHandle,
  CONTROL_HANDLE_TAGS,
  CONTROL_HANDLE_DEPTH,
  CONTROL_HANDLE_MAX_ORDINAL,
  CONTROL_HANDLE_RE,
} from "./controlHandle";

// The self-drawing page map — pages and the links between them, accumulated
// across reloads on THIS device from the navigation the kit already observes.
// ON-DEVICE ONLY: labels are read by the kit's own bubble and nothing else,
// and no part of this rides the snapshot, spans, crashes or any upload path.
export {
  getPageMapStats,
  getPageMapGraph,
  savePageMap,
  loadPageMap,
  MAX_MAP_NODES,
  MAX_MAP_EDGES,
  MAX_MAP_STORED_BYTES,
  _resetPageMapForTests,
} from "./pageMap";
export type {
  PageMapStats,
  PageMapNode,
  PageMapEdge,
  PageMapPersistence,
} from "./pageMap";

// Additive web-meter batch getters (9 new axes). All display-only; each stays
// absent until it has honest data (unsupported APIs => absent forever).
export {
  getLoafCauseStats,
  getBlockingTimeStats,
  getBfCacheStats,
  getInputReadinessStats,
  getSoftNavStats,
  getMemoryTrendStats,
  getResourceBloat,
  getRageClickStats,
  sampleMemoryTrendNow,
} from "./vitals";
export type {
  LoafCauseStats,
  BlockingTimeStats,
  BfCacheStats,
  InputReadinessStats,
  SoftNavStats,
  MemoryTrendStats,
  ResourceBloatStats,
  RageClickStats,
} from "./vitals";

// Tail-latency + long-task thresholds (resilience + stability axes).
export { TAIL_RATIO_THRESHOLDS, LONG_TASK_THRESHOLDS } from "./thresholds";

/* ─── Recurring-problem reporting ──────────────────────────────────────── */

// The shared problem vocabulary (browser copy) — the closed set of kinds every
// kit spells identically, so a problem seen in a visitor's browser and the same
// problem seen on a back end meet in one place.
export {
  PROBLEM_KINDS,
  PROBLEM_KIND_FOLD,
  isProblemKind,
} from "./problemKinds";
export type { ProblemKind } from "./problemKinds";

// On-device accumulation of recurring problems + fix outcomes. Uploads are
// wired automatically by `enableTelemetry`; these exports exist so a host can
// read what the kit has locally and so the live proof can drive one pass.
export {
  signatureFor,
  setCandidateSubmitter,
  setResolutionSubmitter,
  ingestFindings,
  listCandidateRules,
  listSurfaceable,
  promoteCandidateLocal,
  clearAllCandidates,
} from "./candidateRules";
export type { CandidateRule, CrossCuttingFinding } from "./candidateRules";

// The adapter that turns the kit's OWN live readings into findings, plus the
// loop that reports them (cadence + a keepalive flush on `pagehide`).
export {
  collectFindings,
  reportNow,
  startReporting,
  stopReporting,
  isReportingStarted,
} from "./reporting";

// NOTE: ./proposeRule is intentionally NOT re-exported here. It uses
// `node:crypto` and is imported ONLY by the Node-side MCP entrypoints
// (mcp.ts / mcp-stdio.ts), never by browser-shipped code.
