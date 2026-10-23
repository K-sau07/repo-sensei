/** The mock `GET …/symbols`, over the snapshot's symbols in mocks/symbols.ts, with the API's
 * checks in its order: the query's fields, then the snapshot (404 before any index), then
 * whether its symbols are extracted (409 `not_indexed` during the first run). */
import { http, HttpResponse } from "msw";

import type { SymbolKind, SymbolSearch } from "@/lib/api/types";
import { searchSymbols, SYMBOL_KINDS } from "@/mocks/symbols";

import { target, validationFailed } from "./browse";
import { apiError } from "./errors";
import { repoOf } from "./repos";

const DEFAULT_LIMIT = 20;
const LIMIT_MAX = 50;

export const symbolHandlers = [
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
        return apiError(
          409,
          "not_indexed",
          "Symbols appear once this commit's indexing has extracted them.",
        );
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
