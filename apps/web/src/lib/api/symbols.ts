// Hand-typed until the API serves `/symbols`; then it moves to the generated types (types.ts).
import type { Client } from "openapi-fetch";

import { api } from "./client";

export type SymbolKind =
  "module" | "class" | "function" | "method" | "component" | "hook" | "variable";

export interface SymbolHit {
  /** Stable across line changes: `<lang> <path>/<Container>#<name>().` */
  symbol_key: string;
  name: string;
  qualified_name: string;
  kind: SymbolKind;
  path: string;
  /** 1-based, inclusive. */
  start_line: number;
  end_line: number;
  signature: string | null;
  /** 0–1. */
  score: number;
}

export interface SymbolSearch {
  query: string;
  results: SymbolHit[];
}

/** The endpoint in openapi-typescript's shape, so the typed client (and its middleware) serves it.
 * 200 with the commit in `X-Commit-Sha`; 409 `not_indexed` before the repository has symbols. */
export interface SymbolPaths {
  "/api/v1/orgs/{org}/repos/{repo}/symbols": {
    parameters: { query?: never; header?: never; path?: never; cookie?: never };
    get: {
      parameters: {
        query: { q: string; kind?: SymbolKind | null; ref?: string | null; limit?: number };
        header?: never;
        path: { org: string; repo: string };
        cookie?: never;
      };
      requestBody?: never;
      responses: {
        200: { headers: Record<string, unknown>; content: { "application/json": SymbolSearch } };
      };
    };
  };
}

export const SYMBOLS_LIMIT_MAX = 50;

/** The app's API client, typed for `/symbols` as well. */
export const symbolsApi = api as unknown as Client<SymbolPaths>;
