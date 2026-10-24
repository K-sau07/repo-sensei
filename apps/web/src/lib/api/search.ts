// Hand-typed until the API serves `/search`; then it moves to the generated types (types.ts).
import type { Client } from "openapi-fetch";

import { api } from "./client";
import type { SymbolKind } from "./types";

/** The constellation's layers. The search page labels them UI, API, Service, DB, Tests/Config. */
export type Layer = "ui" | "api" | "svc" | "db" | "other";

export const LAYERS: readonly Layer[] = ["ui", "api", "svc", "db", "other"];

/** How the query was read. */
export type QueryClass =
  "symbol" | "literal" | "regex" | "conceptual" | "flow" | "architecture" | "impact";

export type SearchMode = "auto" | "keyword" | "semantic";

/** Why a hit matched: the legs that found it, strongest first. */
export type MatchReason = "keyword" | "semantic" | "symbol" | "graph";

export interface SearchRequest {
  /** 1 to 500 characters. */
  q: string;
  layers?: Layer[];
  langs?: string[];
  /** A path prefix ("backend/app/"). */
  path?: string | null;
  ref?: string | null;
  mode?: SearchMode;
  /** Per lane, 1 to 50 (8 by default). */
  limit?: number;
  cursor?: string | null;
}

/** A match in the snippet: on `line` (absolute, 1-based), from `start` to `end` (exclusive), in
 * UTF-16 code units. */
export interface SnippetHighlight {
  line: number;
  start: number;
  end: number;
}

export interface Snippet {
  start_line: number;
  /** At most 12. */
  lines: string[];
  highlights: SnippetHighlight[];
}

export interface SearchHit {
  /** Stable within the commit: the symbol key, or `<path>#L<start>-<end>`. */
  id: string;
  /** Null for a region of a file with no symbol (markdown, config). */
  symbol_key: string | null;
  name: string;
  kind: SymbolKind | "file";
  path: string;
  language: string | null;
  layer: Layer;
  /** 1-based, inclusive: the whole symbol (or region). */
  start_line: number;
  end_line: number;
  snippet: Snippet;
  /** 0 to 1, comparable within one response. */
  score: number;
  /** Never empty. */
  why: MatchReason[];
}

export interface Interpretation {
  class: QueryClass;
  /** Additive rewrites that ran besides the original. */
  rewrites: string[];
  /** Literals kept verbatim. */
  literals: string[];
  path_filter: string | null;
}

export type ExactKind = "literal" | "regex" | "symbol" | "path";

export interface ExactQuery {
  kind: ExactKind;
  query: string;
}

export type PerLayer<T> = Record<Layer, T>;

export interface SearchResponse {
  query: string;
  interpretation: Interpretation;
  /** Every lane is always present. */
  lanes: PerLayer<SearchHit[]>;
  /** Hits per lane in total, beyond this page. */
  counts: PerLayer<number>;
  /** A lane's cursor for "more". */
  next: PerLayer<string | null>;
  /** What "Pivot to exact" runs again. */
  exact_queries: ExactQuery[];
  partial: { semantic_pending: boolean; summaries_pending: boolean };
}

/** The endpoint in openapi-typescript's shape, so the typed client (and its middleware) serves it.
 * 200 with the commit in `X-Commit-Sha`; 404 `repo_not_indexed`, 409 `not_indexed` (no chunks
 * yet) or `semantic_not_ready`, 422, and 429 `rate_limited` with `Retry-After`. */
export interface SearchPaths {
  "/api/v1/orgs/{org}/repos/{repo}/search": {
    parameters: { query?: never; header?: never; path?: never; cookie?: never };
    post: {
      parameters: {
        query?: never;
        header?: never;
        path: { org: string; repo: string };
        cookie?: never;
      };
      requestBody: { content: { "application/json": SearchRequest } };
      responses: {
        200: { headers: Record<string, unknown>; content: { "application/json": SearchResponse } };
      };
    };
  };
}

export const SEARCH_QUERY_MAX = 500;
export const SEARCH_LIMIT_DEFAULT = 8;
export const SEARCH_LIMIT_MAX = 50;
export const SNIPPET_LINES_MAX = 12;

/** The app's API client, typed for `/search` as well. */
export const searchApi = api as unknown as Client<SearchPaths>;
