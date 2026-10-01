/**
 * The Boosthis checklist — Web edition.
 *
 * 82 browser-specific rules covering the most common web-vitals pitfalls:
 * render-blocking resources, layout shift, main-thread contention, bundle
 * bloat, and SPA lifecycle leaks. Same shape as the Node checklist
 * (`lib/boosthis-runtime-node/src/checklist.ts`) so cross-runtime tooling can
 * treat any rule entry uniformly:
 *
 *   { id, title, whenToApply, evidence, category, languages }
 *
 * Detection stays fully on-device (this file ships in the client); the
 * prescriptive fix text for every rule lives server-only in
 * `lib/boosthis-fixes` and is fetched one rule at a time.
 */

export type Category = "case-study" | "industry" | "operational";

export interface BoosthisChecklistEntry {
  id: string;
  title: string;
  /** When this rule applies — what code shape triggers a check. */
  whenToApply: string;
  /** Files / docs / incidents where this rule was validated. */
  evidence: ReadonlyArray<string>;
  category: Category;
  /** Always ["web"] for this pack. Present for cross-runtime symmetry. */
  languages: ReadonlyArray<"web">;
}

export const BOOSTHIS_CHECKLIST: ReadonlyArray<BoosthisChecklistEntry> = [
  // ─────────── Case Study (13) — classic real-world web-vitals incidents ───────────
  {
    id: "client-registration-retry-no-cooldown",
    title: "Cool down failed registration/handshake retries — never re-attempt on every flush",
    whenToApply:
      "A one-time 'first contact' call (install/device registration, consent, enrollment, license activation, first token mint) is lazily retried from a hot path — every beacon/upload flush, visibility change, timer tick, or route change — until it succeeds, with no cool-down and no special handling for HTTP 429. Symptom: a client that cannot finish registering (offline at setup, pending invite, busy server) silently re-attempts dozens of times an hour; behind a shared proxy/NAT egress IP those retries drain the server's per-IP registration rate bucket and starve OTHER clients' genuine first registrations — the failure surfaces on a different machine than the bug. Distinct from fetch-retry-storm (a tight loop retried too fast): each attempt here is cadence-driven and looks harmless in isolation, so the hammering hides in normal traffic.",
    evidence: [
      "Boosthis production incident (Jul 2026) — per-flush consent retries starved a shared per-IP registration rate bucket and blocked new installs",
      "Google SRE Book — Handling Overload (retry amplification)",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "web-install-id-reuse",
    title:
      "Never reuse a registration/install ID across installs — mint a fresh UUID each time",
    whenToApply:
      "The client sends a registration/install/device/instance ID that was hard-coded, copied from docs or another app, or persisted once and then reused for every later install (a constant string, a value baked into config, or an id kept in `localStorage` and handed to a brand-new install). Symptom: if the ORIGINAL install ever gets orphaned server-side — consented but its credential/token lost — every later install that presents the SAME id inherits that dead identity and gets a credential-less 'success' forever, a deadlock no server lever can break because the server can't tell two installs apart. Rule: mint a FRESH unique id (a real `crypto.randomUUID()`) per logical install/instance and treat it as identity, never as configuration — never copy an id from documentation, another project, or a previous install. Distinct from the callback-latch bug: this is identity COLLISION at the source, not a missed re-issue.",
    evidence: [
      "Boosthis production incident (Jul 2026) — a reused hard-coded install id inherited an orphaned consented-but-tokenless identity, deadlocking every later install",
      "RFC 4122 — UUIDs are meant to be generated fresh per instance, never reused as a shared constant",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "web-credential-callback-value-change",
    title:
      "Credential-issued callbacks must fire on VALUE change, never once-only",
    whenToApply:
      "An `onTokenIssued`/`onCredentialChanged`-style callback (or any 'credential issued/changed' notification the host persists from) is gated by a fired-once latch — a `hasFired` boolean, a one-shot flag, or a `useEffect` with an empty dependency array — so it delivers the FIRST value and never again. Symptom: on restart the host restores a stale persisted token, the server rotates or re-issues a different one, but the callback never re-fires because it 'already fired', so the host keeps persisting and presenting the stale credential and 401-loops forever. Rule: compare each new value against the LAST DELIVERED value and fire whenever they differ — including right after restore-from-storage, where the restored value must be reconciled against a freshly issued one — never gate on 'already fired once'. Distinct from install-id reuse (identity collision at the source): here the identity is fine but the re-issue never propagates.",
    evidence: [
      "Boosthis production incident (Jul 2026) — a fired-once onTokenIssued latch never re-fired after a server token rotation, so the host 401-looped on a stale persisted credential",
      "React docs — Synchronizing with Effects: fire on value change, not once on mount",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "web-fire-and-forget-system-call",
    title: "Always handle fire-and-forget async native/system calls",
    whenToApply:
      "An async browser/system API — `navigator.clipboard.writeText`, the Web Share / Vibration / Wake Lock APIs, IndexedDB, `caches`, `Notification.requestPermission`, or a storage write — is called awaitless from a synchronous event handler (a click/submit handler that fires `navigator.clipboard.writeText(…)` and moves on) with no `.catch` and no surrounding `try`/`catch`. Symptom: when the call rejects (permission denied, not focused, quota exceeded, insecure context) it surfaces as an Uncaught (in promise) error caught only by the global `unhandledrejection` handler — a console error / crash-report noise in the wild and a silent no-op for the user. Rule: every fire-and-forget promise, especially inside a sync handler, must get a `.catch` (or run inside `try`/`catch` in an `async` handler); the call being 'unimportant' is exactly why its failure must be swallowed DELIBERATELY, not left to the global rejection handler.",
    evidence: [
      "Boosthis production incident (Jul 2026) — awaitless clipboard/haptics/storage calls from sync handlers surfaced as unhandled promise rejections instead of a deliberate no-op",
      "MDN — Promise: always attach a rejection handler to fire-and-forget promises to avoid unhandledrejection",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "render-blocking-scripts",
    title: "Load scripts with `defer`/`async` — never bare in `<head>`",
    whenToApply:
      "A `<script src=…>` tag sits in `<head>` without `defer` or `async` (or `document.write` injects one). Symptom: first paint waits for the full script download + parse + execute, so TTFB looks fine but LCP is seconds late on slow connections.",
    evidence: [
      "web.dev — Eliminate render-blocking resources",
      "MDN — script defer/async attributes",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "unoptimized-lcp-image",
    title: "Preload and prioritize the LCP hero image",
    whenToApply:
      "The largest above-the-fold image is discovered late (CSS background, client-rendered `<img>`, or lazy-loaded) and ships at full resolution without `fetchpriority=\"high\"` or a `<link rel=\"preload\">`. Symptom: LCP lands 1–3s after first paint because the hero image only starts downloading after the JS or CSS that references it.",
    evidence: [
      "web.dev — Optimize Largest Contentful Paint",
      "Chrome docs — fetchpriority",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "layout-shift-media",
    title: "Reserve space for images, ads, and embeds",
    whenToApply:
      "`<img>`, `<video>`, `<iframe>`, or ad slots render without explicit `width`/`height` attributes or a CSS `aspect-ratio`, so content below jumps when they load. Symptom: CLS spikes above 0.1 and users mis-tap links that moved under their finger.",
    evidence: [
      "web.dev — Optimize Cumulative Layout Shift",
      "MDN — aspect-ratio",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "font-loading-flash",
    title: "Use `font-display: swap` and preload critical webfonts",
    whenToApply:
      "An `@font-face` rule loads a webfont without `font-display`, so browsers hide text (FOIT) for up to 3s while the font downloads. Symptom: LCP text nodes count only after the font arrives, and users stare at invisible headlines on slow networks.",
    evidence: [
      "web.dev — font-display",
      "MDN — @font-face descriptor reference",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "huge-initial-bundle",
    title: "Code-split routes — never ship one megabyte-scale bundle",
    whenToApply:
      "The app builds to a single JS bundle over ~300kB gzipped with no dynamic `import()` / `React.lazy` route splitting. Symptom: parse + compile alone eats hundreds of milliseconds of main-thread time on mid-range phones, delaying both LCP and the first interaction.",
    evidence: [
      "web.dev — Reduce JavaScript payloads with code splitting",
      "React docs — lazy",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "third-party-tag-pileup",
    title: "Load analytics/ads/chat tags async and after first paint",
    whenToApply:
      "Multiple third-party tags (gtag, fbq, chat widgets, session recorders) load synchronously or in `<head>`, each pulling its own chain of sub-resources. Symptom: main-thread long tasks stack up before the page is interactive, and vitals degrade release-over-release as tags accrete.",
    evidence: [
      "web.dev — Loading third-party JavaScript",
      "Chrome docs — Third-party facades",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "long-tasks-block-input",
    title: "Break up >50ms main-thread tasks around interactions",
    whenToApply:
      "A click/keypress handler (or the render work it triggers) runs a single synchronous block longer than ~50ms — big loops, synchronous state cascades, heavy DOM reads/writes. Symptom: INP goes poor; taps feel dead because the event can't be processed until the current task yields.",
    evidence: [
      "web.dev — Optimize INP: break up long tasks",
      "MDN — scheduler.yield / setTimeout chunking",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "client-fetch-waterfall",
    title: "Parallelize independent fetches — kill request waterfalls",
    whenToApply:
      "Page data loads as a chain of dependent awaits (`await fetch(a)` then `await fetch(b)` then render), or each nested component fires its own fetch only after it mounts. Symptom: time-to-content is the SUM of round-trips instead of the max; the network panel shows a staircase.",
    evidence: [
      "web.dev — Preload critical requests",
      "React docs — fetch-on-render vs render-as-you-fetch",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "unvirtualized-long-list",
    title: "Virtualize lists beyond a few hundred rows",
    whenToApply:
      "A list/table renders every row of a large dataset into the DOM at once (thousands of nodes via `.map()` with no windowing library like react-window). Symptom: initial render and every subsequent update cost seconds of layout/paint, and scrolling stutters on anything but a desktop.",
    evidence: [
      "web.dev — Virtualize large lists",
      "react-window README",
    ],
    category: "case-study",
    languages: ["web"],
  },

  // ─────────── Industry (8) — published best practice ───────────
  {
    id: "images-not-lazy-loaded",
    title: "Lazy-load below-the-fold images",
    whenToApply:
      "Offscreen images load eagerly (no `loading=\"lazy\"` on `<img>`/`<iframe>` below the fold) while competing for bandwidth with the LCP resource. Symptom: dozens of image requests fire on page load for content the user may never scroll to, delaying the hero image.",
    evidence: [
      "web.dev — Browser-level image lazy loading",
      "MDN — loading attribute",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "missing-text-compression",
    title: "Serve text assets with Brotli or gzip",
    whenToApply:
      "HTML, JS, CSS, or JSON responses ship uncompressed (no `Content-Encoding: br`/`gzip`), usually because a CDN or custom server skipped compression config. Symptom: transfer sizes 3–10× larger than needed; every vitals metric degrades on slow connections.",
    evidence: [
      "web.dev — Minify and compress network payloads",
      "MDN — Content-Encoding",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "no-cache-headers-static",
    title: "Cache hashed static assets immutably",
    whenToApply:
      "Fingerprinted build assets (`app.3f9c.js`) are served without `Cache-Control: max-age=31536000, immutable`, or unhashed filenames force short cache lifetimes on everything. Symptom: repeat visits re-download the entire bundle; returning-user LCP is as slow as first-visit.",
    evidence: [
      "web.dev — Love your cache / HTTP caching",
      "MDN — Cache-Control immutable",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "missing-preconnect-hints",
    title: "Preconnect to critical cross-origin hosts",
    whenToApply:
      "The page fetches fonts, APIs, or media from another origin without `<link rel=\"preconnect\">` (or `dns-prefetch`), so DNS + TLS + TCP setup happens inside the critical path. Symptom: waterfall shows 100–500ms of connection setup before the first byte of a critical cross-origin resource.",
    evidence: [
      "web.dev — Establish network connections early with preconnect",
      "MDN — rel=preconnect",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "dom-size-explosion",
    title: "Keep the DOM under ~1,500 nodes",
    whenToApply:
      "The page builds a very large or very deep DOM (thousands of nodes, >32 depth — often via unbounded `.map()` rendering, `innerHTML` dumps, or hidden-but-mounted views). Symptom: every style recalculation and layout pass touches the whole tree, so even small updates cause long tasks.",
    evidence: [
      "Lighthouse audit — Avoid an excessive DOM size",
      "web.dev — DOM size and interactivity",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "animation-triggers-layout",
    title: "Animate `transform`/`opacity`, never layout properties",
    whenToApply:
      "CSS or JS animates `top`/`left`/`width`/`height`/`margin` (e.g. `el.style.left` in a rAF loop), forcing layout + paint every frame instead of a compositor-only transform. Symptom: animations jank at <60fps and burn main-thread time that interactions need.",
    evidence: [
      "web.dev — Stick to compositor-only properties",
      "MDN — CSS performance: animating transform",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "legacy-polyfill-overload",
    title: "Stop shipping legacy polyfills to modern browsers",
    whenToApply:
      "The build targets ancient browsers globally (broad browserslist, blanket core-js/babel polyfill imports) so every modern visitor downloads and parses transpiled helpers + polyfills they don't need. Symptom: bundle is 20–40% bigger than the same code built with modern targets.",
    evidence: [
      "web.dev — Serve modern code to modern browsers",
      "babel docs — @babel/preset-env useBuiltIns",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "unused-code-shipped",
    title: "Strip unused JS/CSS from the critical bundle",
    whenToApply:
      "Coverage tooling shows large fractions (>40%) of shipped JS/CSS never execute on the landing page — whole component libraries, icon packs, or CSS frameworks imported wholesale without tree-shaking. Symptom: parse/compile cost and download size scale with code the page never runs.",
    evidence: [
      "web.dev — Remove unused code",
      "Chrome DevTools — Coverage panel",
    ],
    category: "industry",
    languages: ["web"],
  },

  // ─────────── Operational (8) — production/SPA lifecycle lessons ───────────
  {
    id: "sync-storage-hot-path",
    title: "Keep `localStorage` and sync XHR off hot paths",
    whenToApply:
      "Render or input handlers read/parse large `localStorage`/`sessionStorage` blobs synchronously (`JSON.parse(localStorage.getItem(…))` per render), or legacy code issues a synchronous XHR. Symptom: main thread stalls on storage I/O during exactly the moments users are interacting.",
    evidence: [
      "web.dev — localStorage is synchronous",
      "MDN — Synchronous XHR deprecation",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "listener-leak-spa",
    title: "Clean up listeners and subscriptions on SPA navigation",
    whenToApply:
      "Components/pages call `addEventListener`, `setInterval`, or subscribe to stores/sockets without a matching cleanup on unmount, so every SPA navigation accretes zombie handlers. Symptom: the app gets slower the longer it stays open; memory and handler counts climb across navigations.",
    evidence: [
      "React docs — useEffect cleanup",
      "Chrome DevTools — Memory: detached nodes",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "excessive-rerender-storm",
    title: "Scope state so updates don't re-render the whole tree",
    whenToApply:
      "High-frequency state (input text, scroll position, timers, websocket ticks) lives in a root-level context/store, so every change re-renders the entire component tree. Symptom: typing or streaming updates peg the main thread; profiler shows the whole app re-rendering per keystroke.",
    evidence: [
      "React docs — createContext pitfalls / memo",
      "web.dev — Optimize long tasks from rendering",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "hydration-mismatch-rework",
    title: "Fix SSR hydration mismatches — they double render cost",
    whenToApply:
      "Server-rendered HTML disagrees with the client render (locale/date/random content, browser-only branches), so hydration warns and React re-renders the tree from scratch. Symptom: the page paints fast then stalls or flickers as hydration throws the server HTML away and rebuilds it.",
    evidence: [
      "React docs — hydrateRoot mismatch behavior",
      "web.dev — Rehydration pitfalls",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "no-error-budget-vitals",
    title: "Monitor field web vitals, not just lab scores",
    whenToApply:
      "The team ships against Lighthouse lab runs only — no PerformanceObserver/web-vitals field data from real users — so regressions on real devices/networks go unnoticed until users complain. Symptom: lab says 95, field p75 says poor; no alert fires when a release degrades LCP or INP.",
    evidence: [
      "web.dev — Lab vs field data",
      "web-vitals library README",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "service-worker-stale-cache",
    title: "Give the service worker an explicit update strategy",
    whenToApply:
      "A service worker caches app shell/bundles with no versioned cache names, no `skipWaiting`/refresh flow, or cache-first on HTML, so users run week-old bundles (or the SW double-fetches everything). Symptom: fixed bugs 'come back' for users, and the network panel shows requests served twice.",
    evidence: [
      "web.dev — Service worker lifecycle",
      "MDN — Cache.delete / cache versioning",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "websocket-reconnect-storm",
    title: "Back off websocket reconnects exponentially",
    whenToApply:
      "A `new WebSocket` wrapper reconnects immediately in `onclose` with no exponential backoff or jitter, so a server blip makes every open tab hammer reconnects in a tight loop. Symptom: CPU spikes in idle tabs, the server sees a reconnect stampede, and the main thread burns on connection churn.",
    evidence: [
      "MDN — WebSocket close handling",
      "Cloud provider guidance — reconnect with exponential backoff and jitter",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "beacon-blocking-unload",
    title: "Use `sendBeacon`/`keepalive` for exit analytics",
    whenToApply:
      "Analytics or state-save calls fire from `beforeunload`/`unload` via fetch or sync XHR, delaying navigation and losing data when the page dies first. Symptom: leaving the page feels sticky, and exit events silently vanish on mobile where unload handlers rarely run.",
    evidence: [
      "MDN — Navigator.sendBeacon",
      "web.dev — Page Lifecycle API: never use unload",
    ],
    category: "operational",
    languages: ["web"],
  },
  // ─────────── Cross-runtime parity (2) — ReDoS + fetch retry-storm ───────────
  {
    id: "web-regex-redos",
    title: "Never run a catastrophic-backtracking regex on the main thread",
    whenToApply:
      "Client-side code runs a regex over user- or network-controlled text (form validation, URL/query parsing, a router matching `location.pathname`, markdown/rich-text sanitizing, parsing pasted content) where the pattern nests or adjoins unbounded quantifiers over an overlapping class — `(a+)+`, `(.*)*`, `(x+)+y`, a repeated group that can match the same text two ways, or an unanchored `.*` before an alternation. On a crafted or accidental input the engine backtracks super-linearly and the SINGLE UI thread locks up: the page freezes, clicks and scrolls stop registering, and the tab can be killed. Symptom: one specific string (often a long run of one character with no terminator) pins the CPU at 100% and the whole page goes unresponsive. Distinct from a merely slow but linear regex — this is exponential blowup a short input can trigger.",
    evidence: [
      "OWASP — Regular expression Denial of Service (ReDoS)",
      "MDN — catastrophic backtracking; the main thread blocks on regex",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "fetch-retry-storm",
    title:
      "Cap fetch/XHR retries with backoff + jitter — never a tight retry loop",
    whenToApply:
      "A browser data call (`fetch`, `axios`, an SDK/GraphQL client, or a `useEffect` that refires on error) retries on failure with no exponential backoff, no jitter, and no max-attempts ceiling — an immediate re-`fetch` in a `.catch`, a `while`/recursion that retries as fast as it resolves, or an effect whose dependency flips on every failed response so it loops forever. Symptom: the moment an API slows or returns 5xx, every open tab hammers it flat out — a client-side thundering herd that turns a brief blip into a sustained outage, drains battery and mobile data, and can trip the server's own rate limiter so even recovered requests keep failing. Distinct from websocket-reconnect-storm (socket lifecycle): this is request/response retry amplification.",
    evidence: [
      "AWS Architecture Blog — Exponential Backoff And Jitter",
      "Google SRE Book — Handling Overload (retry amplification)",
    ],
    category: "industry",
    languages: ["web"],
  },
  // ─────────── Navigation reachability (2) — dead clicks + orphan routes ───────────
  {
    id: "web-dead-click",
    title: "Every clickable control must lead somewhere — no dead clicks",
    whenToApply:
      "A page renders something that LOOKS interactive (a button, link, card, or element with a pointer cursor / role=button) but clicking it does nothing: the handler is missing, an empty stub, `href=\"#\"` with no `preventDefault` + action, or it points at a route that does not exist. Symptom: users click and nothing happens, so they rage-click, reload, or leave — the control looks live but is a dead end, with no error in the console. Boosthis' on-device dead-click detector watches for clicks on actionable-looking targets that produce no navigation, no DOM change, and no scroll within a short window and surfaces the rate. Detection is fully in-page — no URLs, selectors, or text leave the browser.",
    evidence: [
      "lib/boosthis-runtime-web/src/vitals.ts (dead-click detector — click + MutationObserver, count-only)",
      "Nielsen Norman Group — dead clicks as a top user-frustration signal",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-unreachable-route",
    title: "Every registered route must be reachable — no orphan routes",
    whenToApply:
      "A route/view is registered in the SPA router but nothing in the app ever links or navigates to it (no `<Link>`, `<a href>`, `router.push`, or redirect resolves to it from a reachable page), so it ships as dead code that can silently drift out of sync, or it is a feature users can never reach. Walk your route table against the links your app actually renders and remove or wire up any route with zero inbound paths from the entry route. Symptom: a component that still bundles and mounts but is orphaned from the navigable graph. This is the browser counterpart of the app's unreachable-screen rule — the reachability analysis itself stays in the browser; only two anonymous counts (declared routes, unreachable routes) ride the perf snapshot to your dashboard, never the route names.",
    evidence: [
      "Reachability-graph analysis: a registered route with no inbound edge from the entry point is unreachable",
      "web.dev — audit dead routes / unused code paths in SPA routers",
    ],
    category: "industry",
    languages: ["web"],
  },
  // ─────────── Circuit lens — graph/wiring fault detectors ───────────
  {
    id: "web-nav-loop-oscillation",
    title: "No route ping-pong — pages that bounce A→B→A are an oscillating loop",
    whenToApply:
      "Two routes keep navigating to each other in a tight A→B→A→B cycle within one session: a router guard on route A redirects to B while B's guard immediately redirects back (an auth guard vs. an onboarding/consent check with opposing conditions), a `useEffect` that calls `router.push` based on state the destination page flips back, or server 302s ping-ponging with a client-side redirect. Symptom: the URL bar flickers between two paths, history fills with duplicate entries so Back is broken, the page visibly re-renders in a shuffle, and in the worst case the tab pegs a CPU core until the browser gives up with `ERR_TOO_MANY_REDIRECTS`. A control loop that oscillates is a classic wiring fault — the detector watches the in-page route graph for a repeated two-node cycle inside one session and flags it only on repeat (route labels + counts only, nothing leaves the browser).",
    evidence: [
      "Control-systems oscillation: two coupled guards with opposing conditions form an unstable feedback loop",
      "The browser counterpart of the app's rn-nav-loop-oscillation rule — same session-cycle detection",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-fanout-overload",
    title: "Bound the burst — one page firing N concurrent requests on load is fan-out overload",
    whenToApply:
      "A single page load or user action fires an anomalously large burst of concurrent requests: a component tree where every widget fetches its own data on mount (the client-side N+1 — one call per card in a grid), an effect that re-subscribes on every render so requests multiply, or a dashboard that refreshes every panel at once on a timer. Symptom: the network tab shows a request storm on landing, the browser's per-origin connection limit queues the overflow so EVERYTHING slows, perceived latency is set by the slowest of the N calls, and the server sees one visit multiplied N×. In circuit terms this is over-current: one node driving more parallel load than the path is rated for. The detector counts concurrent outbound requests per action in-page and flags the outliers (counts + coarse buckets only, no URLs).",
    evidence: [
      "Browser per-origin connection limits — excess parallel requests queue and serialize",
      "Sibling of client-retry-no-backoff (retry amplification) — this rule measures the initial burst itself",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-highest-leverage-node",
    title: "Fix the highest-leverage page first — centrality × latency beats absolute-slowest",
    whenToApply:
      "You are choosing what to optimize next and several pages look slow. One page usually sits on far more user journeys than any other (the shell/landing route, a list page feeding every detail view) — and fixing a mildly slow page that EVERY journey crosses improves aggregate Web Vitals more than perfecting the slowest page nobody visits. Symptom of getting it wrong: weeks spent tuning a rarely-reached page while field LCP/INP percentiles barely move, because the traffic-weighted constraint went untouched. The detector combines how many observed journeys pass through each route (centrality on the in-page route graph) with that route's own vitals to rank the single highest-leverage fix — a derived ranking over data already measured, nothing leaves the browser.",
    evidence: [
      "Theory of Constraints — throughput is set by the constraint; improve the constraint first",
      "Betweenness centrality (graph theory) — the node on the most paths dominates aggregate experience",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-cut-vertex-spof",
    title: "Know your cut-vertex routes — one page every journey funnels through is a single point of failure",
    whenToApply:
      "One route is an articulation point of your navigation graph: remove it and whole regions of the site become unreachable (a login wall, a home shell every deep link bounces through, a checkout step every purchase funnels into). If that one route is slow, broken, or erroring, everything behind it is blocked at once — and per-page vitals give no warning, because the risk is structural concentration, not current slowness. Symptom: a single regression takes out most user journeys simultaneously, and 'why does every path go through this page?' is discovered only during the incident. The detector finds cut vertices in the observed route graph and flags the concentration risk (route labels + structure stay in the browser — nothing about your navigation graph rides the snapshot, only aggregate loop/burst magnitudes).",
    evidence: [
      "Graph theory — articulation points / cut vertices disconnect the graph when removed",
      "Power-grid N-1 contingency planning: no single element's loss may take down the network",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-crash-cascade",
    title: "Trace the crash cascade — 'when A errors, B errors next' is one fault, not two",
    whenToApply:
      "JS error signatures fire in correlated sequences: when route A's error signature fires, route B — the page users navigate to next, or the component consuming state A left half-written — tends to error right after (a failed fetch cached as `undefined`, a store left in a broken shape, a chunk-load failure poisoning the next navigation). Treating each error as isolated hides the blast radius, so you patch the downstream symptom while the upstream trip keeps firing. Symptom: 'unrelated' error signatures that always spike together in the same sessions, in the same order. Like a cascading grid failure, the first trip propagates. The detector correlates code-derived error signatures along the route graph — 'when A fires, B tends to fire next' — and surfaces the directional pair (signatures + counts only, never raw messages).",
    evidence: [
      "Power-grid cascading-failure analysis — one breaker trip overloads and trips the next line",
      "Boosthis crash-risk feed (code-derived signatures) — this rule adds the along-the-graph correlation",
    ],
    category: "industry",
    languages: ["web"],
  },
  // ─────────── Rendering & paint (5) — layout, CSS, media discovery ───────────
  {
    id: "web-content-visibility-longpage",
    title: "Use `content-visibility: auto` to skip rendering offscreen sections",
    whenToApply:
      "A long document (feed, article, docs page, dashboard with many stacked sections) lays out and paints every offscreen block on load because no `content-visibility: auto` + `contain-intrinsic-size` is set on the repeating section containers. Symptom: initial layout, style recalc, and paint scale with the WHOLE page height, so a 20-screen page burns render time on content the user has not scrolled to — LCP and first paint lag while the browser rasterizes far-below-the-fold sections. Skipping rendering of near-viewport content with content-visibility cuts that work; a missing contain-intrinsic-size placeholder height causes scrollbar jump and CLS.",
    evidence: [
      "web.dev — content-visibility: the new CSS property that boosts rendering performance",
      "MDN — content-visibility and contain-intrinsic-size",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-forced-reflow-thrash",
    title: "Batch DOM reads and writes — never thrash layout in a loop",
    whenToApply:
      "Code interleaves layout-reading properties (`offsetWidth`, `offsetHeight`, `offsetTop`, `getBoundingClientRect`, `clientHeight`, `scrollTop`, `getComputedStyle`) with style/DOM writes inside a loop or across a render pass, forcing a synchronous reflow (forced synchronous layout / layout thrashing) on every iteration. Symptom: the profiler shows repeated purple 'Recalculate Style' + 'Layout' bars and a 'forced reflow' warning; a list of N elements costs O(N) full-page layouts. Fix: read all measurements first, then write all mutations in a second pass, or defer writes to `requestAnimationFrame` so the browser lays out once. FastDOM-style read/write batching removes the interleave.",
    evidence: [
      "web.dev — Avoid large, complex layouts and layout thrashing",
      "MDN — forced synchronous layout; batch reads before writes",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-render-blocking-css",
    title: "Inline critical CSS and defer the rest — stylesheets block first paint",
    whenToApply:
      "A large `<link rel=\"stylesheet\">` (or `@import` chains inside CSS) sits in `<head>`, so the browser blocks the first paint until the entire render-blocking stylesheet downloads and parses. Symptom: a white screen persists until CSS arrives — first paint and LCP wait on the full CSS payload, worst on slow networks and high-latency mobile. Fix: inline the small critical (above-the-fold) CSS in the head and load the rest non-render-blocking with `media`/`onload` swap or `rel=\"preload\" as=\"style\"`; never chain stylesheets with `@import` (each adds a serial round-trip on the critical path).",
    evidence: [
      "web.dev — Defer non-critical CSS / Extract critical CSS",
      "MDN — render-blocking stylesheets and @import serialization",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-responsive-image-formats",
    title: "Ship responsive `srcset` + next-gen AVIF/WebP images, not one huge JPEG/PNG",
    whenToApply:
      "An `<img>` serves a single oversized JPEG/PNG at natural resolution to every device with no `srcset`/`sizes` and no next-gen format (AVIF or WebP via `<picture>` sources). Symptom: a phone downloads a 2000px, multi-hundred-kB image to fill a 360px slot, wasting bandwidth and delaying LCP; Lighthouse flags 'Properly size images' and 'Serve images in next-gen formats'. Fix: provide a `srcset` with several widths plus a correct `sizes`, and offer AVIF/WebP `<source>` entries with a JPEG/PNG fallback so the browser picks the smallest sufficient file for the viewport and DPR.",
    evidence: [
      "web.dev — Serve responsive images / Use WebP and AVIF images",
      "MDN — srcset, sizes, and the picture element",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-sync-image-decode",
    title: "Decode large images off the main thread — avoid paint-time decode stalls",
    whenToApply:
      "Large images are painted synchronously (no `decoding=\"async\"` on `<img>`, or JS swaps a big `src` without awaiting `img.decode()` first), so the browser decodes the bitmap on the main thread at paint time. Symptom: a jank spike / dropped frames right when a heavy image appears — a carousel advance, a route change, or a gallery scroll stutters because decode + rasterize of a multi-megapixel image blocks the frame. Fix: add `decoding=\"async\"`, and for programmatic swaps call `await img.decode()` before inserting the element so the decode happens off the critical rendering path.",
    evidence: [
      "web.dev — Image decoding and the main thread",
      "MDN — HTMLImageElement.decode() and the decoding attribute",
    ],
    category: "industry",
    languages: ["web"],
  },
  // ─────────── Interaction responsiveness (3) — INP-adjacent event work ───────────
  {
    id: "web-non-passive-scroll-listeners",
    title: "Mark scroll/touch/wheel listeners `passive` so scrolling never blocks",
    whenToApply:
      "`addEventListener('touchstart'/'touchmove'/'wheel'/'scroll', …)` is registered without `{ passive: true }`, so the browser must wait for the handler to finish (in case it calls `preventDefault`) before it can scroll. Symptom: scrolling and swipes feel laggy or stick; the console warns 'Added non-passive event listener to a scroll-blocking event', and INP/scroll responsiveness degrades on touch devices. Fix: pass `{ passive: true }` on any listener that never calls `preventDefault`; only omit it (and actually call `preventDefault`) when you genuinely need to cancel the gesture.",
    evidence: [
      "web.dev — Use passive listeners to improve scrolling performance",
      "MDN — EventTarget.addEventListener passive option",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "web-undebounced-high-frequency-events",
    title: "Debounce/throttle scroll, resize, and input handlers that do heavy work",
    whenToApply:
      "A high-frequency event — `scroll`, `resize`, `mousemove`, `pointermove`, or `input` — runs expensive work on every fire (state updates, layout reads, network calls, re-renders) with no debounce or throttle. Symptom: dragging, resizing, or typing pins the main thread because the handler fires dozens of times per second, each triggering a re-render or reflow; INP goes poor and animations stutter. Fix: throttle to one execution per animation frame with `requestAnimationFrame`, or debounce trailing work (search-as-you-type, resize recompute) so it runs once after the burst settles; keep the synchronous handler body tiny.",
    evidence: [
      "web.dev — Debounce your input handlers / Optimize INP",
      "MDN — throttling and debouncing high-frequency events",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "web-willchange-layer-explosion",
    title: "Don't over-apply `will-change` — too many compositor layers exhaust GPU memory",
    whenToApply:
      "`will-change` (or a `translateZ(0)`/`translate3d` hack) is set broadly — on a whole list, many cards, or permanently in a static rule rather than just-before an animation — so the browser promotes far too many elements to their own compositor layers. Symptom: memory balloons, the compositor thread and GPU upload cost spike, and paint gets SLOWER not faster; on low-end devices tabs are evicted or scrolling janks. Fix: apply `will-change` only to the few elements about to animate, add it shortly before the animation and remove it after, and prefer animating `transform`/`opacity` without a blanket layer hint.",
    evidence: [
      "web.dev — Stick to compositor-only properties and manage layer count",
      "MDN — will-change: use sparingly, not preemptively everywhere",
    ],
    category: "operational",
    languages: ["web"],
  },
  // ─────────── Network calls (3) — request bloat, speculative nav & hanging calls ───────────
  {
    id: "web-hanging-backend-call",
    title: "Give every browser call a deadline — a hanging call never comes back on its own",
    whenToApply:
      "The app calls a backend or a hosted database (Supabase, Firebase, or any REST/GraphQL endpoint) with no timeout and no `AbortSignal`, so a request that never receives a response simply stays open forever. Symptom: a spinner that spins for the whole session, a screen stuck on 'Loading…', a form whose submit button never re-enables — and NOTHING in the console, because no error was ever thrown and no `catch` ever ran. This is worse than a failure: a failure re-renders, a hang does not. It is also invisible to any measurement taken from completions alone, which is why the browser kit watches calls where they START — a call still open when a reading is taken is reported as hanging rather than dropped. Front-end-only apps are the most exposed: with no server of their own there is no gateway timeout to end the wait, so the browser holds the socket until the tab closes. Fix: give every call an explicit deadline with `AbortSignal.timeout(ms)`, always handle the abort as a real outcome (show an error and a retry, never leave the spinner), and pair long operations with a visible cancel control.",
    evidence: [
      "MDN — AbortSignal.timeout() and passing `signal` to fetch",
      "fetch() has NO default timeout: the spec leaves the wait to the caller, so an unanswered request never settles on its own",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-bloated-cookies-headers",
    title: "Keep cookies small — large cookies bloat every request on the origin",
    whenToApply:
      "Large or numerous cookies (session blobs, feature flags, serialized JSON, tracking IDs) are set on a broad `path=/` scope, so the browser attaches the full `Cookie` header to EVERY request to that origin — including static assets, images, fonts, and API calls. Symptom: request headers swell to several kilobytes each, inflating upload on slow uplinks and delaying TTFB; servers hit header-size limits. Fix: store large state in `localStorage`/IndexedDB or server-side keyed by a small session id, scope cookies narrowly by `path`/`Domain`, and serve static assets from a cookieless subdomain so their requests carry no cookie header.",
    evidence: [
      "web.dev — Minimize request size / cookieless static asset origins",
      "MDN — Set-Cookie path and domain scoping",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-no-next-nav-prefetch",
    title: "Prefetch/prerender the likely next navigation with Speculation Rules",
    whenToApply:
      "A multi-page site or SPA makes the user wait on the FULL next-page load because it never speculatively fetches the likely next route — no `<link rel=\"prefetch\">`, no framework route prefetch on hover/viewport, and no Speculation Rules `prefetch`/`prerender` for high-confidence destinations. Symptom: clicking a nav link, 'next', or a product tile starts a cold document + data fetch from zero, so perceived navigation latency is a full round-trip even though the destination was highly predictable. Fix: prefetch on link hover/viewport-intersection, or add a Speculation Rules script that prerenders the top one or two likely destinations, while budgeting so speculation does not steal bandwidth from the current page's LCP.",
    evidence: [
      "web.dev — Prerender pages / the Speculation Rules API",
      "MDN — rel=prefetch and speculationrules script type",
    ],
    category: "industry",
    languages: ["web"],
  },
  // ─────────── Cross-language wave 1 (6) — concepts proven in other runtimes ───────────
  {
    id: "web-abort-cancel-client-only",
    title: "AbortController cancels only the client side — never assume the server stopped",
    whenToApply:
      "Code passes an AbortController `signal` into fetch/XHR and treats a successful abort as if the operation was undone — cancelling a save then immediately re-submitting, navigating away and assuming the mutation never happened, or racing two aborted-and-retried writes. Aborting rejects the client promise with `AbortError`, but the request may already be in flight and the server usually completes the work anyway. Symptom: 'cancelled' actions still take effect server-side (duplicate orders, double posts after cancel+retry), and stray unhandled `AbortError` rejections pollute the console/error reporting because the abort path was never caught.",
    evidence: [
      "MDN — AbortController/AbortSignal: abort rejects the fetch promise, it does not undo server work",
      "web.dev — resilient requests: idempotency for retried/cancelled mutations",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-bootstrap-self-gate",
    title: "Never gate the first bootstrap/registration call on state only that call can change",
    whenToApply:
      "Startup logic suppresses the first registration/activation/handshake fetch behind an app-state flag that only that same call's success would flip — `if (!isRegistered) return;` before the register call, a `ready`/auth guard set only by the bootstrap response, or an effect whose enabling condition is the credential it exists to obtain. Symptom: a fresh install sits silently unregistered forever — no error, no retry, nothing in the network tab — because the gate can never open without the call it is blocking. The deadlock hides in dev where a cached credential satisfies the gate.",
    evidence: [
      "Boosthis production incident (Jul 2026) — a first-run call gated on its own result left fresh clients permanently unregistered",
      "Control-systems deadlock: a guard whose only unlocking event is the action it blocks",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-remote-data-null-guard",
    title: "Guard remote/storage data before dereferencing nested fields",
    whenToApply:
      "Code reads nested properties straight off external data — `data.user.profile.name` from a fetch result, `JSON.parse(localStorage.getItem(...)).settings.theme`, router `location.state.item.id` — without null/undefined/shape checks. API responses can be partial, storage can hold a stale schema from a previous release, and navigation state is absent on deep links/refresh. Symptom: `TypeError: Cannot read properties of undefined` crashes that only reproduce for users with old persisted data, error responses, or direct-URL entry — never in the happy-path dev flow.",
    evidence: [
      "MDN — optional chaining and nullish coalescing for defensive access",
      "Boosthis crash telemetry — undefined-dereference is the top browser crash signature across registered projects",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-unsafe-payload-parse",
    title: "Parse fetch/storage payloads defensively — JSON.parse and response.json() throw on real-world bodies",
    whenToApply:
      "`await response.json()` or `JSON.parse(...)` runs on an untrusted body with no try/catch and no content-type/status check — an API that returns an HTML error page on 502, an empty 204 body, a truncated response over a flaky connection, a captive-portal login page, or corrupted `localStorage`. Symptom: the failure surfaces as a cryptic `SyntaxError: Unexpected token '<'` far from the real cause (the upstream error the body actually described), and one malformed cached value can crash-loop the app at boot.",
    evidence: [
      "MDN — Response.json() rejects on invalid JSON; check ok/status and content-type first",
      "Captive-portal/CDN error pages returning HTML to fetch callers — a classic 'Unexpected token <' class",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-transport-must-throw",
    title: "Retrying fetch wrappers must reject on transport failure — never resolve a synthetic response",
    whenToApply:
      "A retry/timeout wrapper around fetch/XHR catches a transport-level failure (offline, DNS, CORS block, abort, timeout, dropped socket) and RESOLVES a fake Response-like object (`{ ok: false, status: 0 }`) instead of rethrowing. The retry path only engages on rejection, so the synthetic 'response' is treated as a final answer and the wrapper's whole purpose — recovering from flaky transport — silently never runs. Symptom: retries never fire on exactly the failures they exist for; one network blip becomes a permanent failure while the retry counter stays at zero.",
    evidence: [
      "Boosthis production incident (Jul 2026) — a transport adapter resolved {status:0}, skipping the retry-over-dead-socket path entirely",
      "fetch spec — network errors reject the promise; status 0 is not a real HTTP response",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-single-event-perf-logging",
    title: "Emit related page perf metrics in ONE event per navigation, not scattered singles",
    whenToApply:
      "Instrumentation reports LCP, INP, CLS, and custom timings as separate analytics events fired at different moments (each metric's observer sends its own beacon), so no single record holds the whole picture of one page view. Symptom: answering 'sessions where LCP was poor AND INP was poor' requires joining event streams on fragile session/page keys, late metrics (INP finalizes at pagehide) get lost or attach to the wrong view, and per-metric sampling makes the joins statistically meaningless.",
    evidence: [
      "web-vitals library — buffer metrics and flush one payload on visibilitychange/pagehide",
      "Indeed Engineering — one combined perf event per screen beats three separate streams",
    ],
    category: "industry",
    languages: ["web"],
  },
  // ─────────── Case Study (3) — cross-runtime batch (vendored drift, dev-server port hold, detached observer) ───────────
  {
    id: "web-vendored-copy-drifts-from-source",
    title: "Regenerate bundled/vendored assets in CI and fail on diff — never hand-edit the built copy",
    whenToApply:
      "The site ships a GENERATED or COPIED artefact that duplicates a source of truth — a committed production bundle in `dist/`/`build/`, a fingerprinted asset served from a stale `public/` copy, a vendored third-party script pinned in the repo, a generated `manifest.json`/`asset-manifest.json`, or an inlined critical-CSS blob — and someone edits the SOURCE while the served copy keeps its old bytes. Symptom: nothing errors, the page renders, but the browser loads the STALE committed bundle (often behind a long `Cache-Control` immutable header keyed on an unchanged filename), so users get old behaviour and the source fix appears to do nothing. Two plausible versions coexist and the diff is invisible until someone hard-reloads.",
    evidence: [
      "Boosthis production incident (Aug 2026) — a committed dist bundle served old code because the build was not re-run and diffed in CI",
      "web.dev — cache-busting requires the generator to fingerprint output; a hand-edited built file keeps its old immutable URL",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "web-dev-server-old-process-holds-port",
    title: "A dev-server restart must serve the NEW build — fail loud when the port is already held",
    whenToApply:
      "A dev/preview server (Vite, webpack-dev-server, an HMR watcher) is restarted but the OLD process still holds the port — an orphaned watcher, a detached terminal process, or an editor task that respawns without killing the previous one. Symptom: either the new server errors with the port in use, OR the tool silently picks the NEXT free port while the OLD server keeps serving the previous build on the URL you have open — so the tab hot-reloads against a stale bundle and your latest change never appears, with no error to explain it. A config that auto-increments the port on conflict instead of failing is this trap.",
    evidence: [
      "Boosthis production incident (Aug 2026) — an orphaned Vite watcher kept serving the old build on the expected port while the restarted one moved to a random port",
      "Vite/webpack-dev-server docs — set strictPort so a busy port fails loudly instead of silently shifting",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "web-observer-never-attached",
    title: "Assert the PerformanceObserver/RUM listener is attached and prove one real interaction moves it",
    whenToApply:
      "A `PerformanceObserver`, an `IntersectionObserver` used for visibility metrics, an event listener that feeds a custom timing, or a RUM SDK's tracker is constructed and compiles, but nothing activates it — `.observe()` is never called, the listener is never `addEventListener`'d, or the RUM `init()` runs but the metric hook is never bound. Symptom: the metric reads zero forever and its dashboard tile is empty or permanently 'collecting', while unit tests pass because the observer callback works when invoked directly. The failure is the missing attachment, which only a real interaction driven through the mounted page can catch.",
    evidence: [
      "Boosthis production incident (Aug 2026) — an INP observer was created but observe() was never called, so the metric stayed at zero in production",
      "MDN PerformanceObserver / web-vitals — a metric only emits after the observer is registered and a real entry is recorded",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "web-fixed-ms-timing-assert",
    title: "Poll to a deadline instead of asserting a fixed millisecond budget",
    whenToApply:
      "A Playwright/Testing Library test, a readiness gate, or a synthetic health check hinges on a FIXED number of milliseconds — a `page.waitForTimeout(300)` (or a bare `setTimeout`) before the assertion, an `expect(performance.now() - start).toBeLessThan(200)` reading a wall-clock delta the test itself measured, or a check that sleeps then asserts an element/state has appeared. On a shared CI runner, a throttled preview container, or a laptop mid-build, wall time measures the MACHINE not the code, so the check flakes red when nothing is wrong AND stays green on an idle box even after the code got slower — both directions fail. Fix it with the framework's own waiting: Playwright auto-waiting locators and web-first `expect().toPass()`, Testing Library `waitFor`/`findBy*` polling to a generous deadline, and `vi.useFakeTimers()` to drive time deterministically instead of really sleeping; assert ORDERING/causality or the operation's OWN `performance.measure` duration, not the test clock. Keep real latency budgets as p75 Web Vitals (LCP/INP/CLS) or Lighthouse budgets measured from field/RUM percentiles, never a constant in a unit test, and where a hard timeout must exist make it a generous HANG bound in seconds, not a performance assertion.",
    evidence: [
      "Boosthis production incident (Aug 2026) — a gate asserted a finish-time by reading the instant it ran instead of waiting for it with a bounded poll, so it failed on a loaded runner while the code was correct",
      "Playwright auto-waiting / web-vitals docs — rely on locators and expect().toPass() polling, and track LCP/INP as field percentiles rather than gating on a fixed waitForTimeout",
    ],
    category: "case-study",
    languages: ["web"],
  },
  {
    id: "web-credentialed-refusal-signin-challenge",
    title: "Do not turn a credentialed refusal into a sign in restart",
    whenToApply:
      "In browser JavaScript response handling through fetch interceptors and localStorage or cookie credential state, a response with status 401 and an authenticate challenge clears a stored credential and starts sign in even though the request carried an authorization header. The page turns one refused call into a forced login and reports a healthy reachable service as unavailable.",
    evidence: [
      "Fetch API and browser credential storage documentation",
      "HTTP authentication status and challenge semantics",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "web-generic-refusal-message",
    title: "Give every refusal a distinct actionable code and message",
    whenToApply:
      "In browser JavaScript, distinct failures such as offline transport expired session permission refusal and server fault all become the same generic message. Users and support cannot tell which action can recover the request.",
    evidence: [
      "Fetch API error handling documentation",
      "HTTP problem detail and stable error code guidance",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "web-strict-credential-scheme-parsing",
    title: "Normalize credential schemes and token whitespace before parsing",
    whenToApply:
      "In browser JavaScript, authorization headers are built by concatenating a scheme with a pasted or stored token without trimming surrounding whitespace quotes or line breaks. A valid credential becomes a near miss before fetch sends it.",
    evidence: [
      "Fetch Headers API documentation",
      "HTTP authentication scheme parsing guidance",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "web-silent-protection-fail-open",
    title: "Surface every degraded protection even when it fails open",
    whenToApply:
      "In browser JavaScript, a local protection such as credential refresh input validation or request gating catches an unavailable dependency or bad configuration and continues without recording that protection is degraded. The page silently sends work through an unprotected path.",
    evidence: [
      "Browser networking and security documentation",
      "Client degraded mode observability guidance",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "web-validated-url-browser-reresolves",
    title: "Do not treat browser URL validation as address validation",
    whenToApply:
      "Browser code accepts an import callback image or fetch URL, approves its hostname with `URL`, a regular expression, or an allowlist, and then passes the name to `fetch`, XHR, an image element, or navigation with redirects enabled. Browser JavaScript cannot inspect or pin the resolved socket address, so a later DNS answer or redirect can target localhost a private network or link local metadata while the original name still looks approved.",
    evidence: [
      "Fetch Standard redirect and network fetching algorithms",
      "Private Network Access specification",
      "OWASP server side request forgery prevention guidance",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-global-error-hooks-replaced",
    title: "Do not replace browser logging and global error hooks",
    whenToApply:
      "A browser library or app bootstrap assigns `window.onerror` or `window.onunhandledrejection`, overwrites `console.error` or other console methods, or removes previously registered error listeners instead of chaining them. The host reporter formatter redaction and destination stop receiving failures after the new integration initializes.",
    evidence: [
      "MDN Window error event and window.onerror",
      "MDN Window unhandledrejection event",
      "MDN EventTarget addEventListener",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-locks-exclusive-reentry",
    title: "Do not request the same exclusive Web Lock while holding it",
    whenToApply:
      "A callback passed to `navigator.locks.request` for an exclusive lock awaits a helper observer state callback or getter that calls `navigator.locks.request` with the same lock name. The Web Locks API queues the inner request until the outer callback settles, while the outer callback is waiting for the inner request, so the page operation hangs without an exception.",
    evidence: [
      "Web Locks API exclusive lock scheduling",
      "MDN LockManager request callback lifetime",
      "Web Locks specification lock request queue",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-retry-leaves-fetch-running",
    title: "Do not retry while the abandoned fetch is still running",
    whenToApply:
      "A browser retry helper times out a fetch XHR stream or worker operation with `Promise.race` or a timer and starts the next attempt without calling `AbortController.abort`, `XMLHttpRequest.abort`, stream cancellation, or worker termination. The losing request keeps its connection and completes work whose result has no consumer, so repeated timeouts multiply in flight attempts.",
    evidence: [
      "MDN AbortController abort and AbortSignal",
      "MDN XMLHttpRequest abort",
      "Fetch Standard request cancellation",
    ],
    category: "industry",
    languages: ["web"],
  },

  // ─────────── Realtime connections (3) — long-lived socket/stream lifecycle ───────────
  {
    id: "web-realtime-no-heartbeat",
    title: "Send a heartbeat so a dead socket is noticed",
    whenToApply:
      "A page holds a `WebSocket` or `EventSource` open for minutes and never sends an application-level ping, never expects a periodic server message, and has no timer that reconnects when nothing has arrived for a while. TCP will happily keep a half-open connection alive for many minutes after a proxy, load balancer, mobile handover or laptop sleep has silently discarded the other end, and the browser fires no `close` or `error` for it. Symptom: chat, presence or a live dashboard quietly stops updating with the socket still reading `OPEN` — no error in the console, nothing red anywhere, and the user only finds out when they send something and get no reply.",
    evidence: [
      "MDN WebSocket readyState and close event",
      "RFC 6455 section 5.5.2 — Ping and Pong frames",
      "MDN EventSource — reconnection and the retry field",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "web-socket-never-closed",
    title: "Close a live connection when the view that opened it is gone",
    whenToApply:
      "A component, route or hook opens a `WebSocket` or `EventSource` and never calls `close()` on the way out — no cleanup function returned from `useEffect`, no `close()` on unmount, or a cleanup that only clears a local reference. Every visit opens another connection while the old ones stay open, still receiving and still firing handlers on state nobody renders. Symptom: connection count climbs the longer the tab is open, the same message is handled several times, memory grows, and the server hits its per-client connection limit for a page that looks like it has one socket.",
    evidence: [
      "MDN WebSocket close",
      "MDN EventSource close",
      "React documentation — cleaning up an Effect that opens a connection",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "web-socket-reconnect-per-view",
    title: "Keep one live connection across route changes",
    whenToApply:
      "The live connection is created inside a page/route component rather than above the router, so every client-side navigation closes it and opens a new one. Symptom: a user clicking between tabs of the same app produces a reconnect per click — a handshake, an auth round trip and a state resync each time — which reads on the server as a reconnect storm from a healthy user, loses any message sent during the gap, and makes presence flap between online and offline.",
    evidence: [
      "MDN WebSocket — the connection is closed when its owner is discarded",
      "React documentation — lifting shared state and long-lived resources above the tree that re-renders",
    ],
    category: "operational",
    languages: ["web"],
  },
  {
    id: "web-app-far-from-its-backend",
    title: "Keep the page's own API close to the page, and judge that distance from connection setup, never from call time",
    whenToApply:
      "A site is served from an edge network while the API it calls answers from a single distant region \u2014 or the API is reached over a fresh connection on every visit \u2014 so a visitor pays the connect and TLS handshake before any data moves, on top of whatever the API takes to answer. Symptom: a page that feels slow everywhere except near the API's region, with backend timings that look fine in the server's own logs. Judge it from the connection SETUP phases of resource timing (connectStart\u2192connectEnd, which contains the TLS handshake), never from the entry's total duration: duration also contains the server's thinking time, so a slow-but-nearby API would be misread as a distant one. Two limits keep this honest in a browser: these phases are only readable for the page's own origin unless a third party opts in with Timing-Allow-Origin, so say nothing about origins that did not \u2014 and a reused connection reports no connect phase at all, which is a healthy result, not a zero-distance reading.",
    evidence: [
      "MDN PerformanceResourceTiming \u2014 connectStart/connectEnd/secureConnectionStart are zeroed for cross-origin resources without Timing-Allow-Origin, and collapse to fetchStart when a connection is reused",
      "HTTP/2 and HTTP/3 connection reuse \u2014 only the FIRST request to an origin pays setup, so a distance verdict must be drawn from newly-opened connections",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-dependency-call-no-timeout",
    title: "Put a deadline on every outside-service call the page makes — sign-in first",
    whenToApply:
      "A page calls an outside service directly — a hosted identity provider, an object store, a payments or search vendor — with no `AbortSignal.timeout(...)`, no timeout option on the SDK client, and no server of its own that could time the wait out. Symptom: the provider degrades rather than fails, so the page holds a spinner that never resolves; there is no backend log to look at and no server-side timeout to trip, because the only party waiting is the browser tab. Sign-in is the worst place for it: it sits in front of the whole app, so a stalling identity provider makes the product look down before anything renders.",
    evidence: [
      "MDN — AbortSignal.timeout() and passing a signal to fetch",
      "Google SRE Book — Addressing Cascading Failures (a slow dependency is more dangerous than a dead one)",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-dependency-retry-uncapped",
    title: "Cap the page's retries against an outside service — thousands of tabs cannot be turned off centrally",
    whenToApply:
      "A page retries a refused or failed outside-service call with no attempt ceiling per page load, no total budget, no jitter and no handling for 429 and `Retry-After` — commonly a `setInterval` poller that never stops, a data-fetching library left on an unbounded retry setting, or a `catch` that simply calls the same request again. Symptom: every open tab keeps hammering a provider that is already refusing, and because the retries come from thousands of browsers rather than from one server there is nothing central to turn off; a 4xx that will refuse forever is retried exactly like a 503, and a tab left open overnight can spend hours re-attempting.",
    evidence: [
      "RFC 9110 — Retry-After",
      "Google SRE Book — Handling Overload (retry amplification)",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-dependency-calls-serial",
    title: "Start the page's independent outside-service calls together, not one after another",
    whenToApply:
      "One page load awaits several outside-service calls in sequence where no call consumes the previous one's result — identity, then the stored file, then the billing record — typically a run of `await`s in a route loader or effect, or a child component that only starts its fetch once its parent's fetch resolved. Symptom: the page's wait is the SUM of every provider's latency rather than the slowest one, which shows up as a long gap before first meaningful paint that no single provider is slow enough to explain. Distinct from client-fetch-waterfall in what it protects: these are calls to different third parties, so the ceiling is the slowest provider and a partial-failure policy has to be chosen deliberately.",
    evidence: [
      "MDN — Promise.all and Promise.allSettled",
      "Google SRE Book — Latency (serial dependencies add, parallel dependencies max)",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-dependency-on-visitor-path",
    title: "Take an outside service off the visitor's path when the visitor never sees its answer",
    whenToApply:
      "A page awaits an outside-service call whose answer the visitor never sees before it navigates or completes an action — an analytics event, a CRM sync, a marketing pixel, an audit write — so the click waits on it. Symptom: the visitor pays the latency and inherits the failure of work they will never see; worse, the call is often started immediately before a navigation, so the browser cancels it in flight and the event is lost anyway — the visitor waited AND the data was not recorded. Distinct from the deadline and retry rules: the problem here is not how long the call takes, it is that anyone is waiting for it at all.",
    evidence: [
      "MDN — navigator.sendBeacon and fetch keepalive survive a page unload",
      "Google SRE Book — Addressing Cascading Failures (shed non-critical work from the serving path)",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-job-retries-forever",
    title: "Cap a background-sync retry and give the last failure somewhere to go",
    whenToApply:
      "A Service Worker background-sync or retry queue re-registers its own failure with no attempt ceiling and nothing persisted — a `sync` handler that rejects unconditionally so the browser reschedules it, a handler that calls `registration.sync.register(tag)` again from its own `catch`, or a retry count held in a variable that dies with the worker. A 400 that will never succeed is treated exactly like a 503. Symptom: a permanently-rejected payload is re-attempted every time the browser decides conditions are right, from every device that ever queued it, and because the failure is never recorded anywhere it is invisible to the user and to the server that could fix it.",
    evidence: [
      "MDN — Background Sync: a rejected sync event is rescheduled by the browser",
      "AWS Architecture guidance — a poison message needs a destination other than the queue it fails in",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-ai-call-no-timeout",
    title: "Put a deadline on every AI provider call",
    whenToApply:
      "Browser code sends an AI request through `fetch` — usually to the app's own AI endpoint, or directly to an OpenAI Responses / Chat Completions or Anthropic Messages HTTP endpoint when a safe short-lived credential permits it — without an `AbortSignal.timeout(...)` or page-owned abort signal. Browser apps should not contain long-lived provider API keys, and there is no browser timeout default to rely on. Symptom: a generation that stalls leaves the chat composer disabled and a spinner or streaming placeholder alive indefinitely, even after the route that started it is gone. Distinct from web-dependency-call-no-timeout in what it bounds: AI generation is unusually long-lived and its deadline must cover both the response headers and consumption of the streamed body.",
    evidence: [
      "MDN — AbortSignal.timeout() and passing a signal to fetch",
      "OpenAI API reference — Responses and Chat Completions endpoints",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-ai-retry-ignores-retry-after",
    title: "Honor the AI provider's Retry-After before retrying",
    whenToApply:
      "A browser AI client catches a 429 or overloaded response from the app's AI endpoint or a provider HTTP API and schedules another `fetch` from `setTimeout`, an effect, or a data-fetching library without first reading the response's `Retry-After` header. Symptom: every open tab retries before the provider's published deadline, sustaining the rate limit while consuming attempts and user battery on refusals that were known in advance. If the app proxies AI calls, preserve Retry-After on that browser-facing response rather than collapsing it into a generic 500. Distinct from web-dependency-retry-uncapped in scope: this rule applies even to a bounded AI retry loop when its otherwise finite attempts ignore the provider's requested delay.",
    evidence: [
      "RFC 9110 — Retry-After",
      "OpenAI API documentation — rate limits",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-ai-calls-serial",
    title: "Run independent AI calls concurrently with Promise.all",
    whenToApply:
      "One browser action awaits independent AI requests one after another — for example a completion, moderation check, embedding, classification, or two unrelated panel summaries sent through `fetch` to the app's AI endpoint — even though no later prompt consumes an earlier answer. Symptom: a page that could progressively render in the time of the slowest generation instead waits for the SUM of several multi-second generations. Start the requests together with `Promise.all`, or use `Promise.allSettled` only when the UI deliberately supports partial answers. Distinct from web-dependency-calls-serial in what it finds: these calls may all use one first-party endpoint, but fan out to separate expensive AI generations behind it.",
    evidence: [
      "MDN — Promise.all and Promise.allSettled",
      "OpenAI API reference — asynchronous Responses API operations",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-ai-duplicate-prompt",
    title: "Send each logical AI prompt only once per request",
    whenToApply:
      "One browser action sends the same model, messages, tools, and generation settings to the app's AI endpoint or provider HTTP API more than once — commonly because a React effect runs twice, both hover prefetch and click paths fire, two components call the same helper, or a retry starts after a valid response arrived. Symptom: the network panel shows equivalent AI POST bodies for one action, the user sees only one answer, and the same input and output tokens are billed twice. Share one request-scoped Promise keyed by the logical operation; do not persist raw prompts in `localStorage` merely to deduplicate them. Distinct from web-fanout-overload: this is duplicate semantic work even when the number of concurrent requests is small.",
    evidence: [
      "OpenAI API reference — response usage fields",
      "Anthropic Messages API reference — usage object",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-ai-prompt-cache-cold",
    title: "Keep large shared prompt prefixes stable so provider caching hits",
    whenToApply:
      "Browser code assembles large shared system instructions, tool schemas, examples, or documents for an AI request, but inserts timestamps, request IDs, random object or tool ordering, or visitor-specific text before that reusable content. The resulting JSON sent through the app's AI endpoint or directly to the provider changes its prefix on every action, so OpenAI cached-input usage stays zero or Anthropic `cache_control: { type: \"ephemeral\" }` blocks cannot be reused. Keep the common prefix byte-stable and put changing user content last; browser apps should still send long-lived provider credentials and shared prompt policy through their own backend. Symptom: repeated calls pay full input-token latency and cost despite sharing the same large beginning.",
    evidence: [
      "OpenAI API documentation — prompt caching",
      "Anthropic documentation — prompt caching with cache_control",
    ],
    category: "industry",
    languages: ["web"],
  },
  {
    id: "web-ai-stream-usage-not-requested",
    title: "Request usage metadata on every streamed AI response",
    whenToApply:
      "A browser starts an OpenAI-family streamed Chat Completions request through `fetch` or the app's streaming endpoint with `stream: true` but omits `stream_options: { include_usage: true }`, or stops its `ReadableStream` / SSE reader as soon as content finishes instead of consuming the terminal usage block. Symptom: tokens visibly stream into the page, but the client cannot attribute input, output, cached tokens, or cost and may incorrectly record zero usage. If a backend owns the provider connection, it must request and forward or record that terminal metadata before closing the browser stream. This wire-level option belongs in the JSON request body; it is not a browser EventSource setting.",
    evidence: [
      "OpenAI API reference — stream_options.include_usage",
      "MDN — Using readable streams with fetch",
    ],
    category: "industry",
    languages: ["web"],
  },
];

export const CHECKLIST_COUNT = BOOSTHIS_CHECKLIST.length;

export function getRule(id: string): BoosthisChecklistEntry | undefined {
  return BOOSTHIS_CHECKLIST.find((r) => r.id === id);
}

export function listRules(
  category?: Category,
): ReadonlyArray<BoosthisChecklistEntry> {
  if (!category) return BOOSTHIS_CHECKLIST;
  return BOOSTHIS_CHECKLIST.filter((r) => r.category === category);
}
