import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { duration, durationMs, ease, pressScale } from "./tokens";

const css = readFileSync(path.join(import.meta.dirname, "../../styles/tokens.css"), "utf8");

function declared(name: string): string {
  const match = new RegExp(`--${name}:\\s*([^;]+);`).exec(css);
  if (!match?.[1]) throw new Error(`--${name} is not declared in tokens.css`);
  return match[1].trim();
}

describe("motion tokens", () => {
  it.each(Object.entries(durationMs))("--dur-%s matches", (name, ms) => {
    expect(declared(`dur-${name}`)).toBe(`${ms}ms`);
  });

  it("converts durations to seconds for Motion", () => {
    expect(duration.fast).toBe(0.12);
    expect(duration.signature).toBe(0.7);
  });

  it("matches the easing curves", () => {
    expect(declared("ease-out-ui")).toBe(`cubic-bezier(${ease.out.join(", ")})`);
    expect(declared("ease-inout")).toBe(`cubic-bezier(${ease.inOut.join(", ")})`);
  });

  it("matches the press scale", () => {
    expect(declared("press-scale")).toBe(String(pressScale));
  });
});
