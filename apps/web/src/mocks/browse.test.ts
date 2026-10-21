import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";

import { BROWSE_COMMIT, BROWSE_EXTRAS_KEY, wordSimilarity } from "./browse";
import { resetMockDb } from "./db";
import tree from "./fixtures/browse-tree.json";
import { server } from "./node";

const api = createApiClient({ baseUrl: "http://api.test", getCsrfToken: () => null });
const repo = { org: "acme", repo: "api" };

beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" });
});
afterEach(() => {
  server.resetHandlers();
  resetMockDb();
  vi.unstubAllGlobals();
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

const listing = (path: string, ref?: string) =>
  api.GET("/api/v1/orgs/{org}/repos/{repo}/tree", {
    params: { path: repo, query: { path, ...(ref ? { ref } : {}) } },
  });
const file = (path: string) =>
  api.GET("/api/v1/orgs/{org}/repos/{repo}/file", { params: { path: repo, query: { path } } });

describe("the mock snapshot", () => {
  it("is the indexed run's tree: 245 files, 18,567 lines, 54 directories", () => {
    expect(tree.files).toHaveLength(245);
    expect(tree.files.reduce((sum, f) => sum + (f.loc ?? 0), 0)).toBe(18_567);
    expect(tree.dirs).toHaveLength(54);
    // The Merkle root the recorded run reported (fixtures/index-run.json, merkle details).
    expect(tree.dirs.find((d) => d.path === "")?.hash).toBe(
      "c787a34e22985f6f0ae9660e043d2be2cf3a7aed46fca3348b4da177bae97957",
    );
  });
});

describe("GET /tree", () => {
  it("lists a directory's children, directories first, with the commit and ETag headers", async () => {
    const { data, response } = await listing("backend/app");
    expect(data?.path).toBe("backend/app");
    expect(data?.entries.map((e) => e.name)).toEqual([
      "alembic",
      "api",
      "core",
      "email-templates",
      "__init__.py",
      "crud.py",
      "initial_data.py",
      "main.py",
      "models.py",
      "utils.py",
    ]);
    expect(data?.entries[0]).toMatchObject({ type: "dir", child_count: 4, language: null });
    expect(data?.entries.find((e) => e.name === "main.py")).toMatchObject({
      type: "file",
      language: "python",
      role: "api",
      size: 958,
      loc: 36,
      child_count: null,
    });
    expect(response.headers.get("X-Commit-Sha")).toBe(BROWSE_COMMIT);
    expect(response.headers.get("ETag")).toMatch(/^"t1-[0-9a-f]{64}"$/);
    expect(response.headers.get("Cache-Control")).toBe("private, no-cache");
  });

  it("refuses unsafe paths, files and missing directories as the API does", async () => {
    for (const bad of ["../etc", "/abs", "a//b", "a/./b", "a\\b"]) {
      expect((await failure(listing(bad))).code).toBe("invalid_path");
    }
    expect((await failure(listing("backend/app/main.py"))).code).toBe("not_a_directory");
    expect((await failure(listing("nope"))).status).toBe(404);
  });

  it("reads the indexed commit by prefix or branch, and nothing else", async () => {
    expect((await listing("", "b1125d8")).data?.entries.length).toBeGreaterThan(0);
    expect((await listing("", "main")).data?.entries.length).toBeGreaterThan(0);
    expect((await failure(listing("", "feature/x"))).code).toBe("ref_not_indexed");
    expect((await failure(listing("", "  "))).code).toBe("invalid_ref");
  });
});

describe("GET /file", () => {
  it("serves text with its metadata and content hash", async () => {
    const { data, response } = await file("backend/app/main.py");
    expect(data).toMatchObject({ path: "backend/app/main.py", loc: 36, encoding: "utf-8" });
    expect(data?.content.split("\n")).toHaveLength(37); // 36 lines and the final newline
    expect(data?.content).toContain("FRONTEND_DIR");
    expect(response.headers.get("ETag")).toMatch(/^"f1-[0-9a-f]{64}"$/);
    expect(response.headers.get("X-Commit-Sha")).toBe(BROWSE_COMMIT);
  });

  it("says when a file's content isn't in the fixture", async () => {
    const { data } = await file("frontend/src/routes/signup.tsx");
    expect(data?.loc).toBe(187);
    expect(data?.content).toContain("has this file's metadata but not its content");
  });

  it("answers 415 with metadata for binary files and 413 with a link for large ones", async () => {
    const binary = await failure(file("img/login.png"));
    expect(binary).toMatchObject({ status: 415, code: "binary_file" });
    expect(binary.details).toMatchObject({
      path: "img/login.png",
      flags: ["binary"],
      size: 51_928,
    });

    vi.stubGlobal("localStorage", {
      getItem: (key: string) => (key === BROWSE_EXTRAS_KEY ? "extras" : null),
      setItem: () => undefined,
      removeItem: () => undefined,
    });
    const large = await failure(file("mock-extras/server.log"));
    expect(large).toMatchObject({ status: 413, code: "file_too_large" });
    expect(large.details).toMatchObject({ size: 3 * 1024 * 1024 });
    expect(large.details).toHaveProperty("download_url");
    const long = await file("mock-extras/ten_thousand_lines.py");
    expect(long.data?.content.split("\n")).toHaveLength(10_001);
  });

  it("needs a file's path", async () => {
    expect((await failure(file(""))).code).toBe("invalid_path");
    expect((await failure(file("backend"))).code).toBe("path_not_found");
  });
});

describe("GET /files/search", () => {
  const search = (q: string, limit?: number) =>
    api.GET("/api/v1/orgs/{org}/repos/{repo}/files/search", {
      params: { path: repo, query: { q, ...(limit ? { limit } : {}) } },
    });

  it("ranks file names that match first", async () => {
    const { data } = await search("main.py");
    expect(data?.commit_sha).toBe(BROWSE_COMMIT);
    expect(data?.results.slice(0, 2).map((hit) => hit.path)).toEqual([
      "backend/app/main.py",
      "backend/app/api/main.py",
    ]);
  });

  it("matches characters in order, like the API's ILIKE pattern", async () => {
    const { data } = await search("rtusr");
    expect(data?.results.map((hit) => hit.path)).toContain("backend/app/api/routes/users.py");
  });

  it("limits results and refuses an empty query", async () => {
    expect((await search("e", 5)).data?.results).toHaveLength(5);
    expect((await failure(search("   "))).code).toBe("invalid_query");
  });

  it("computes pg_trgm's word similarity", () => {
    // pg_trgm's documented example: word_similarity('word', 'two words') = 0.8.
    expect(wordSimilarity("word", "two words")).toBeCloseTo(0.8, 5);
    expect(wordSimilarity("main", "main")).toBe(1);
    expect(wordSimilarity("xyz", "main")).toBe(0);
  });
});
