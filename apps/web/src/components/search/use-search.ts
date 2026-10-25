"use client";

import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useQueryStates } from "nuqs";

import { useRepoSlug } from "@/components/repo/use-repo";
import { api } from "@/lib/api/client";
import { searchApi, type Layer, type SearchRequest, type SearchResponse } from "@/lib/api/search";
import type { GrepRequest, GrepResult } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";
import { pathGlobs, requestOf, searchParsers, type SearchState } from "@/lib/search/url-state";

/** Marked when a search is sent and measured when its results are on screen (the budget is
 * 500 ms, the workspace's "data" budget). */
export const SEARCH_SENT_MARK = "search:sent";
export const SEARCH_PAINT_MEASURE = "search:results";

// A commit's results don't change; a minute keeps going back and forth free.
const STALE_MS = 60_000;
const GREP_LIMIT = 100;
const GREP_CONTEXT = 1;

/** The page's state in the URL. Each change is a history entry, so Back and Forward restore the
 * query and filters (typing replaces its own entry; see `SearchInput`). */
export function useSearchState() {
  return useQueryStates(searchParsers, { history: "push", scroll: false });
}

/** A response, with the request it answers: a lane pages on from that, not from the query being
 * typed (the last results stay on screen while the next ones load). */
export type SearchResult = SearchResponse & { commit: string | null; request: SearchRequest };

async function post(
  org: string,
  repo: string,
  body: SearchRequest,
  signal: AbortSignal,
): Promise<SearchResult> {
  const { data, response } = await searchApi.POST("/api/v1/orgs/{org}/repos/{repo}/search", {
    params: { path: { org, repo } },
    body,
    signal,
  });
  if (!data) throw new Error("empty search response");
  return { ...data, commit: response.headers.get("X-Commit-Sha"), request: body };
}

/** The first page of every lane. While a new query runs, the last results stay on screen. */
export function useSearch(state: SearchState, enabled: boolean) {
  const { org, repo } = useRepoSlug();
  const body = requestOf(state);
  return useQuery({
    queryKey: queryKeys.search(org, repo, body),
    queryFn: ({ signal }) => {
      performance.clearMarks(SEARCH_SENT_MARK);
      performance.mark(SEARCH_SENT_MARK);
      return post(org, repo, body, signal);
    },
    enabled: enabled && body.q.length > 0 && state.mode !== "regex",
    staleTime: STALE_MS,
    placeholderData: keepPreviousData,
  });
}

/** One lane's further pages ("More"), each from the cursor of the one before. Nothing is read
 * until it's asked for. */
export function useLanePages(
  body: SearchRequest,
  lane: Layer,
  first: string | null,
  requested: boolean,
) {
  const { org, repo } = useRepoSlug();
  return useInfiniteQuery({
    queryKey: queryKeys.searchLane(org, repo, body, lane, first),
    queryFn: ({ pageParam, signal }) =>
      post(org, repo, { ...body, layers: [lane], cursor: pageParam }, signal),
    initialPageParam: first ?? "",
    getNextPageParam: (page) => page.next[lane] ?? undefined,
    enabled: requested && first !== null,
    staleTime: STALE_MS,
  });
}

/** The query as an exact regular expression over the files (`mode=regex`), with the path
 * prefix as globs. */
export function useGrep(state: SearchState, enabled: boolean) {
  const { org, repo } = useRepoSlug();
  const body: GrepRequest = {
    pattern: state.q.trim(),
    regex: true,
    case: "smart",
    path_globs: pathGlobs(state.path),
    limit: GREP_LIMIT,
    context: GREP_CONTEXT,
    ref: state.ref,
  };
  return useQuery({
    queryKey: queryKeys.grep(org, repo, body),
    queryFn: async ({ signal }): Promise<GrepResult> => {
      performance.clearMarks(SEARCH_SENT_MARK);
      performance.mark(SEARCH_SENT_MARK);
      const { data } = await api.POST("/api/v1/orgs/{org}/repos/{repo}/grep", {
        params: { path: { org, repo } },
        body,
        signal,
      });
      if (!data) throw new Error("empty grep response");
      // The generated path type loses the ranges' pair shape the schema has.
      return data as GrepResult;
    },
    enabled: enabled && body.pattern.length > 0 && state.mode === "regex",
    staleTime: STALE_MS,
    placeholderData: keepPreviousData,
  });
}

/** Measure from the search being sent to the frame that shows its results. */
export function measureResultsPainted(): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const frame = requestAnimationFrame(() => {
    timer = setTimeout(() => {
      if (performance.getEntriesByName(SEARCH_SENT_MARK, "mark").length === 0) return;
      performance.measure(SEARCH_PAINT_MEASURE, SEARCH_SENT_MARK);
      performance.clearMarks(SEARCH_SENT_MARK);
    }, 0);
  });
  return () => {
    cancelAnimationFrame(frame);
    clearTimeout(timer);
  };
}
