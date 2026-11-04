import { describe, expect, it } from "vitest";

import { validBranch } from "./handlers/repos";

describe("the mock's branch names", () => {
  it("follows git's rules, as the API does", () => {
    for (const ok of ["release", "fix#1", "a+b", "feature/x", "rélease"]) {
      expect(validBranch(ok)).toBe(true);
    }
    for (const bad of [
      "",
      "-x",
      "x.lock",
      "x/",
      ".hidden",
      "a//b",
      "x.",
      "a..b",
      "a b",
      "@",
      "a@{b",
      "a[b",
    ]) {
      expect(validBranch(bad)).toBe(false);
    }
  });
});
