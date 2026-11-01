"use client";

/** The graph explorer's reads (6.7): a level of the dependency graph, its DSM and cycles, the
 * evidence behind one of its edges, and the third-party packages; each at the viewed ref.
 *
 * The API counts only edges at least half sure unless asked: a call matched by its name alone is
 * a guess, and a cycle made of guesses is coupling the code may not have. With `?guesses=true`
 * they are asked for too (`min_conf=0`). */
import { parseAsBoolean, useQueryState } from "nuqs";

import { useViewRef } from "@/components/repo/use-ref";
import { useRepoSlug } from "@/components/repo/use-repo";
import { $api } from "@/lib/api/client";
import type { DepLevel, Deps } from "@/lib/api/deps";
import { SEARCH_LIMIT_MAX, searchBody } from "@/lib/api/search";

const STALE_MS = 5 * 60_000; // a commit's levels don't change

/** `min_conf` when guesses are counted too; else the API's own (0.5). */
function useMinConf(): { min_conf?: number } {
  const [guesses] = useQueryState("guesses", parseAsBoolean.withDefault(false));
  return guesses ? { min_conf: 0 } : {};
}

export function useDeps(level: DepLevel | null) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  const conf = useMinConf();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/deps",
    { params: { path: { org, repo }, query: { level: level ?? "module", ref, ...conf } } },
    {
      enabled: level !== null,
      staleTime: STALE_MS,
      // JSON here (the endpoint answers MessagePack too, when asked).
      select: (data: unknown) => data as Deps,
    },
  );
}

/** The groups a matrix is drawn for at most (the API's cap: past it, 422 `dsm_too_large`). */
export const MAX_DSM = 2_500;

export function useDsm(level: DepLevel, enabled = true) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  const conf = useMinConf();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/deps/dsm",
    { params: { path: { org, repo }, query: { level, ref, ...conf } } },
    { staleTime: STALE_MS, enabled, retry: false },
  );
}

export function useCycles(level: DepLevel) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  const conf = useMinConf();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/deps/cycles",
    { params: { path: { org, repo }, query: { level, ref, ...conf } } },
    { staleTime: STALE_MS },
  );
}

export function useEdgeEvidence(level: DepLevel, src: string | null, dst: string | null) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  const conf = useMinConf();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/deps/edge-evidence",
    {
      params: {
        path: { org, repo },
        query: { level, src: src ?? "", dst: dst ?? "", limit: 50, ref, ...conf },
      },
    },
    { enabled: src !== null && dst !== null, staleTime: STALE_MS },
  );
}

export function usePackages(enabled = true) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/deps/packages",
    { params: { path: { org, repo }, query: { ref } } },
    { enabled, staleTime: STALE_MS },
  );
}

/** Hits for a phrase ("where the session is checked"), to light the nodes they're in. */
export function useGraphSearch(q: string, enabled: boolean) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  return $api.useQuery(
    "post",
    "/api/v1/orgs/{org}/repos/{repo}/search",
    { params: { path: { org, repo } }, body: searchBody({ q, limit: SEARCH_LIMIT_MAX, ref }) },
    { enabled: enabled && q.trim().length > 0, staleTime: STALE_MS },
  );
}
