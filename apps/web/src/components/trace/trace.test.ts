import { describe, expect, it } from "vitest";

import type { Trace } from "@/lib/api/trace";
import { overviewTraceHref, pathIndex, traceHref } from "@/lib/trace/url-state";
import fixture from "@/mocks/fixtures/trace.json";
import { GAP_ENTRY, LOGIN_ENTRY } from "@/mocks/trace";

import { beamPath } from "./trace-beam";
import { gapLane, hopSummary, lanesOf } from "./trace-swimlane";

const traces = fixture.traces as unknown as Record<string, Trace>;
const login = traces[LOGIN_ENTRY]?.paths[0];
const gap = traces[GAP_ENTRY]?.paths[0];

function hopAt(i: number) {
  const hop = login?.hops[i];
  if (!hop) throw new Error("no such hop");
  return hop;
}

describe("the swimlanes", () => {
  it("are every layer a request crosses, in its order, and 'other' only when used", () => {
    if (!login) throw new Error("no Login trace");
    expect(lanesOf(login)).toEqual(["ui", "state", "client", "route", "handler", "service", "db"]);
    expect(lanesOf({ ...login, hops: [{ ...hopAt(0), layer: "other" }] })).toContain("other");
  });

  it("put a gap in the lane after the one its path stops in", () => {
    expect(gapLane("client")).toBe("route");
    expect(gapLane("handler")).toBe("service");
    expect(gapLane("db")).toBe("db");
    const last = gap?.hops.at(-1);
    expect(last?.layer).toBe("client");
  });

  it("say each hop in words, for screen readers", () => {
    if (!login) throw new Error("no Login trace");
    expect(hopSummary(login, 0)).toBe(
      "Step 1 of 9, Component, component Login, entry, frontend/src/routes/login.tsx:53",
    );
    expect(hopSummary(login, 1)).toBe(
      "Step 2 of 9, Hook / client, hook useAuth, uses, frontend/src/routes/login.tsx:54",
    );
  });
});

describe("the beam", () => {
  it("goes through each card, curving from one card's right to the next one's left", () => {
    expect(
      beamPath([
        { left: 0, right: 10, y: 5, layer: "ui" },
        { left: 30, right: 40, y: 25, layer: "state" },
      ]),
    ).toBe("M0 5L10 5C20 5 20 25 30 25L40 25");
    expect(beamPath([])).toBe("");
  });
});

describe("the trace's URLs", () => {
  it("leave defaults out, and pick a path tab within the paths there are", () => {
    expect(traceHref("acme", "api")).toBe("/acme/api/trace");
    expect(traceHref("acme", "api", { from: "Login", path: 2, view: "list" })).toBe(
      "/acme/api/trace?from=Login&path=2&view=list",
    );
    expect(overviewTraceHref("acme", "api", "Login", 1)).toBe("/acme/api?trace=Login");
    expect(pathIndex(2, 3)).toBe(1);
    expect(pathIndex(4, 3)).toBe(0);
    expect(pathIndex(0, 3)).toBe(0);
  });
});
