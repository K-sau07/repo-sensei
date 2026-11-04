import { describe, expect, it } from "vitest";

import { appearanceCss, DEFAULT_APPEARANCE, readAppearance } from "./appearance";

describe("appearance", () => {
  it("reads what was saved, and the default for anything it can't", () => {
    expect(readAppearance(null)).toEqual(DEFAULT_APPEARANCE);
    expect(readAppearance("not json")).toEqual(DEFAULT_APPEARANCE);
    expect(readAppearance('{"accentHue": 400, "contrast": 3, "density": "tiny"}')).toEqual(
      DEFAULT_APPEARANCE,
    );
    expect(
      readAppearance('{"accentHue": 255, "contrast": 1, "density": "compact", "motion": "reduce"}'),
    ).toEqual({ ...DEFAULT_APPEARANCE, accentHue: 255, contrast: 1, density: "compact",
                motion: "reduce" }); // prettier-ignore
  });

  it("writes no palette for the default, and one for each base otherwise", () => {
    expect(appearanceCss(DEFAULT_APPEARANCE)).toBe("");
    const css = appearanceCss({ ...DEFAULT_APPEARANCE, accentHue: 150 });
    expect(css).toContain('[data-theme="dark"][data-theme]{');
    expect(css).toContain('[data-theme="light"][data-theme]{');
    expect(css).toContain("--brand: oklch(");
    expect(css).not.toContain("<");
  });
});
