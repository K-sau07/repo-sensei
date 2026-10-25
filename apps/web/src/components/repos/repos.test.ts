import { describe, expect, it } from "vitest";

import { formatCost, formatSize } from "./connect-repo";

describe("repository sizes", () => {
  it.each([
    [0, "1 kB"],
    [12, "12 kB"],
    [1023, "1023 kB"],
    [3482, "3.4 MB"],
    [49_152, "48 MB"],
    [1_258_291, "1.2 GB"],
  ])("%d KiB reads as %s", (sizeKb, shown) => {
    expect(formatSize(sizeKb)).toBe(shown);
  });
});

describe("an index's cost", () => {
  it("is shown when every model is priced", () => {
    expect(formatCost({ cost_usd: "0.124", unpriced: [] })).toBe("~$0.12");
    expect(formatCost({ cost_usd: "3", unpriced: [] })).toBe("~$3.00");
  });

  it("is unknown when a model has no price", () => {
    expect(formatCost({ cost_usd: "0.12", unpriced: ["claude-unknown-1"] })).toBe("Unknown");
  });
});
