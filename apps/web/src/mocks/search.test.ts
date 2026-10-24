import type { Client } from "openapi-fetch";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import {
  LAYERS,
  type SearchPaths,
  type SearchRequest,
  type SearchResponse,
} from "@/lib/api/search";

import { BROWSE_COMMIT, contentFiles } from "./browse";
import { db, resetMockDb } from "./db";
import content from "./fixtures/browse-content.json";
import { startJob } from "./jobs";
import { server } from "./node";
import {
  checkBody,
  globTest,
  grep,
  indexFiles,
  linesOf,
  readQuery,
  search,
  SearchError,
  type Indexed,
} from "./search";

const client = createApiClient({ baseUrl: "http://api.test", getCsrfToken: () => null });
const api = client as unknown as Client<SearchPaths>;
const path = { params: { path: { org: "acme", repo: "api" } } };

beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" });
});
afterEach(() => {
  server.resetHandlers();
  resetMockDb();
  vi.useRealTimers();
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

const fileLines = new Map(
  Object.entries(content.files as Record<string, string>).map(([p, text]) => [p, linesOf(text)]),
);

let files: Indexed[] = [];
beforeAll(async () => {
  files = indexFiles(await contentFiles());
});

const run = (body: SearchRequest, semanticReady = true) =>
  search(files, body, { semanticReady, commit: BROWSE_COMMIT });

const hitsOf = (response: SearchResponse) => LAYERS.flatMap((layer) => response.lanes[layer]);

/** Every hit is on lines that exist in the file the mock serves, its snippet is those lines (at
 * most 12, inside the hit), and every highlight is inside a snippet line. */
function expectRealLines(response: SearchResponse) {
  for (const hit of hitsOf(response)) {
    const lines = fileLines.get(hit.path);
    expect(lines, hit.path).toBeDefined();
    if (!lines) continue;
    expect(hit.start_line, hit.id).toBeGreaterThanOrEqual(1);
    expect(hit.end_line, hit.id).toBeGreaterThanOrEqual(hit.start_line);
    expect(hit.end_line, hit.id).toBeLessThanOrEqual(lines.length);
    const { snippet } = hit;
    expect(snippet.lines.length, hit.id).toBeGreaterThan(0);
    expect(snippet.lines.length, hit.id).toBeLessThanOrEqual(12);
    expect(snippet.start_line, hit.id).toBeGreaterThanOrEqual(hit.start_line);
    expect(snippet.start_line + snippet.lines.length - 1, hit.id).toBeLessThanOrEqual(hit.end_line);
    expect(snippet.lines, hit.id).toEqual(
      lines.slice(snippet.start_line - 1, snippet.start_line - 1 + snippet.lines.length),
    );
    for (const mark of snippet.highlights) {
      const text = snippet.lines[mark.line - snippet.start_line];
      expect(text, `${hit.id} line ${String(mark.line)}`).toBeDefined();
      expect(mark.start).toBeGreaterThanOrEqual(0);
      expect(mark.end).toBeGreaterThan(mark.start);
      expect(mark.end).toBeLessThanOrEqual(text?.length ?? 0);
    }
    expect(hit.why.length, hit.id).toBeGreaterThan(0);
    expect(hit.score).toBeGreaterThan(0);
    expect(hit.score).toBeLessThanOrEqual(1);
    if (hit.symbol_key) expect(hit.id).toBe(hit.symbol_key);
    else expect(hit.id).toBe(`${hit.path}#L${String(hit.start_line)}-${String(hit.end_line)}`);
  }
}

const QUERIES = [
  "create_user",
  "how are passwords hashed",
  '"SECRET_KEY"',
  "def \\w+_token",
  "login",
  "user email",
  "sidebar",
  "backend/app/api delete item",
  "fastapi",
  "© copyright",
  "deploy docker compose",
];

describe("the mock search", () => {
  it("only answers with lines that exist, for every kind of query, mode and filter", () => {
    for (const q of QUERIES) {
      expectRealLines(run({ q }));
      expectRealLines(run({ q }, false));
      expectRealLines(run({ q, mode: "keyword", limit: 50 }));
      expectRealLines(run({ q, mode: "semantic", limit: 50 }));
      expectRealLines(run({ q, layers: ["api", "db"], langs: ["python"], path: "backend/" }));
    }
  });

  it("finds create_user where it is defined, by the symbol leg first", () => {
    const response = run({ q: "create_user" });
    expect(response.interpretation.class).toBe("symbol");
    const api = response.lanes.api[0];
    expect(api).toMatchObject({
      name: "create_user",
      kind: "function",
      path: "backend/app/api/routes/users.py",
      start_line: 54,
      end_line: 78,
      layer: "api",
    });
    expect(api?.why[0]).toBe("symbol");
    expect(response.lanes.db[0]).toMatchObject({
      name: "create_user",
      path: "backend/app/crud.py",
    });
    // Scores are comparable within the response: each lane is best first.
    for (const layer of LAYERS) {
      const scores = response.lanes[layer].map((hit) => hit.score);
      expect(scores).toEqual([...scores].sort((a, b) => b - a));
    }
  });

  it("puts each file in its layer, and other roles in Tests/Config", () => {
    const response = run({ q: "SECRET_KEY", limit: 50 });
    for (const layer of LAYERS) {
      for (const hit of response.lanes[layer]) expect(hit.layer).toBe(layer);
    }
    expect(response.lanes.other.map((hit) => hit.path)).toContain("backend/app/core/config.py");
  });

  it("highlights every match in the snippet, in UTF-16 columns", () => {
    const hit = run({ q: '"SECRET_KEY"' }).lanes.other.find((h) => h.name === "SECRET_KEY");
    expect(hit?.snippet.highlights).toEqual([{ line: 23, start: 4, end: 14 }]);
    expect(hit?.snippet.lines[0]?.slice(4, 14)).toBe("SECRET_KEY");
    // An emoji (two UTF-16 units) before a match moves it by two.
    const readme = linesOf((content.files as Record<string, string>)["README.md"] ?? "");
    const line = readme.findIndex((text) => /\p{Extended_Pictographic}.*\w/u.test(text));
    expect(line).toBeGreaterThanOrEqual(0);
    const text = readme[line] ?? "";
    const word = /[A-Za-z]{4,}/.exec(text.slice(text.search(/\p{Extended_Pictographic}/u)));
    const found = hitsOf(run({ q: `"${word?.[0] ?? ""}"`, limit: 50 })).find(
      (h) => h.path === "README.md" && h.snippet.highlights.some((m) => m.line === line + 1),
    );
    const mark = found?.snippet.highlights.find((m) => m.line === line + 1);
    expect(mark && text.slice(mark.start, mark.end)).toBe(word?.[0]);
  });

  it("reads queries by a few rules", () => {
    expect(readQuery("create_user").interpretation.class).toBe("symbol");
    expect(readQuery("Settings.emails_enabled").interpretation).toMatchObject({
      class: "symbol",
      literals: ["Settings.emails_enabled"],
    });
    expect(readQuery('"Bearer"').interpretation).toMatchObject({
      class: "literal",
      literals: ["Bearer"],
    });
    expect(readQuery("`items_router`").interpretation.class).toBe("literal");
    expect(readQuery("def \\w+_token").interpretation.class).toBe("regex");
    expect(readQuery("^from app").interpretation.class).toBe("regex");
    const concept = readQuery("how are passwords hashed?");
    expect(concept.interpretation).toMatchObject({ class: "conceptual", path_filter: null });
    expect(concept.interpretation.rewrites).toEqual(
      expect.arrayContaining(["passwords_hashed", "password", "hash"]),
    );
    expect(concept.exact).toContainEqual({ kind: "regex", query: "passwords.*hashed" });
    const scoped = readQuery("delete item in backend/app/api/");
    expect(scoped.interpretation.path_filter).toBe("backend/app/api/");
    expect(scoped.exact).toContainEqual({ kind: "path", query: "backend/app/api/" });
    expect(() => readQuery("def (\\w+")).toThrow();
  });

  it("pages one lane with its cursor: every hit once, counts unchanged, other lanes empty", () => {
    const first = run({ q: "user", limit: 5 });
    const total = first.counts.ui;
    expect(total).toBeGreaterThan(10);
    const seen = first.lanes.ui.map((hit) => hit.id);
    let cursor = first.next.ui;
    while (cursor) {
      const page = run({ q: "user", limit: 5, layers: ["ui"], cursor });
      expect(page.counts).toEqual(first.counts);
      for (const layer of LAYERS) {
        if (layer !== "ui") expect(page.lanes[layer]).toEqual([]);
      }
      seen.push(...page.lanes.ui.map((hit) => hit.id));
      cursor = page.next.ui;
    }
    expect(seen).toHaveLength(total);
    expect(new Set(seen).size).toBe(total);
    expect(first.next.svc === null || first.counts.svc > 5).toBe(true);
  });

  it("refuses a cursor from another query, lane or a made-up one", () => {
    const first = run({ q: "user", limit: 5 });
    const cursor = first.next.ui ?? "";
    const invalid = (body: SearchRequest) => {
      try {
        run(body);
      } catch (error) {
        return error instanceof SearchError ? error.code : "other";
      }
      return "none";
    };
    expect(invalid({ q: "users", limit: 5, layers: ["ui"], cursor })).toBe("invalid_cursor");
    expect(invalid({ q: "user", limit: 5, layers: ["api"], cursor })).toBe("invalid_cursor");
    expect(invalid({ q: "user", limit: 5, cursor })).toBe("invalid_cursor");
    expect(invalid({ q: "user", limit: 5, layers: ["ui"], cursor: "not-a-cursor" })).toBe(
      "invalid_cursor",
    );
  });

  it("in partial mode, has keyword and symbol hits only", () => {
    const full = run({ q: "how are passwords hashed", limit: 50 });
    const partial = run({ q: "how are passwords hashed", limit: 50 }, false);
    expect(full.partial).toEqual({ semantic_pending: false, summaries_pending: false });
    expect(partial.partial.semantic_pending).toBe(true);
    expect(hitsOf(full).some((hit) => hit.why.includes("semantic"))).toBe(true);
    expect(hitsOf(partial).some((hit) => hit.why.includes("semantic"))).toBe(false);
    expect(hitsOf(partial).length).toBeGreaterThan(0);
    expect(() => run({ q: "x", mode: "semantic" }, false)).toThrow(
      expect.objectContaining({ status: 409, code: "semantic_not_ready" }) as Error,
    );
  });

  it("keyword mode leaves related words out", () => {
    const keyword = run({ q: "how are passwords hashed", mode: "keyword", limit: 50 });
    expect(hitsOf(keyword).every((hit) => !hit.why.includes("semantic"))).toBe(true);
  });

  it("applies the filters: layers, languages and a path prefix", () => {
    const response = run({ q: "user", layers: ["db"], langs: ["python"], path: "backend/app/" });
    for (const layer of LAYERS) {
      if (layer !== "db") expect(response.counts[layer]).toBe(0);
    }
    expect(response.counts.db).toBeGreaterThan(0);
    for (const hit of response.lanes.db) {
      expect(hit.language).toBe("python");
      expect(hit.path.startsWith("backend/app/")).toBe(true);
    }
  });

  it("checks the body as the API does", () => {
    const code = (body: unknown) => {
      try {
        checkBody(body);
      } catch (error) {
        return error instanceof SearchError ? `${String(error.status)} ${error.code}` : "other";
      }
      return "ok";
    };
    expect(code({ q: "a" })).toBe("ok");
    expect(code({})).toBe("422 validation_failed");
    expect(code({ q: "" })).toBe("422 validation_failed");
    expect(code({ q: "x".repeat(501) })).toBe("422 validation_failed");
    expect(code({ q: "a", limit: 51 })).toBe("422 validation_failed");
    expect(code({ q: "a", layers: ["service"] })).toBe("422 validation_failed");
    expect(code({ q: "a", mode: "fuzzy" })).toBe("422 validation_failed");
    expect(code({ q: "   " })).toBe("422 invalid_query");
  });
});

describe("the mock grep", () => {
  it("reports columns in characters, 1-based, and context lines", () => {
    const result = grep(files, { pattern: "SECRET_KEY", context: 1 }, BROWSE_COMMIT);
    const hit = result.hits.find((h) => h.path === "backend/app/core/config.py" && h.line === 23);
    expect(hit).toMatchObject({ col: 5, ranges: [[5, 15]], offset: 0 });
    expect(hit?.before).toHaveLength(1);
    expect(hit?.after).toHaveLength(1);
    // Hits come in path order.
    const paths = result.hits.map((h) => h.path);
    expect(paths).toEqual([...paths].sort());
  });

  it("counts an emoji as one character", () => {
    const result = grep(files, { pattern: "\\p{Extended_Pictographic}\\s*\\w", regex: true }, "c");
    for (const hit of result.hits) {
      const lines = fileLines.get(hit.path) ?? [];
      const text = lines[hit.line - 1] ?? "";
      const [start, end] = hit.ranges[0] ?? [1, 1];
      const chars = Array.from(text);
      expect(chars.slice(start - 1, end - 1).join("")).toMatch(/^\p{Extended_Pictographic}/u);
    }
  });

  it("filters by globs, stops at the limit and refuses a bad pattern", () => {
    const python = grep(files, { pattern: "import", path_globs: ["*.py"] }, "c");
    expect(python.hits.every((h) => h.path.endsWith(".py"))).toBe(true);
    const scoped = grep(files, { pattern: "def", path_globs: ["backend/app/api/**"] }, "c");
    expect(scoped.hits.every((h) => h.path.startsWith("backend/app/api/"))).toBe(true);
    const limited = grep(files, { pattern: "e", limit: 3 }, "c");
    expect(limited).toMatchObject({ truncated: true });
    expect(limited.hits).toHaveLength(3);
    expect(() => grep(files, { pattern: "(", regex: true }, "c")).toThrow(
      expect.objectContaining({ code: "invalid_pattern" }) as Error,
    );
    expect(globTest("!x")("!x")).toBe(true);
    expect(globTest("src/**")("src/a/b.ts")).toBe(true);
    expect(globTest("*.ts")("src/a/b.tsx")).toBe(false);
  });
});

describe("POST /search", () => {
  it("answers with lanes, counts and the commit in X-Commit-Sha", async () => {
    const { data, response } = await api.POST("/api/v1/orgs/{org}/repos/{repo}/search", {
      ...path,
      body: { q: "create_user" },
    });
    expect(response.headers.get("X-Commit-Sha")).toBe(BROWSE_COMMIT);
    expect(Object.keys(data?.lanes ?? {}).sort()).toEqual([...LAYERS].sort());
    expect(data?.lanes.api[0]?.name).toBe("create_user");
    expect(data?.partial.semantic_pending).toBe(false);
  });

  it("is 404 repo_not_indexed before any index", async () => {
    const repo = db.repos.acme?.[0];
    if (repo) repo.last_indexed_sha = null;
    db.jobs = {};
    const error = await failure(
      api.POST("/api/v1/orgs/{org}/repos/{repo}/search", { ...path, body: { q: "login" } }),
    );
    expect(error).toMatchObject({ status: 404, code: "repo_not_indexed" });
  });

  it("is 409 not_indexed during a first run before its chunks, then partial", async () => {
    const repo = db.repos.acme?.[0];
    if (!repo) throw new Error("no seeded repository");
    repo.last_indexed_sha = null;
    db.jobs = {};
    vi.useFakeTimers({ toFake: ["Date"] });
    const started = Date.now();
    startJob(repo.id);
    // Discovery is done 4.46 s in (the recording at 3x), the chunks stage 0.35 s later and the
    // run 40 ms after that.
    vi.setSystemTime(started + 4600);
    const error = await failure(
      api.POST("/api/v1/orgs/{org}/repos/{repo}/search", { ...path, body: { q: "login" } }),
    );
    expect(error).toMatchObject({ status: 409, code: "not_indexed" });
    vi.setSystemTime(started + 4820);
    const { data } = await api.POST("/api/v1/orgs/{org}/repos/{repo}/search", {
      ...path,
      body: { q: "login" },
    });
    expect(data?.partial.semantic_pending).toBe(true);
  });

  it("refuses a bad body with the standard error", async () => {
    const error = await failure(
      api.POST("/api/v1/orgs/{org}/repos/{repo}/search", { ...path, body: { q: "a", limit: 0 } }),
    );
    expect(error).toMatchObject({ status: 422, code: "validation_failed" });
  });
});

describe("Retry-After", () => {
  it("is read from a 429 into the error", async () => {
    const { http, HttpResponse } = await import("msw");
    server.use(
      http.post("*/api/v1/orgs/:org/repos/:repo/search", () =>
        HttpResponse.json(
          { error: { code: "rate_limited", message: "Slow down.", request_id: "r" } },
          { status: 429, headers: { "Retry-After": "12" } },
        ),
      ),
    );
    const error = await failure(
      api.POST("/api/v1/orgs/{org}/repos/{repo}/search", { ...path, body: { q: "a" } }),
    );
    expect(error).toMatchObject({ status: 429, code: "rate_limited", retryAfter: 12 });
  });
});
