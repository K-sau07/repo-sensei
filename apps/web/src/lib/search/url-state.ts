/**
 * The search page's state in its URL: `?q=` (the query), `layer` and `lang` (comma-separated),
 * `path` (a prefix), `ref` (a branch or commit) and `mode` ("keyword" for an exact keyword search,
 * "regex" to run the query as a regular expression over the files). Defaults stay out of the URL.
 */
import type { Route } from "next";
import {
  createSerializer,
  parseAsArrayOf,
  parseAsString,
  parseAsStringLiteral,
  type inferParserType,
} from "nuqs";

import { LAYERS, type SearchRequest } from "@/lib/api/search";
import { repoPath } from "@/lib/routes";

export const EXACT_MODES = ["keyword", "regex"] as const;
export type ExactMode = (typeof EXACT_MODES)[number];

export const searchParsers = {
  q: parseAsString.withDefault(""),
  layer: parseAsArrayOf(parseAsStringLiteral(LAYERS)).withDefault([]),
  lang: parseAsArrayOf(parseAsString).withDefault([]),
  path: parseAsString.withDefault(""),
  ref: parseAsString,
  mode: parseAsStringLiteral(EXACT_MODES),
};

export type SearchState = inferParserType<typeof searchParsers>;

export const EMPTY_SEARCH: SearchState = {
  q: "",
  layer: [],
  lang: [],
  path: "",
  ref: null,
  mode: null,
};

const serialize = createSerializer(searchParsers);

/** The search page with this state (only what differs from the defaults is in the URL). */
export function searchPath(org: string, repo: string, state: Partial<SearchState> = {}): Route {
  return serialize(repoPath(org, repo, "search"), { ...EMPTY_SEARCH, ...state }) as Route;
}

/** The search request for a state (`mode: "regex"` isn't one: it runs as an exact search). */
export function requestOf(state: SearchState): SearchRequest {
  return {
    q: state.q.trim(),
    ...(state.layer.length > 0 ? { layers: [...state.layer] } : {}),
    ...(state.lang.length > 0 ? { langs: [...state.lang] } : {}),
    ...(state.path.trim() ? { path: state.path.trim() } : {}),
    ...(state.ref ? { ref: state.ref } : {}),
    ...(state.mode === "keyword" ? { mode: "keyword" as const } : {}),
  };
}

/** The path prefix as ripgrep globs, so an exact search takes in the paths a search does: every
 * one that starts with it ("src/ap" takes in src/app/ and src/api.ts). It's literal (`[org]`
 * isn't a character class) and anchored at the root (`/`). */
export function pathGlobs(path: string): string[] {
  const prefix = path.trim().replace(/^\/+/, "");
  if (!prefix) return [];
  const literal = prefix.replace(/[\\*?[\]{}]/g, "\\$&");
  // `*` stops at a slash: the first is a file or folder name that goes on, the second inside it.
  return [`/${literal}*`, `/${literal}*/**`];
}

/** The filters set, for the filter button's count. */
export function filterCount(state: SearchState): number {
  return (
    (state.layer.length > 0 ? 1 : 0) +
    (state.lang.length > 0 ? 1 : 0) +
    (state.path.trim() ? 1 : 0) +
    (state.ref ? 1 : 0)
  );
}
