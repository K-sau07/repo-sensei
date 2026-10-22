import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/errors";
import { queryKeys } from "@/lib/query/keys";

import { isFilePath, renderedCommit } from "./rendered";

const INDEXED = "b".repeat(40);
const BRANCH_TIP = "c".repeat(40);
const OLD = "a".repeat(40);

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

const shown = (path: string, ref: string | null = null) => ({
  org: "acme",
  repo: "api",
  path,
  ref,
});

describe("renderedCommit", () => {
  it("is the commit the file was served from at that ref, not the last index", () => {
    const cache = client();
    const path = "backend/app/main.py";
    // ?ref=main: the API answered with the branch's newest snapshot.
    cache.setQueryData(queryKeys.codeFile("acme", "api", "main", INDEXED, path), {
      commit: BRANCH_TIP,
    });
    // The same file read without a ref, earlier.
    cache.setQueryData(queryKeys.codeFile("acme", "api", null, INDEXED, path), { commit: INDEXED });
    expect(renderedCommit(cache, shown(path, "main"))).toBe(BRANCH_TIP);
    // A prefix resolves to whatever commit it names.
    cache.setQueryData(queryKeys.codeFile("acme", "api", "aaaaaaa", INDEXED, path), {
      commit: OLD,
    });
    expect(renderedCommit(cache, shown(path, "aaaaaaa"))).toBe(OLD);
  });

  it("reads a directory's listing too, and a file shown as an error that names its commit", () => {
    const cache = client();
    cache.setQueryData(queryKeys.codeTree("acme", "api", null, INDEXED, "backend"), {
      commit: INDEXED,
    });
    expect(renderedCommit(cache, shown("backend"))).toBe(INDEXED);

    const key = queryKeys.codeFile("acme", "api", null, INDEXED, "img/login.png");
    const query = cache.getQueryCache().build(cache, { queryKey: key });
    query.setState({
      status: "error",
      error: new ApiError({
        status: 415,
        code: "binary_file",
        message: "Not text.",
        details: { commit_sha: BRANCH_TIP },
      }),
      errorUpdatedAt: Date.now(),
    });
    expect(renderedCommit(cache, shown("img/login.png"))).toBe(BRANCH_TIP);
  });

  it("is the newest read when several versions are cached, and null when nothing was shown", () => {
    const cache = client();
    const path = "README.md";
    cache.setQueryData(
      queryKeys.codeFile("acme", "api", null, OLD, path),
      { commit: OLD },
      {
        updatedAt: 1,
      },
    );
    cache.setQueryData(
      queryKeys.codeFile("acme", "api", null, INDEXED, path),
      { commit: INDEXED },
      {
        updatedAt: 2,
      },
    );
    expect(renderedCommit(cache, shown(path))).toBe(INDEXED);
    expect(renderedCommit(cache, shown("other.md"))).toBeNull();
    expect(renderedCommit(cache, { ...shown(path), repo: "web" })).toBeNull();
  });

  it("with no ref, counts only reads of what no ref means now (not an earlier ?ref=main)", () => {
    const cache = client();
    const path = "backend/app/main.py";
    cache.setQueryData(queryKeys.codeFile("acme", "api", "main", INDEXED, path), {
      commit: BRANCH_TIP,
    });
    expect(renderedCommit(cache, { ...shown(path), version: INDEXED })).toBeNull();
    cache.setQueryData(queryKeys.codeFile("acme", "api", null, INDEXED, path), { commit: INDEXED });
    expect(renderedCommit(cache, { ...shown(path), version: INDEXED })).toBe(INDEXED);
  });
});

describe("isFilePath", () => {
  it("knows from what the code browser read, and guesses from the name before that", () => {
    const cache = client();
    cache.setQueryData(queryKeys.codeFile("acme", "api", null, INDEXED, "Makefile.d"), {
      commit: INDEXED,
    });
    cache.setQueryData(queryKeys.codeTree("acme", "api", null, INDEXED, "lib.v2"), {
      commit: INDEXED,
    });
    expect(isFilePath(cache, shown("Makefile.d"))).toBe(true);
    expect(isFilePath(cache, shown("lib.v2"))).toBe(false); // a directory with a dot
    expect(isFilePath(cache, shown("backend/app/main.py"))).toBe(true);
    expect(isFilePath(cache, shown("backend/app"))).toBe(false);
    expect(isFilePath(cache, shown(""))).toBe(false);
  });
});
