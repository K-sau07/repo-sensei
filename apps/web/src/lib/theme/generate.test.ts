import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { apca, wcag } from "./contrast";
import {
  COLOR_TOKENS,
  generateTheme,
  SURFACE_TOKENS,
  TEXT_CONTRAST_MINIMUMS,
  type ThemeBase,
  type ThemeTokens,
  themeToCss,
} from "./generate";

const BASES: ThemeBase[] = ["dark", "light"];
const CONTRASTS = [0, 0.5, 1];
const ACCENT_HUES = [null, ...Array.from({ length: 24 }, (_, i) => i * 15)];

// WCAG 2 AA for normal-size text; the spec targets both APCA and WCAG 2.2 AA.
const WCAG_TEXT_MINIMUM = 4.5;

function themes(): { label: string; base: ThemeBase; tokens: ThemeTokens }[] {
  return BASES.flatMap((base) =>
    CONTRASTS.flatMap((contrast) =>
      ACCENT_HUES.map((accentHue) => ({
        label: `${base} contrast=${contrast} accent=${accentHue ?? "neutral"}`,
        base,
        tokens: generateTheme({ base, accentHue, contrast }),
      })),
    ),
  );
}

function cssBlock(css: string, selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  if (start === -1) throw new Error(`missing block ${selector}`);
  const body = css.slice(start, css.indexOf("}", start));
  const entries = [...body.matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)].map((m): [string, string] => [
    m[1] ?? "",
    (m[2] ?? "").trim(),
  ]);
  return Object.fromEntries(entries);
}

describe("theme contrast", () => {
  it.each(themes())("text tokens meet APCA minimums on every surface: $label", ({ tokens }) => {
    const failures: string[] = [];
    for (const [token, minimum] of Object.entries(TEXT_CONTRAST_MINIMUMS)) {
      for (const surface of SURFACE_TOKENS) {
        const lc = apca(tokens[token as keyof ThemeTokens], tokens[surface]);
        if (lc < minimum) failures.push(`${token} on ${surface}: Lc ${lc.toFixed(1)} < ${minimum}`);
      }
    }
    expect(failures).toEqual([]);
  });

  it.each(themes())("text tokens meet WCAG 2 AA on every surface: $label", ({ tokens }) => {
    const failures: string[] = [];
    for (const token of Object.keys(TEXT_CONTRAST_MINIMUMS)) {
      for (const surface of SURFACE_TOKENS) {
        const ratio = wcag(tokens[token as keyof ThemeTokens], tokens[surface]);
        if (ratio < WCAG_TEXT_MINIMUM) failures.push(`${token} on ${surface}: ${ratio.toFixed(2)}`);
      }
    }
    expect(failures).toEqual([]);
  });

  it.each(themes())("text on a brand fill is readable: $label", ({ tokens }) => {
    expect(apca(tokens["brand-fg"], tokens.brand)).toBeGreaterThanOrEqual(60);
    expect(wcag(tokens["brand-fg"], tokens.brand)).toBeGreaterThanOrEqual(WCAG_TEXT_MINIMUM);
  });
});

describe("theme generator", () => {
  it("keeps signal fixed whatever the accent", () => {
    const signals = new Set(
      ACCENT_HUES.map((h) => generateTheme({ base: "dark", accentHue: h }).signal),
    );
    expect(signals.size).toBe(1);
  });

  it("raises contrast monotonically", () => {
    for (const base of BASES) {
      const lows = generateTheme({ base, contrast: 0 });
      const highs = generateTheme({ base, contrast: 1 });
      for (const token of ["text-muted", "text-faint"] as const) {
        expect(apca(highs[token], highs.bg)).toBeGreaterThan(apca(lows[token], lows.bg));
      }
    }
  });

  it("clamps out-of-range inputs", () => {
    expect(generateTheme({ base: "dark", contrast: 7 })).toEqual(
      generateTheme({ base: "dark", contrast: 1 }),
    );
    expect(generateTheme({ base: "light", accentHue: 370 }).brand).toBe(
      generateTheme({ base: "light", accentHue: 10 }).brand,
    );
  });

  it("serializes every token for inline overrides", () => {
    const css = themeToCss(generateTheme({ base: "dark" }));
    for (const token of COLOR_TOKENS) expect(css).toContain(`--${token}: oklch(`);
  });
});

describe("tokens.css", () => {
  const css = readFileSync(path.join(import.meta.dirname, "../../styles/tokens.css"), "utf8");

  it.each([
    ["dark", ':root,\n[data-theme="dark"]'],
    ["light", '[data-theme="light"]'],
  ] as const)("matches the generator defaults (%s)", (base, selector) => {
    const declared = cssBlock(css, selector);
    const expected = generateTheme({ base });
    for (const token of COLOR_TOKENS) expect(declared[token], token).toBe(expected[token]);
  });
});
