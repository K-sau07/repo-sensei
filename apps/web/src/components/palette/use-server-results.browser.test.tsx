import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "vitest-browser-react";

import { SERVER_DEBOUNCE_MS, useServerResults } from "./use-server-results";

interface Call {
  kind: "files" | "symbols";
  q: string;
  signal: AbortSignal;
  respond: (body: unknown, status?: number) => void;
}

let calls: Call[] = [];

/** Every request waits until the test answers it (or it's aborted). */
function fakeFetch(input: RequestInfo | URL): Promise<Response> {
  const request = input instanceof Request ? input : new Request(input);
  const url = new URL(request.url);
  const kind = url.pathname.endsWith("/symbols") ? "symbols" : "files";
  return new Promise((resolve, reject) => {
    request.signal.addEventListener("abort", () => {
      reject(new DOMException("Aborted", "AbortError"));
    });
    calls.push({
      kind,
      q: url.searchParams.get("q") ?? "",
      signal: request.signal,
      respond: (body, status = 200) => {
        resolve(
          new Response(JSON.stringify(body), {
            status,
            headers: { "Content-Type": "application/json" },
          }),
        );
      },
    });
  });
}

const files = (q: string, paths: string[]) => ({
  commit_sha: "c".repeat(40),
  query: q,
  results: paths.map((path, index) => ({
    path,
    name: path.slice(path.lastIndexOf("/") + 1),
    language: "python",
    role: "api",
    size: 100,
    score: 4 - index,
  })),
});
const symbols = (q: string, names: string[]) => ({
  query: q,
  results: names.map((name, index) => ({
    symbol_key: `py a.py/${name}().`,
    name,
    qualified_name: name,
    kind: "function",
    path: "a.py",
    start_line: index + 1,
    end_line: index + 2,
    signature: null,
    score: 1 - index / 10,
  })),
});

beforeEach(() => {
  calls = [];
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
  // The API client reads process.env (Next and Storybook define it; this test runs bare).
  vi.stubGlobal("process", { env: {} });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function setup(initial = "") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(
    (props?: { query: string }) =>
      useServerResults({ org: "acme", repo: "api", ref: null }, props?.query ?? ""),
    { initialProps: { query: initial }, wrapper },
  );
}

/** Let pending work (the query's scheduling, the client's middleware, responses) reach React. */
async function settle() {
  for (let round = 0; round < 5; round += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  }
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await settle();
}

const open = (kind: Call["kind"]) => calls.filter((call) => call.kind === kind);

describe("useServerResults", () => {
  it("waits for typing to pause before asking", async () => {
    const { rerender } = await setup();
    for (const query of ["l", "lo", "log"]) {
      await rerender({ query });
      await advance(SERVER_DEBOUNCE_MS - 20);
    }
    expect(calls).toEqual([]);
    await advance(20);
    expect(open("files").map((call) => call.q)).toEqual(["log"]);
    expect(open("symbols").map((call) => call.q)).toEqual(["log"]);
  });

  it("aborts the request in flight on the next keystroke, then asks for the new query", async () => {
    const { rerender } = await setup("log");
    await advance(SERVER_DEBOUNCE_MS);
    const [first] = open("files");
    expect(first?.signal.aborted).toBe(false);

    await rerender({ query: "logi" });
    // At once, not when the pause is over.
    await settle();
    expect(first?.signal.aborted).toBe(true);
    expect(open("symbols")[0]?.signal.aborted).toBe(true);

    await advance(SERVER_DEBOUNCE_MS);
    expect(open("files").map((call) => call.q)).toEqual(["log", "logi"]);
  });

  it("asks again for a query that was aborted and typed back", async () => {
    const { rerender } = await setup("log");
    await advance(SERVER_DEBOUNCE_MS);
    await rerender({ query: "logx" });
    await settle();
    await rerender({ query: "log" });
    await advance(SERVER_DEBOUNCE_MS);
    await settle();
    expect(open("files").map((call) => call.q)).toEqual(["log", "log"]);
    expect(open("files")[1]?.signal.aborted).toBe(false);
  });

  it("keeps the last results only while the query extends them, showing what still matches", async () => {
    const { result, rerender } = await setup("log");
    await advance(SERVER_DEBOUNCE_MS);
    open("files")[0]?.respond(files("log", ["app/login.py", "app/logs.py", "app/blog.md"]));
    await settle();
    await vi.waitFor(() => {
      expect(result.current.files.hits).toHaveLength(3);
    });

    await rerender({ query: "logi" }); // extends "log": keep what still matches
    await advance(SERVER_DEBOUNCE_MS);
    expect(result.current.files.hits.map((hit) => hit.path)).toEqual(["app/login.py"]);
    expect(result.current.files.pending).toBe(true);

    await rerender({ query: "lo" }); // doesn't extend it: start empty
    await advance(SERVER_DEBOUNCE_MS);
    expect(result.current.files.hits).toEqual([]);
  });

  it("searches files from one character, symbols from two", async () => {
    const { rerender } = await setup("l");
    await advance(SERVER_DEBOUNCE_MS);
    expect(open("files")).toHaveLength(1);
    expect(open("symbols")).toHaveLength(0);
    await rerender({ query: "lo" });
    await advance(SERVER_DEBOUNCE_MS);
    expect(open("symbols")).toHaveLength(1);
  });

  it("one failing search doesn't stop the other", async () => {
    const { result } = await setup("login");
    await advance(SERVER_DEBOUNCE_MS);
    open("symbols")[0]?.respond(
      { error: { code: "not_indexed", message: "Not yet.", request_id: "r" } },
      409,
    );
    open("files")[0]?.respond(files("login", ["app/login.py"]));
    await settle();
    await vi.waitFor(() => {
      expect(result.current.files.hits).toHaveLength(1);
      expect(result.current.symbols.error).toMatchObject({ status: 409, code: "not_indexed" });
    });
    expect(result.current.symbols.hits).toEqual([]);
  });

  it("and the other way round", async () => {
    const { result } = await setup("login");
    await advance(SERVER_DEBOUNCE_MS);
    open("files")[0]?.respond(
      { error: { code: "internal_error", message: "Boom.", request_id: "r" } },
      500,
    );
    open("symbols")[0]?.respond(symbols("login", ["login_access_token"]));
    await settle();
    await vi.waitFor(() => {
      expect(result.current.symbols.hits.map((hit) => hit.name)).toEqual(["login_access_token"]);
      expect(result.current.files.error).toMatchObject({ status: 500 });
    });
  });
});
