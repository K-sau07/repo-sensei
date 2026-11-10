import { readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { ROUTE_WORDS, routePattern } from "./route";

describe("routePattern", () => {
  it("names each param where its value was", () => {
    expect(routePattern("/acme/api/ask", { org: "acme", repo: "api" })).toBe("/[org]/[repo]/ask");
    expect(routePattern("/acme", { org: "acme" })).toBe("/[org]");
    expect(routePattern("/acme/settings/members", { org: "acme" })).toBe("/[org]/settings/members");
    expect(
      routePattern("/acme/api/fixes/0199f000", { org: "acme", repo: "api", fixId: "0199f000" }),
    ).toBe("/[org]/[repo]/fixes/[fixId]");
  });

  it("matches params as Next gives them, still percent-encoded", () => {
    const path = "/acme/api/code/src/app/%5Borg%5D/main%20file.py";
    expect(
      routePattern(path, {
        org: "acme",
        repo: "api",
        path: ["src", "app", "%5Borg%5D", "main%20file.py"],
      }),
    ).toBe("/[org]/[repo]/code/[...path]");
  });

  it("sends anything that isn't a param or a route word as _", () => {
    // A catch-all that didn't match (whatever the reason) gives no directory's name away.
    expect(routePattern("/acme/api/code/backend/app/main.py", { org: "acme", repo: "api" })).toBe(
      "/[org]/[repo]/code/_/_/_",
    );
    expect(routePattern("/acme/naïve name", { org: "acme" })).toBe("/[org]/_");
  });

  it("knows every route word of the app's pages", () => {
    const words = new Set<string>();
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        const name = entry.name;
        if (!name.startsWith("[") && !name.startsWith("(") && !name.startsWith("_")) {
          words.add(name);
        }
        walk(join(dir, name));
      }
    };
    walk(join(__dirname, "../../app/(app)/[org]"));
    expect([...words].filter((word) => !ROUTE_WORDS.has(word))).toEqual([]);
  });
});
