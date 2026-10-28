import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { LAYERS } from "@/lib/api/search";

import { BROWSE_COMMIT } from "./browse";
import { db, resetMockDb } from "./db";
import content from "./fixtures/browse-content.json";
import fixture from "./fixtures/graph.json";
import { runEvents } from "./index-run";
import { server } from "./node";

const api = createApiClient({
  baseUrl: "http://api.test",
  getCsrfToken: () => null,
});
const path = { org: "acme", repo: "api" };
const NEIGHBORS = "/api/v1/orgs/{org}/repos/{repo}/graph/neighbors";
const PATH = "/api/v1/orgs/{org}/repos/{repo}/graph/path";
const OUTLINE = "/api/v1/orgs/{org}/repos/{repo}/symbols/outline";

const GET_DB = "py backend/app/api/deps.py/get_db().";
const ENDPOINT = "api_endpoint GET /api/v1/items";
const TABLE = "table item";

beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" });
});
afterEach(() => {
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

const files = content.files as Record<string, string>;

describe("the mock graph", () => {
  it("has its definitions at lines of files the mock serves, and edges between its nodes", () => {
    const keys = new Set(fixture.nodes.map((node) => node.symbol_key));
    expect(keys.size).toBe(fixture.nodes.length);
    for (const node of fixture.nodes) {
      expect(LAYERS, node.symbol_key).toContain(node.lane);
      if (node.path === null) continue;
      const text = files[node.path];
      expect(text, node.path).toBeDefined();
      const lines = (text ?? "").split("\n").length;
      expect(node.start_line, node.symbol_key).toBeGreaterThanOrEqual(1);
      expect(node.end_line, node.symbol_key).toBeLessThanOrEqual(lines);
    }
    for (const edge of fixture.edges) {
      expect(keys.has(edge.src) && keys.has(edge.dst), `${edge.src} → ${edge.dst}`).toBe(true);
      expect(edge.confidence).toBeGreaterThan(0);
      expect(edge.confidence).toBeLessThanOrEqual(1);
    }
  });

  it("crosses the layers: an endpoint is handled by a route that reads a table", () => {
    const has = (src: string, type: string, dst: string) =>
      fixture.edges.some((e) => e.src === src && e.type === type && e.dst === dst);
    expect(has(ENDPOINT, "handled_by", "py backend/app/api/routes/items.py/read_items().")).toBe(
      true,
    );
    expect(has("py backend/app/api/routes/items.py/read_items().", "reads", TABLE)).toBe(true);
  });
});

describe("GET /graph/neighbors", () => {
  it("answers a symbol's callers, each once, surest first, with the commit", async () => {
    const { data, response } = await api.GET(NEIGHBORS, {
      params: { path, query: { symbol: GET_DB, direction: "in", types: "injects" } },
    });
    expect(response.headers.get("X-Commit-Sha")).toBe(BROWSE_COMMIT);
    expect(data?.root).toMatchObject({ symbol_key: GET_DB, name: "get_db", lane: "api" });
    expect(data?.root).not.toHaveProperty("docstring");
    const hops = data?.hops ?? [];
    expect(hops.length).toBe(20);
    expect(data?.truncated).toBe(false);
    const keys = hops.map((hop) => hop.node.symbol_key);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toContain("py backend/app/api/routes/items.py/read_items().");
    for (const hop of hops) {
      expect(hop).toMatchObject({ depth: 1, via: GET_DB, edge: { type: "injects" } });
      expect(hop.edge.direction).toBe("in");
    }
    const order = hops.map((hop) => [-hop.edge.confidence, hop.node.symbol_key] as const);
    expect(order).toEqual([...order].sort((a, b) => a[0] - b[0] || (a[1] < b[1] ? -1 : 1)));
  });

  it("goes deeper level by level without coming back, and says when the limit cut it", async () => {
    const deep = await api.GET(NEIGHBORS, {
      params: { path, query: { symbol: ENDPOINT, direction: "out", depth: 2 } },
    });
    const hops = deep.data?.hops ?? [];
    expect(hops[0]).toMatchObject({ depth: 1, edge: { type: "handled_by" } });
    expect(hops.some((hop) => hop.depth === 2 && hop.node.symbol_key === TABLE)).toBe(true);
    expect(hops.map((hop) => hop.depth)).toEqual([...hops.map((hop) => hop.depth)].sort());
    expect(hops.map((hop) => hop.node.symbol_key)).not.toContain(ENDPOINT);

    const cut = await api.GET(NEIGHBORS, {
      params: { path, query: { symbol: GET_DB, direction: "in", limit: 5 } },
    });
    expect(cut.data?.hops).toHaveLength(5);
    expect(cut.data?.truncated).toBe(true);
  });

  it("refuses unknown types and out-of-range fields, and an unknown symbol", async () => {
    expect(
      await failure(
        api.GET(NEIGHBORS, { params: { path, query: { symbol: GET_DB, types: "calls,eats" } } }),
      ),
    ).toMatchObject({ status: 422, code: "invalid_types" });
    expect(
      await failure(api.GET(NEIGHBORS, { params: { path, query: { symbol: GET_DB, depth: 4 } } })),
    ).toMatchObject({ status: 422, code: "validation_failed" });
    expect(
      await failure(api.GET(NEIGHBORS, { params: { path, query: { symbol: "py nope/x()." } } })),
    ).toMatchObject({ status: 404, code: "symbol_not_found" });
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
      const error = await failure(
        api.GET(NEIGHBORS, { params: { path, query: { symbol: GET_DB } } }),
      );
      expect(error).toMatchObject({ status: 409, code: "graph_not_ready" });
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("GET /graph/path", () => {
  it("is the surest chain from one node to another", async () => {
    const { data } = await api.GET(PATH, {
      params: { path, query: { from: ENDPOINT, to: TABLE } },
    });
    expect(data?.found).toBe(true);
    const hops = data?.hops ?? [];
    expect(hops.map((hop) => hop.edge.type)).toEqual(["handled_by", "reads"]);
    let at = ENDPOINT;
    for (const [i, hop] of hops.entries()) {
      expect(hop).toMatchObject({ via: at, depth: i + 1, edge: { direction: "out" } });
      at = hop.node.symbol_key;
    }
    expect(at).toBe(TABLE);
    const product = hops.reduce((p, hop) => p * hop.edge.confidence, 1);
    expect(data?.confidence).toBeCloseTo(product, 4);
  });

  it("is not found against the edges, and 404 for an unknown end", async () => {
    const back = await api.GET(PATH, { params: { path, query: { from: TABLE, to: ENDPOINT } } });
    expect(back.data).toEqual({ found: false, hops: [], confidence: 0 });
    expect(
      await failure(api.GET(PATH, { params: { path, query: { from: ENDPOINT, to: "nope" } } })),
    ).toMatchObject({ status: 404, code: "symbol_not_found" });
  });
});

describe("GET /symbols/outline", () => {
  it("lists a file's definitions in order, without the module", async () => {
    const { data } = await api.GET(OUTLINE, {
      params: { path, query: { path: "backend/app/api/deps.py" } },
    });
    const symbols = data?.symbols ?? [];
    expect(symbols.map((s) => s.name)).toContain("get_db");
    expect(symbols.every((s) => s.kind !== "module")).toBe(true);
    const starts = symbols.map((s) => s.start_line);
    expect(starts).toEqual([...starts].sort((a, b) => a - b));
    expect(data?.truncated).toBe(false);
  });

  it("is 404 path_not_found for a file that isn't there", async () => {
    expect(
      await failure(api.GET(OUTLINE, { params: { path, query: { path: "backend/nope.py" } } })),
    ).toMatchObject({ status: 404, code: "path_not_found" });
  });
});
