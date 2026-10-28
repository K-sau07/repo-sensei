/** The mock `GET …/symbols`, over the snapshot's symbols in mocks/symbols.ts, with the API's
 * checks in its order: the query's fields, then the snapshot (404 before any index), then
 * whether its symbols are extracted (409 `not_indexed` during the first run). `…/symbols/at` and
 * `…/symbols/outline` answer from the mock graph's definitions (mocks/graph.ts). */
import { http, HttpResponse } from "msw";

import type { SymbolKind, SymbolOutline, SymbolSearch, SymbolsAt } from "@/lib/api/types";
import { fileAt } from "@/mocks/browse";
import { mockGraph, outline, symbolsAt } from "@/mocks/graph";
import { searchSymbols, SYMBOL_KINDS } from "@/mocks/symbols";

import { checkPath, target, validationFailed } from "./browse";
import { apiError } from "./errors";
import { repoOf } from "./repos";

const DEFAULT_LIMIT = 20;
const LIMIT_MAX = 50;

interface Params {
  org: string;
  repo: string;
}

const NOT_INDEXED = "Symbols appear once this commit's indexing has extracted them.";

/** A file's symbols request: the snapshot, the path checked, the symbols extracted, the file
 * there; or the error response, in the API's order. */
function fileTarget(params: Params, request: Request) {
  const raw = new URL(request.url).searchParams.get("path");
  if (raw === null) return validationFailed("path", "Field required");
  if (raw.length === 0) return validationFailed("path", "String should have at least 1 character");
  const path = checkPath(raw);
  if (path instanceof Response) return path;
  const found = target(params, request);
  if (found instanceof Response) return found;
  if (!found.repo.last_indexed_sha) return apiError(409, "not_indexed", NOT_INDEXED);
  if (!path || !fileAt(path)) {
    return apiError(404, "path_not_found", "No such file in this snapshot.");
  }
  return { ...found, path };
}

const headersFor = (commit: string) => ({
  "Cache-Control": "private, no-cache",
  "X-Commit-Sha": commit,
});

export const symbolHandlers = [
  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/symbols/at", async ({ params, request }) => {
    const raw = new URL(request.url).searchParams.get("line");
    const line = Number(raw);
    if (raw === null) return validationFailed("line", "Field required");
    if (!Number.isInteger(line) || line < 1) {
      return validationFailed("line", "Input should be greater than or equal to 1");
    }
    const found = fileTarget(params, request);
    if (found instanceof Response) return found;
    const symbols = symbolsAt(await mockGraph(), found.path, line);
    return HttpResponse.json<SymbolsAt>(
      { path: found.path, line, symbols },
      { headers: headersFor(found.commit) },
    );
  }),
  http.get<Params>(
    "*/api/v1/orgs/:org/repos/:repo/symbols/outline",
    async ({ params, request }) => {
      const found = fileTarget(params, request);
      if (found instanceof Response) return found;
      return HttpResponse.json<SymbolOutline>(
        { path: found.path, symbols: outline(await mockGraph(), found.path), truncated: false },
        { headers: headersFor(found.commit) },
      );
    },
  ),
  http.get<{ org: string; repo: string }>(
    "*/api/v1/orgs/:org/repos/:repo/symbols",
    ({ params, request }) => {
      const repo = repoOf(params.org, params.repo);
      if (repo instanceof Response) return repo;
      const search = new URL(request.url).searchParams;
      const q = search.get("q");
      if (q === null) return validationFailed("q", "Field required");
      if (q.length === 0) return validationFailed("q", "String should have at least 1 character");
      if (q.length > 200) return validationFailed("q", "String should have at most 200 characters");
      const limit = Number(search.get("limit") ?? DEFAULT_LIMIT);
      if (!Number.isInteger(limit) || limit < 1 || limit > LIMIT_MAX) {
        return validationFailed("limit", "Input should be between 1 and 50");
      }
      const kind = search.get("kind");
      if (kind !== null && !SYMBOL_KINDS.includes(kind as SymbolKind)) {
        return validationFailed("kind", `Input should be one of: ${SYMBOL_KINDS.join(", ")}`);
      }
      const query = q.trim();
      if (!query) return apiError(422, "invalid_query", "Type something to search for.");
      const found = target(params, request);
      if (found instanceof Response) return found;
      // The first run's snapshot is browsable before its symbols stage has run.
      if (!found.repo.last_indexed_sha) {
        return apiError(409, "not_indexed", NOT_INDEXED);
      }
      return HttpResponse.json<SymbolSearch>(
        {
          query,
          results: searchSymbols(query, { kind: kind as SymbolKind | null, limit }),
        },
        { headers: { "Cache-Control": "private, no-cache", "X-Commit-Sha": found.commit } },
      );
    },
  ),
];
