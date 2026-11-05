import { describe, expect, it } from "vitest";

import { effortLabel, modelName, recommended, tierLabel } from "./models";

const MODELS = { fast: "claude-haiku-5-5", standard: "claude-sonnet-5-5", deep: "claude-opus-5-5" };

describe("models settings", () => {
  it("names a model as people do, and an id it doesn't know as it is", () => {
    expect(modelName("claude-opus-5-5")).toBe("Opus 5.5");
    expect(modelName("claude-sonnet-5")).toBe("Sonnet 5");
    expect(modelName("my-own-model")).toBe("my-own-model");
  });

  it("says what each step runs on as recommended, with the deployment's models", () => {
    expect(recommended("answers", MODELS)).toMatch(/^Sonnet 5\.5, .*Opus 5\.5 at high effort/);
    expect(recommended("diagrams", MODELS)).toMatch(/^Haiku 5\.5 at low effort/);
    expect(recommended("fixes", MODELS)).toContain("verify every patch");
  });

  it("labels a choice as the floor it is", () => {
    expect(tierLabel("recommended", MODELS)).toBe("Recommended");
    expect(tierLabel("deep", MODELS)).toBe("At least Opus 5.5");
    expect(effortLabel("high")).toBe("At least high");
  });
});
