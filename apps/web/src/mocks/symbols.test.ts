import type { Client } from "openapi-fetch";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { SymbolPaths } from "@/lib/api/symbols";

import { BROWSE_COMMIT, fileAt } from "./browse";
import { db, resetMockDb } from "./db";
import content from "./fixtures/browse-content.json";
import { server } from "./node";
import { searchSymbols, SYMBOLS } from "./symbols";

const api = createApiClient({
  baseUrl: "http://api.test",
  getCsrfToken: () => null,
}) as unknown as Client<SymbolPaths>;
const params = (query: Record<string, string | number>) => ({
  params: { path: { org: "acme", repo: "api" }, query: { q: "", ...query } },
});

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

describe("the mock symbols", () => {
  it("are definitions at their real lines in files the mock serves", () => {
    expect(SYMBOLS.length).toBeGreaterThan(100);
    for (const symbol of SYMBOLS) {
      expect(fileAt(symbol.path), symbol.path).toBeDefined();
      const text = files[symbol.path];
      expect(text, symbol.path).toBeDefined();
      const lines = (text ?? "").split("\n");
      expect(symbol.start_line).toBeGreaterThanOrEqual(1);
      expect(symbol.end_line).toBeGreaterThanOrEqual(symbol.start_line);
      expect(symbol.end_line).toBeLessThanOrEqual(lines.length);
      // The name is defined at the start (after any decorators).
      const head = lines.slice(symbol.start_line - 1, symbol.start_line + 5).join("\n");
      expect(head, symbol.symbol_key).toMatch(
        new RegExp(`(def|class|function|const)\\s+${symbol.name}\\b|^${symbol.name}\\b`, "m"),
      );
    }
  });

  it("have unique keys in the endpoint's scheme", () => {
    const keys = SYMBOLS.map((symbol) => symbol.symbol_key);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toContain("python backend/app/api/routes/login.py/login_access_token().");
    expect(keys).toContain("python backend/app/core/config.py/Settings#");
    expect(keys).toContain("python backend/app/core/config.py/Settings#emails_enabled().");
  });

  it("include login_access_token at lines 23–42 (its decorator to its last line)", () => {
    const symbol = SYMBOLS.find((s) => s.name === "login_access_token");
    expect(symbol).toMatchObject({ kind: "function", start_line: 23, end_line: 42 });
    const lines = (files["backend/app/api/routes/login.py"] ?? "").split("\n");
    expect(lines[22]).toBe('@router.post("/login/access-token")');
    expect(lines[23]).toBe("def login_access_token(");
  });
});

describe("searchSymbols", () => {
  it("ranks an exact name, then prefixes, then substrings", () => {
    const hits = searchSymbols("login", { kind: null, limit: 20 });
    expect(hits[0]?.name).toBe("Login");
    expect(hits[0]?.score).toBe(1);
    expect(hits.map((hit) => hit.name)).toContain("login_access_token");
    const scores = hits.map((hit) => hit.score);
    expect(scores).toEqual([...scores].sort((a, b) => b - a));
  });

  it("filters by kind and stops at the limit", () => {
    const hooks = searchSymbols("use", { kind: "hook", limit: 20 });
    expect(hooks.map((hit) => hit.name).sort()).toEqual(["useAuth", "useSidebar"]);
    expect(searchSymbols("e", { kind: null, limit: 3 })).toHaveLength(3);
  });
});

describe("GET /symbols", () => {
  it("answers with ranked symbols and the commit in X-Commit-Sha", async () => {
    const { data, response } = await api.GET(
      "/api/v1/orgs/{org}/repos/{repo}/symbols",
      params({ q: "login_access" }),
    );
    expect(response.headers.get("X-Commit-Sha")).toBe(BROWSE_COMMIT);
    expect(data?.query).toBe("login_access");
    expect(data?.results[0]).toEqual({
      symbol_key: "python backend/app/api/routes/login.py/login_access_token().",
      name: "login_access_token",
      qualified_name: "login_access_token",
      kind: "function",
      path: "backend/app/api/routes/login.py",
      start_line: 23,
      end_line: 42,
      signature:
        "def login_access_token(session: SessionDep, form_data: Annotated[OAuth2PasswordRequestForm, Depends()]) -> Token",
      score: expect.any(Number) as number,
    });
  });

  it("defaults to 20 results, at most 50", async () => {
    const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/symbols", params({ q: "e" }));
    expect(data?.results).toHaveLength(20);
    const error = await failure(
      api.GET("/api/v1/orgs/{org}/repos/{repo}/symbols", params({ q: "e", limit: 51 })),
    );
    expect(error).toMatchObject({ status: 422, code: "validation_failed" });
  });

  it("refuses an unknown kind and an empty query", async () => {
    expect(
      await failure(
        api.GET("/api/v1/orgs/{org}/repos/{repo}/symbols", params({ q: "a", kind: "table" })),
      ),
    ).toMatchObject({ status: 422, code: "validation_failed" });
    expect(
      await failure(api.GET("/api/v1/orgs/{org}/repos/{repo}/symbols", params({ q: "  " }))),
    ).toMatchObject({ status: 422, code: "invalid_query" });
  });

  it("is 409 not_indexed before the repository's first index", async () => {
    const repo = db.repos.acme?.[0];
    if (repo) repo.last_indexed_sha = null;
    const error = await failure(
      api.GET("/api/v1/orgs/{org}/repos/{repo}/symbols", params({ q: "login" })),
    );
    expect(error).toMatchObject({ status: 409, code: "not_indexed" });
  });

  it("is 404 for a repository that isn't there", async () => {
    const error = await failure(
      api.GET("/api/v1/orgs/{org}/repos/{repo}/symbols", {
        params: { path: { org: "acme", repo: "nope" }, query: { q: "login" } },
      }),
    );
    expect(error.status).toBe(404);
  });
});
