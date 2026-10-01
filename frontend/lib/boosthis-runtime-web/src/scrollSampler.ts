/** Scroll frame sampler — the input for the Scroll + Frame Floor axes.
 *
 * Frames are sampled ONLY while a scroll is actually in progress: a `scroll`
 * listener starts a requestAnimationFrame loop, and the loop stops ~200ms after
 * the last scroll event. So NO permanent rAF loop ever runs in the customer's
 * page — if the user never scrolls, both axes stay absent (correct).
 *
 * From each active-scroll rAF sample we derive:
 *   - jankFraction — fraction of frames whose gap blew the 60fps budget (>32ms),
 *   - a worst-completed-1s-window effective FPS (frames ÷ window seconds) so one
 *     frozen second while scrolling is never averaged away (Frame Floor).
 * blankEvents / worstBlankPx have no browser equivalent and are reported 0/0.
 *
 * GUEST SAFETY: passive listener, guarded everywhere, bounded memory (the 1s
 * window aggregates incrementally — no per-frame buffer grows unbounded), the
 * loop self-terminates when scrolling stops, and everything is fully removed on
 * uninstall. PRIVACY: frame timings + counts only — never scroll position,
 * target, or any customer value.
 */

import type { ScrollStatsLike, FrameFloorStatsLike } from "./meterAxes";

/** A frame gap above this (ms) is "janky" (blew the ~30fps budget). Mirrors the
 *  RN scroll sampler's >32ms jank cutoff. */
const JANK_GAP_MS = 32;

/** Stop sampling this long after the last scroll event. */
const SCROLL_IDLE_MS = 200;

/** 1-second window length for the frame-floor bucketing. */
const FLOOR_WINDOW_MS = 1_000;

let installed = false;
let scrolling = false;
let rafId: number | null = null;
let lastFrameTs = 0;
let idleTimer: ReturnType<typeof setTimeout> | null = null;
let scrollHandler: (() => void) | null = null;

// Session-cumulative scroll frame stats.
let frameCount = 0;
let jankFrames = 0;

// Frame-floor bucketing: accumulate frames into the current 1s window; when the
// window completes, fold its effective FPS into the session worst (lowest).
let windowStartTs = 0;
let windowFrames = 0;
let completedWindows = 0;
let worstWindowFps = Infinity;

function nowMs(): number {
  try {
    if (
      typeof performance !== "undefined" &&
      typeof performance.now === "function"
    ) {
      return performance.now();
    }
  } catch {
    // fall through
  }
  return Date.now();
}

function closeWindow(): void {
  if (windowStartTs > 0 && windowFrames > 0) {
    const fps = (windowFrames * 1000) / FLOOR_WINDOW_MS;
    if (fps < worstWindowFps) worstWindowFps = fps;
    completedWindows++;
  }
  windowStartTs = 0;
  windowFrames = 0;
}

function onFrame(ts: number): void {
  if (!scrolling) return;
  try {
    if (lastFrameTs > 0) {
      const gap = ts - lastFrameTs;
      if (Number.isFinite(gap) && gap > 0) {
        frameCount++;
        if (gap > JANK_GAP_MS) jankFrames++;
        // Frame-floor 1s bucket.
        if (windowStartTs === 0) windowStartTs = ts;
        windowFrames++;
        if (ts - windowStartTs >= FLOOR_WINDOW_MS) closeWindow();
      }
    }
    lastFrameTs = ts;
  } catch {
    // never let sampling break the page
  }
  scheduleFrame();
}

function scheduleFrame(): void {
  try {
    if (typeof requestAnimationFrame === "function") {
      rafId = requestAnimationFrame(onFrame);
    }
  } catch {
    rafId = null;
  }
}

function stopSampling(): void {
  scrolling = false;
  try {
    if (rafId != null && typeof cancelAnimationFrame === "function") {
      cancelAnimationFrame(rafId);
    }
  } catch {
    // ignore
  }
  rafId = null;
  lastFrameTs = 0;
  // A partial window at scroll-end is discarded (never a full second → would
  // fake a low floor); only complete 1s windows count.
  windowStartTs = 0;
  windowFrames = 0;
}

function onScroll(): void {
  try {
    if (!scrolling) {
      scrolling = true;
      lastFrameTs = 0;
      scheduleFrame();
    }
    if (idleTimer != null) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      idleTimer = null;
      stopSampling();
    }, SCROLL_IDLE_MS);
  } catch {
    // scroll tracking must never break the host page
  }
}

/** Attach the passive scroll listener. Idempotent; a no-op outside a browser.
 *  Sampling begins only when the user actually scrolls. */
export function installScrollSampler(): void {
  if (installed) return;
  try {
    if (typeof window === "undefined" || typeof addEventListener !== "function") {
      return;
    }
    scrollHandler = onScroll;
    addEventListener("scroll", scrollHandler, { passive: true, capture: true });
    installed = true;
  } catch {
    installed = false;
  }
}

/** Detach the scroll listener + stop any in-progress sampling. Safe to call
 *  repeatedly; called on teardown/disable and in tests. */
export function uninstallScrollSampler(): void {
  try {
    if (scrollHandler && typeof removeEventListener === "function") {
      removeEventListener("scroll", scrollHandler, true);
    }
  } catch {
    // ignore
  }
  scrollHandler = null;
  try {
    if (idleTimer != null) clearTimeout(idleTimer);
  } catch {
    // ignore
  }
  idleTimer = null;
  stopSampling();
  installed = false;
}

/** Current scroll-jank stats (label-free). blankEvents/worstBlankPx are always
 *  0 in the browser (no list-virtualization signal exists). */
export function getScrollStats(): ScrollStatsLike {
  return {
    frameCount,
    jankFraction: frameCount > 0 ? jankFrames / frameCount : 0,
    blankEvents: 0,
    worstBlankPx: 0,
  };
}

/** Current frame-floor stats: the worst completed 1s window's effective FPS and
 *  how many completed windows back it. */
export function getFrameFloorStats(): FrameFloorStatsLike {
  return {
    floorFps: Number.isFinite(worstWindowFps) ? worstWindowFps : 0,
    windowCount: completedWindows,
  };
}

/** @internal test hook — seed the scroll/frame stats deterministically without
 *  a live rAF loop. */
export function _setScrollStatsForTests(s: {
  frameCount: number;
  jankFrames: number;
  worstWindowFps: number;
  completedWindows: number;
}): void {
  frameCount = s.frameCount;
  jankFrames = s.jankFrames;
  worstWindowFps = s.worstWindowFps;
  completedWindows = s.completedWindows;
}

/** @internal test hook — reset all scroll/frame state. */
export function _resetScrollSamplerForTests(): void {
  uninstallScrollSampler();
  frameCount = 0;
  jankFrames = 0;
  windowStartTs = 0;
  windowFrames = 0;
  completedWindows = 0;
  worstWindowFps = Infinity;
}
