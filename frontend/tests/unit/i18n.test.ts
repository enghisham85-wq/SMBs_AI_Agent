import ar from "../../src/i18n/ar.json";
import en from "../../src/i18n/en.json";

// Plural suffixes differ (ar adds zero/two/few/many), so keys are compared without them.
const PLURAL = /_(zero|one|two|few|many|other)$/;

function keys(obj: Record<string, unknown>, prefix = ""): Set<string> {
  const out = new Set<string>();
  for (const [k, v] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object") keys(v as Record<string, unknown>, path).forEach((x) => out.add(x));
    else out.add(path.replace(PLURAL, ""));
  }
  return out;
}

const SOURCES = import.meta.glob("../../src/**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const enKeys = keys(en);
const arKeys = keys(ar);

describe("Arabic interface", () => {
  it("has an Arabic string for every English key", () => {
    expect([...enKeys].filter((k) => !arKeys.has(k))).toEqual([]);
  });

  it("has no Arabic keys the English file lacks", () => {
    expect([...arKeys].filter((k) => !enKeys.has(k))).toEqual([]);
  });

  it("defines every literal key used with t()", () => {
    const used = new Set<string>();
    for (const text of Object.values(SOURCES)) {
      for (const m of text.matchAll(/\bt\(\s*"([a-z_]+(?:\.[a-z0-9_]+)+)"/g)) used.add(m[1]);
    }
    expect(used.size).toBeGreaterThan(100); // sanity check on the scan
    expect([...used].filter((k) => !enKeys.has(k)).sort()).toEqual([]);
  });

  it("marks Arabic strings as Arabic text", () => {
    expect(ar.nav.home).toMatch(/[؀-ۿ]/);
    expect(ar.scorecard.supplier).toMatch(/[؀-ۿ]/);
  });
});
