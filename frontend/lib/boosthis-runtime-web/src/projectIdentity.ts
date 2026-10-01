/** WHICH PROJECT this kit reports to, in the developer's own words.
 *
 * The server returns the project name + short code on consent. The name is
 * developer-authored display data, so it is screened again here before any
 * browser surface draws it. Renderers must always use textContent.
 */

const MAX_NAME_CHARS = 60;
const MAX_CODE_CHARS = 16;

export const PROJECT_UNNAMED_TEXT = "Unnamed project";
export const PROJECT_UNKNOWN_TEXT = "Not received from Boosthis yet";
export const PROJECT_LABEL = "Project";
export const SCORE_CAPTION =
  "Measured on this device. Not proof anything reached Boosthis.";

export interface KitProject {
  name: string | null;
  code: string | null;
}

/** Strip control characters, collapse whitespace, and enforce the wire cap. */
export function sanitizeProjectName(value: unknown): string | null {
  if (typeof value !== "string") return null;
  // eslint-disable-next-line no-control-regex
  const cleaned = value
    .replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, MAX_NAME_CHARS)
    .trim();
  return cleaned.length > 0 ? cleaned : null;
}

/** Project codes are server-authored, but still held to their closed shape. */
export function sanitizeProjectCode(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const cleaned = value.replace(/[^A-Za-z0-9]/g, "").slice(0, MAX_CODE_CHARS);
  return cleaned.length > 0 ? cleaned : null;
}

/** The one spelling used by every browser surface. Never throws. */
export function projectDisplay(p: KitProject | null | undefined): string {
  try {
    const code = p?.code ?? null;
    if (!code) return PROJECT_UNKNOWN_TEXT;
    return `${p?.name ?? PROJECT_UNNAMED_TEXT} (${code})`;
  } catch {
    return PROJECT_UNKNOWN_TEXT;
  }
}

let current: KitProject = { name: null, code: null };

/** Record a consent reply without letting an older reply erase known state. */
export function setKitProject(name: unknown, code: unknown): void {
  try {
    const nextCode = sanitizeProjectCode(code);
    const nextName = sanitizeProjectName(name);
    if (nextCode) current = { name: nextName, code: nextCode };
    else if (nextName) current = { name: nextName, code: current.code };
  } catch {
    // Display-only state must never throw into the host page.
  }
}

export function getKitProject(): KitProject {
  return { name: current.name, code: current.code };
}

export function _resetKitProject(): void {
  current = { name: null, code: null };
}

export function serializeKitProject(p: KitProject): string {
  return JSON.stringify({ name: p.name, code: p.code });
}

/** Parse persisted state through the same sanitizers used for the wire. */
export function parseKitProject(raw: unknown): KitProject | null {
  if (typeof raw !== "string" || !raw) return null;
  try {
    const obj = JSON.parse(raw) as { name?: unknown; code?: unknown };
    const code = sanitizeProjectCode(obj?.code);
    const name = sanitizeProjectName(obj?.name);
    if (!code && !name) return null;
    return { name, code };
  } catch {
    return null;
  }
}