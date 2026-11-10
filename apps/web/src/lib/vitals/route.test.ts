import { describe, expect, it } from "vitest";

import { routePattern } from "./route";

describe("routePattern", () => {
  it("names each param where its value was", () => {
    expect(routePattern("/acme/api/ask", { org: "acme", repo: "api" })).toBe("/[org]/[repo]/ask");
    expect(routePattern("/acme", { org: "acme" })).toBe("/[org]");
    expect(routePattern("/acme/settings/members", { org: "acme" })).toBe("/[org]/settings/members");
  });

  it("takes a catch-all's every segment, decoded", () => {
    expect(
      routePattern("/acme/api/code/backend/app/main%20file.py", {
        org: "acme",
        repo: "api",
        path: ["backend", "app", "main file.py"],
      }),
    ).toBe("/[org]/[repo]/code/[...path]");
  });

  it("names an id as its param, and sends a segment it can't take as _", () => {
    expect(
      routePattern("/acme/api/fixes/0199f000", { org: "acme", repo: "api", fixId: "0199f000" }),
    ).toBe("/[org]/[repo]/fixes/[fixId]");
    expect(routePattern("/acme/naïve name", { org: "acme" })).toBe("/[org]/_");
  });
});
