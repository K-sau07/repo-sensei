import { describe, expect, it } from "vitest";

import { formatDelta, formatMetric, itemMetric, lowerIsBetter, runKind } from "./evals";

const run = (metrics: Record<string, unknown>, retriever = "hybrid", ks = [10]) => ({
  metrics,
  retriever,
  config: { ks },
});

describe("evals dashboard", () => {
  it("compares items on the score of the run's kind, as the API does", () => {
    expect(runKind(run({ fixbench: true }))).toBe("fixbench");
    expect(itemMetric(run({ fixbench: true }))).toBe("fixed");
    expect(itemMetric(run({ bench: true }))).toBe("hop_recall");
    expect(itemMetric(run({}, "ask:fast"))).toBe("checklist");
    expect(itemMetric(run({}, "hybrid", [5, 50]))).toBe("recall_symbol@5");
    expect(itemMetric(run({}, "hybrid", [10, 50]))).toBe("recall_symbol@10");
  });

  it("knows cost, time, calibration and errors are better lower", () => {
    for (const name of ["cost_usd", "p50_ms", "hop_ece", "path_brier", "unsafe", "contradicted"]) {
      expect(lowerIsBetter(name)).toBe(true);
    }
    expect(lowerIsBetter("recall_symbol@10")).toBe(false);
  });

  it("writes scores, costs and changes as they read", () => {
    expect(formatMetric("recall_symbol@10", 0.6666)).toBe("0.667");
    expect(formatMetric("cost_usd", 1.4)).toBe("$1.40");
    expect(formatMetric("p50_ms", 140.4)).toBe("140 ms");
    expect(formatMetric("x", null)).toBe("—");
    expect(formatDelta({ name: "hop_ece", before: 0.08, after: 0.08, delta: 0 })).toBe("0");
    expect(formatDelta({ name: "cost_usd", before: 0.4, after: 0.6, delta: 0.2 })).toBe("+$0.20");
    expect(formatDelta({ name: "hop_recall", before: 1, after: 0.75, delta: -0.25 })).toBe(
      "−0.250",
    );
  });
});
