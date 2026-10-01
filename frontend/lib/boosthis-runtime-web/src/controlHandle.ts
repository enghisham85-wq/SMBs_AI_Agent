/**
 * controlHandle — the one structural, text-free identity a control may carry.
 *
 * WHY A SECOND IDENTITY EXISTS
 * ----------------------------
 * `controlMap.ts` names a control by the developer's own test identifier
 * (`data-testid` and its four siblings). That name is excellent on the
 * device — a person typed it, it is readable, and the panel prints it — and
 * it may never leave the device, because a person typed it. The refusal is
 * recorded in `docs/decisions/control-identity-test-ids-only.md`.
 *
 * A control census has to travel. So it needs an identity with the opposite
 * property: nobody typed it, it cannot be read back into anything, and it is
 * the same handle for every user of the app. That is what this module makes.
 *
 * WHAT THE HANDLE IS
 * ------------------
 * The control's POSITION in the rendered tree, and nothing else:
 *
 *   - each ancestor's tag, taken from the closed list in
 *     {@link CONTROL_HANDLE_TAGS} — anything else (a custom element, whose
 *     name a developer wrote) becomes the single placeholder `x`;
 *   - that element's ordinal among its own preceding siblings carrying the
 *     same token;
 *   - up to {@link CONTROL_HANDLE_DEPTH} levels, root-first;
 *   - hashed to a short digest, so the path itself never travels either.
 *
 * WHAT IS NEVER READ — by rule, and a test records every property touched
 * -----------------------------------------------------------------------
 *   - text of any kind (`textContent`, `innerText`, `innerHTML`)
 *   - `id`, `name`, `class`, `href`, `src`, `value`, `placeholder`, `title`,
 *     `alt`, `aria-*`, `data-*` — no attribute VALUE is read at all
 *   - position on screen, size, computed style
 *   - anything a person typed, anywhere
 *
 * The only things touched are `nodeType`, `tagName`, `parentNode` and
 * `previousSibling`. A tag name outside the closed list is not kept, so even
 * `<acme-invoice-row>` contributes the letter `x`.
 *
 * HOW STABLE IT IS — stated honestly, because the map prints a claim on it
 * -----------------------------------------------------------------------
 * Across RELOADS of the same build: identical, by construction. The path is a
 * function of the markup, so the same control in the same place hashes the
 * same way on every load and for every user.
 *
 * Across RELEASES: identical for as long as the control's structural position
 * is unchanged — which survives renamed classes, rewritten copy, changed
 * styling and new attributes, the bulk of what a release actually changes. It
 * does NOT survive the control being moved, re-nested or re-ordered. So a
 * handle that stops appearing means "no control is at that position any
 * more", which is weaker than "the control was removed", and every surface
 * that compares two releases has to say the weaker thing.
 */

/**
 * The closed list of tag tokens a path segment may carry. Every one of them
 * is defined by the HTML specification, so none of them can be something a
 * developer wrote. Anything not on this list — a custom element, an SVG
 * shape, a framework's own tag — becomes {@link UNKNOWN_TAG_TOKEN}.
 *
 * Kept deliberately small: the structural skeleton of a page is carried by
 * its layout and control elements. A longer list would make the handle more
 * precise and less stable, and precision is not what it is for.
 */
export const CONTROL_HANDLE_TAGS: readonly string[] = [
  // Document skeleton
  "html", "body", "header", "footer", "main", "nav", "aside", "section",
  "article", "div", "span", "form", "fieldset",
  // Lists and tables
  "ul", "ol", "li", "table", "thead", "tbody", "tr", "td", "th",
  // Controls
  "button", "a", "input", "select", "textarea", "label", "summary", "details",
  "option", "dialog",
  // Common content boxes a control commonly sits inside
  "p", "h1", "h2", "h3", "h4", "h5", "h6", "figure", "picture", "img", "svg",
];

/** What an element whose tag is not on the closed list contributes. */
export const UNKNOWN_TAG_TOKEN = "x";

/**
 * How far up the tree a path reaches. Ten levels is past the point where more
 * depth adds stability — a control's nearest ten ancestors already place it —
 * and it is what bounds the cost of taking a handle.
 */
export const CONTROL_HANDLE_DEPTH = 10;

/**
 * How many preceding siblings are counted before the ordinal gives up. A list
 * of ten thousand rows must not cost ten thousand pointer steps per control,
 * and the hundredth row of a list is not a control a release comparison is
 * going to follow individually. Past the cap the segment carries
 * {@link BEYOND_ORDINAL_TOKEN} instead of a number, which is honest: the
 * position is "somewhere past the cap", and every control past it shares one
 * handle rather than each being given a made-up index.
 */
export const CONTROL_HANDLE_MAX_ORDINAL = 64;

/** The ordinal token for an element further along than the cap counts. */
export const BEYOND_ORDINAL_TOKEN = "n";

/** Prefix on every handle, so a handle is recognisable as one on sight. */
export const CONTROL_HANDLE_PREFIX = "c";

/**
 * The exact shape a handle has. The server refuses anything else, and the
 * kit's own transmit screen checks it before the wire — a handle is the one
 * new string the census puts on the wire, so its shape is closed rather than
 * screened.
 */
export const CONTROL_HANDLE_RE = /^c[0-9a-z]{1,8}$/;

/** Lowercase the tag and keep it only if the closed list has it. */
function tagToken(el: Element): string {
  let raw = "";
  try {
    raw = typeof el.tagName === "string" ? el.tagName.toLowerCase() : "";
  } catch {
    return UNKNOWN_TAG_TOKEN;
  }
  return CONTROL_HANDLE_TAGS.indexOf(raw) >= 0 ? raw : UNKNOWN_TAG_TOKEN;
}

/**
 * The element's ordinal among preceding siblings carrying the same token,
 * 1-based, or {@link BEYOND_ORDINAL_TOKEN} past the cap.
 *
 * Walks `previousSibling` rather than `previousElementSibling` so a host that
 * does not implement the element-only accessor still answers; non-element
 * nodes are skipped and never counted.
 */
function ordinalToken(el: Element, token: string): string {
  let seen = 0;
  let steps = 0;
  let node: Node | null = null;
  try {
    node = el.previousSibling;
  } catch {
    return "1";
  }
  while (node) {
    if (steps++ > CONTROL_HANDLE_MAX_ORDINAL * 4) return BEYOND_ORDINAL_TOKEN;
    try {
      if (node.nodeType === 1 && tagToken(node as Element) === token) {
        seen++;
        if (seen >= CONTROL_HANDLE_MAX_ORDINAL) return BEYOND_ORDINAL_TOKEN;
      }
      node = node.previousSibling;
    } catch {
      break;
    }
  }
  return String(seen + 1);
}

/**
 * The structural path, root-first, as a readable string.
 *
 * Exported for the tests that prove reproducibility and prove what it is made
 * of. It is NEVER put on the wire — {@link controlHandle} hashes it first, so
 * even the shape of a customer's markup does not travel.
 */
export function controlStructuralPath(el: unknown): string | null {
  try {
    const first = el as Node | null;
    if (!first || first.nodeType !== 1) return null;
    const segments: string[] = [];
    let node: Node | null = first;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < CONTROL_HANDLE_DEPTH) {
      const element = node as Element;
      const token = tagToken(element);
      segments.push(`${token}${ordinalToken(element, token)}`);
      depth++;
      try {
        node = element.parentNode;
      } catch {
        break;
      }
    }
    if (segments.length === 0) return null;
    return segments.reverse().join(".");
  } catch {
    // taking a handle must never break the host page
    return null;
  }
}

/** FNV-1a, 32-bit. Chosen because it is four lines, allocation-free and
 *  deterministic across every engine — not for any cryptographic property,
 *  and nothing here depends on one: the input is already text-free. */
function fnv1a(input: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    h ^= input.charCodeAt(i);
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  return h >>> 0;
}

/**
 * The control's handle: `c` followed by the base-36 digest of its structural
 * path. Null when the argument is not an element or the host threw.
 *
 * Deterministic and pure — the same markup gives the same handle on every
 * load, in every browser, for every user.
 */
export function controlHandle(el: unknown): string | null {
  const path = controlStructuralPath(el);
  if (path === null) return null;
  try {
    return CONTROL_HANDLE_PREFIX + fnv1a(path).toString(36);
  } catch {
    return null;
  }
}

/** Whether a string is shaped like a handle this kit produced. Used by the
 *  transmit screen and by the census before anything is kept. */
export function isControlHandle(value: unknown): value is string {
  return typeof value === "string" && CONTROL_HANDLE_RE.test(value);
}
