/** The mock `GET …/symbols` (not in the API yet; its contract is in lib/api/symbols.ts), over the
 * snapshot's symbols in mocks/symbols.ts, with the checks, errors and headers of the browse
 * endpoints. */
import { http, HttpResponse } from "msw";

import { SYMBOLS_LIMIT_MAX, type SymbolKind, type SymbolSearch } from "@/lib/api/symbols";
import { searchSymbols, SYMBOL_KINDS } from "@/mocks/symbols";

import { target, validationFailed } from "./browse";
import { apiError } from "./errors";
import { repoOf } from "./repos";

const DEFAULT_LIMIT = 20;

export const symbolHandlers = [
  http.get<{ org: string; repo: string }>(
    "*/api/v1/orgs/:org/repos/:repo/symbols",
    ({ params, request }) => {
      const repo = repoOf(params.org, params.repo);
      if (repo instanceof Response) return repo;
      // Symbols come from an index: none before the first one finishes.
      if (!repo.last_indexed_sha) {
        return apiError(409, "not_indexed", "Symbols appear once this repository is indexed.");
      }
      const found = target(params, request);
      if (found instanceof Response) return found;
      const search = found.url.searchParams;
      const q = search.get("q");
      if (q === null) return validationFailed("q", "Field required");
      if (q.length === 0) return validationFailed("q", "String should have at least 1 character");
      if (q.length > 200) return validationFailed("q", "String should have at most 200 characters");
      const limit = Number(search.get("limit") ?? DEFAULT_LIMIT);
      if (!Number.isInteger(limit) || limit < 1 || limit > SYMBOLS_LIMIT_MAX) {
        return validationFailed("limit", "Input should be between 1 and 50");
      }
      const kind = search.get("kind");
      if (kind !== null && !SYMBOL_KINDS.includes(kind as SymbolKind)) {
        return validationFailed("kind", `Input should be one of: ${SYMBOL_KINDS.join(", ")}`);
      }
      const query = q.trim();
      if (!query) return apiError(422, "invalid_query", "Type something to search for.");
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
