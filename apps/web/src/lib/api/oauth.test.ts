import { describe, expect, it } from "vitest";

import { formatUserCode, scopeLabel, sortScopes } from "./oauth";

describe("oauth labels", () => {
  it("orders scopes from reading to what goes further, unknown ones last", () => {
    expect(sortScopes(["ask", "zeta", "connect:repo", "read:repo"])).toEqual([
      "read:repo",
      "ask",
      "connect:repo",
      "zeta",
    ]);
  });

  it("labels every scope the server offers, and shows an unknown one as it is", () => {
    for (const scope of ["read:repo", "ask", "connect:repo"]) {
      expect(scopeLabel(scope).detail).not.toBe("");
    }
    expect(scopeLabel("other")).toEqual({ title: "other", detail: "" });
  });

  it("formats a typed user code", () => {
    expect(formatUserCode("bcdf ghjk")).toBe("BCDF-GHJK");
    expect(formatUserCode("bcd")).toBe("BCD");
  });
});
