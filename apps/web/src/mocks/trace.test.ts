import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { LAYERS } from "@/lib/api/search";
import { downHops, returnGroups, TRACE_LAYERS, type Trace } from "@/lib/api/trace";

import { BROWSE_COMMIT } from "./browse";
import { db, resetMockDb } from "./db";
import content from "./fixtures/browse-content.json";
import fixture from "./fixtures/trace.json";
import { TRACE_SCENARIO_KEY } from "./handlers/trace";
import { runEvents } from "./index-run";
import { server } from "./node";
import { AMBIGUOUS_ENTRY, GAP_ENTRY, LOGIN_ENTRY } from "./trace";

const api = createApiClient({
  baseUrl: "http://api.test",
  getCsrfToken: () => "csrf-token",
});
const path = { org: "acme", repo: "api" };
const TRACE = "/api/v1/orgs/{org}/repos/{repo}/trace";

beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" });
});
afterEach(() => {
  vi.unstubAllGlobals();
  server.resetHandlers();
  resetMockDb();
});
afterAll(() => {
  server.close();
});

async function failure(call: Promise<unknown>): Promise<ApiError> {
  try {
    await call;
  } catch (error) {
    if (error instanceof ApiError) return error;
    throw error;
  }
  throw new Error("expected the call to fail");
}

/** A scenario, as the browser's localStorage would hold it. */
function scenario(value: string) {
  const store = new Map([[TRACE_SCENARIO_KEY, value]]);
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    removeItem: (key: string) => store.delete(key),
  });
}

const files = content.files as Record<string, string>;
const traces = fixture.traces as unknown as Record<string, Trace>;
const all = [...Object.values(traces), ...(Object.values(fixture.unlinked) as unknown as Trace[])];

describe("the mock traces", () => {
  it("are the API's shape: lanes, layers, paths of at most three, one hop per node", () => {
    for (const trace of all) {
      expect(trace.paths.length).toBeGreaterThan(0);
      expect(trace.paths.length).toBeLessThanOrEqual(3);
      for (const path of trace.paths) {
        // The return leg (back up to what the client does with the response) comes after the
        // way down, and is no part of its sureness.
        const down = downHops(path);
        const [entry, ...rest] = down;
        expect(entry?.edge).toBeNull();
        for (const hop of rest) {
          expect(hop.edge).not.toBeNull();
          expect(hop.edge?.confidence).toBeGreaterThan(0);
          expect(hop.edge?.confidence).toBeLessThanOrEqual(1);
        }
        for (const hop of path.hops) {
          expect(TRACE_LAYERS).toContain(hop.layer);
          expect(LAYERS).toContain(hop.node.lane);
        }
        const keys = down.map((hop) => hop.node.symbol_key);
        expect(new Set(keys).size).toBe(keys.length);
        const product = rest.reduce((p, hop) => p * (hop.edge?.confidence ?? 1), 1);
        expect(path.path_confidence).toBeCloseTo(product, 3);
        for (const gap of path.breaks) expect(gap.after_hop).toBeLessThan(down.length);
        for (const group of returnGroups(path)) {
          expect(keys).toContain(group.hop.node.symbol_key);
          expect(group.outcomes.length).toBeGreaterThan(0);
          for (const { hop } of group.outcomes) {
            expect(hop.edge?.type).toBe("produces");
            expect(hop.node.outcome).toBeTruthy();
          }
        }
      }
    }
  });

  it("hold no local paths, only the repository's", () => {
    const text = JSON.stringify(fixture);
    expect(text).not.toMatch(/"\/(?:Users|home|tmp|var|private)\//);
    for (const trace of all) {
      for (const hop of trace.paths.flatMap((p) => p.hops)) {
        if (hop.node.path) expect(hop.node.path.startsWith("/")).toBe(false);
        if (hop.edge?.evidence) expect(hop.edge.evidence.path.startsWith("/")).toBe(false);
      }
    }
  });

  it("go from the Login page to the user table, every hop's file served by the mock", () => {
    const [best] = traces[LOGIN_ENTRY]?.paths ?? [];
    if (!best) throw new Error("no Login trace");
    expect(best.reaches_goal).toBe(true);
    expect(downHops(best).map((hop) => hop.layer)).toEqual([
      "ui",
      "state",
      "ui",
      "client",
      "route",
      "handler",
      "db",
      "db",
      "db",
    ]);
    expect(downHops(best).at(-1)?.node).toMatchObject({ kind: "table", name: "user" });
    // And back up: useAuth stores the token and goes home; its onError hands the error on.
    expect(
      returnGroups(best).map((group) => [
        group.hop.node.name,
        group.outcomes.map(({ hop }) => hop.node.outcome?.kind),
      ]),
    ).toEqual([["useAuth", ["storage", "navigate", "error"]]]);
    for (const hop of best.hops) {
      if (hop.node.path) expect(files[hop.node.path], hop.node.path).toBeDefined();
      if (hop.edge?.evidence) expect(files[hop.edge.evidence.path]).toBeDefined();
    }
  });

  it("have a gap with the reason 3.10 found, and an endpoint two routes could be", () => {
    const [gap] = traces[GAP_ENTRY]?.paths ?? [];
    expect(gap?.reaches_goal).toBe(false);
    expect(gap?.breaks).toEqual([
      {
        after_hop: 2,
        reason: "client call has no matching endpoint",
        detail: "no POST endpoint at /api/v1/items/export (it has DELETE, GET, PUT)",
      },
    ]);
    const ambiguous = traces[AMBIGUOUS_ENTRY]?.paths ?? [];
    const endpoints = ambiguous.map((p) => p.hops.find((hop) => hop.layer === "route"));
    expect(endpoints.every((hop) => (hop?.edge?.alternatives ?? 0) >= 2)).toBe(true);
    expect(new Set(endpoints.map((hop) => hop?.node.symbol_key)).size).toBe(2);
  });
});

describe("GET /trace", () => {
  it("answers the fixture's trace by key, file or name, with the commit", async () => {
    const { data, response } = await api.GET(TRACE, {
      params: { path, query: { from: LOGIN_ENTRY } },
    });
    expect(response.headers.get("X-Commit-Sha")).toBe(BROWSE_COMMIT);
    expect(data).toEqual(traces[LOGIN_ENTRY]);
    expect(data?.linked).toBe(true);
    for (const other of ["frontend/src/routes/login.tsx", "Login"]) {
      const { data: same } = await api.GET(TRACE, { params: { path, query: { from: other } } });
      expect(same).toEqual(data);
    }
  });

  it("refuses a missing or out-of-range field, and an entry the graph doesn't have", async () => {
    expect(await failure(api.GET(TRACE, { params: { path, query: { from: "" } } }))).toMatchObject({
      status: 422,
      code: "validation_failed",
    });
    expect(
      await failure(api.GET(TRACE, { params: { path, query: { from: "Login", min_conf: 2 } } })),
    ).toMatchObject({ status: 422, code: "validation_failed" });
    expect(
      await failure(api.GET(TRACE, { params: { path, query: { from: "py nope.py/x()." } } })),
    ).toMatchObject({ status: 404, code: "trace_entry_not_found" });
  });

  it("is 409 graph_not_ready while the first run hasn't linked the files", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      const repo = db.repos.acme?.find((r) => r.slug === "api");
      if (!repo) throw new Error("no seeded repository");
      repo.last_indexed_sha = null;
      const discovered =
        runEvents().find((e) => e.type === "stage_done" && e.stage === "discover")?.at ?? 0;
      const queued = Date.now() - discovered - 10;
      db.jobs[repo.id] = [
        {
          id: "first",
          repo_id: repo.id,
          created_at: new Date(queued).toISOString(),
          queued_ms: queued,
        },
      ];
      const error = await failure(api.GET(TRACE, { params: { path, query: { from: "Login" } } }));
      expect(error).toMatchObject({ status: 409, code: "graph_not_ready" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("is 429 with Retry-After when rate limited, and unlinked before the links stage", async () => {
    scenario("rate-limited");
    expect(
      await failure(api.GET(TRACE, { params: { path, query: { from: "Login" } } })),
    ).toMatchObject({ status: 429, code: "rate_limited", retryAfter: 30 });
    scenario("unlinked");
    const { data } = await api.GET(TRACE, { params: { path, query: { from: "Login" } } });
    expect(data?.linked).toBe(false);
    expect(data?.paths.every((p) => p.hops.every((hop) => hop.layer !== "client"))).toBe(true);
  });
});

describe("POST /trace", () => {
  it("takes the same entry in its body", async () => {
    const { data } = await api.POST(TRACE, {
      params: { path },
      body: { from: GAP_ENTRY, kind: "auto", min_conf: 0, direction: "both" },
    });
    expect(data).toEqual(traces[GAP_ENTRY]);
    expect(
      await failure(
        api.POST(TRACE, {
          params: { path },
          body: { from: GAP_ENTRY, kind: "nope" as "auto", min_conf: 0, direction: "both" },
        }),
      ),
    ).toMatchObject({ status: 422, code: "validation_failed" });
  });
});
