// The search endpoint's types, from the generated OpenAPI schema (regenerate with `make openapi`).
import { api } from "./client";
import type { Schemas } from "./types";

/** The constellation's layers. The search page labels them UI, API, Service, DB, Tests/Config. */
export type Layer = Schemas["Lane"];

export const LAYERS: readonly Layer[] = ["ui", "api", "svc", "db", "other"];

export type Interpretation = Schemas["SearchInterpretation"];

/** How the query was read. */
export type QueryClass = Interpretation["class"];

export type SearchMode = Schemas["SearchMode"];

export type SearchHit = Schemas["SearchResultHit"];

/** Why a hit matched: the legs that found it, strongest first. */
export type MatchReason = SearchHit["why"][number];

type Request = Schemas["SearchRequest"];

/** A search as the page asks for it: `mode` and `limit` left out mean the API's defaults
 * (`auto`, {@link SEARCH_LIMIT_DEFAULT}), so URLs and cache keys stay short. */
export type SearchRequest = Omit<Request, "mode" | "limit"> &
  Partial<Pick<Request, "mode" | "limit">>;

/** A match in the snippet: on `line` (absolute, 1-based), from `start` to `end` (exclusive), in
 * UTF-16 code units. */
export type SnippetHighlight = Schemas["SearchHighlight"];

export type Snippet = Schemas["SearchSnippet"];

export type ExactQuery = Schemas["SearchExactQuery"];

export type ExactKind = ExactQuery["kind"];

export type PerLayer<T> = Record<Layer, T>;

/** 200 with the commit in `X-Commit-Sha`; 404 `repo_not_indexed`, 409 `not_indexed` (no chunks
 * yet) or `semantic_not_ready`, 422, and 429 `rate_limited` with `Retry-After`. */
export type SearchResponse = Schemas["SearchResponse"];

export const SEARCH_QUERY_MAX = 500;
export const SEARCH_LIMIT_DEFAULT = 8;
export const SEARCH_LIMIT_MAX = 50;
export const SNIPPET_LINES_MAX = 12;

/** The body sent: the page's request with the API's defaults filled in. */
export function searchBody(request: SearchRequest): Request {
  return { mode: "auto", limit: SEARCH_LIMIT_DEFAULT, ...request };
}

/** The app's API client (the search endpoint is in its generated paths). */
export const searchApi = api;
