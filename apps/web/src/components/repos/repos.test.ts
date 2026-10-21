import { describe, expect, it } from "vitest";

import { formatSize } from "./connect-repo";

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
