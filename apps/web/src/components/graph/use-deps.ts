"use client";

/** The graph explorer's reads (6.7): a level of the dependency graph, its DSM and cycles, the
 * evidence behind one of its edges, and the third-party packages; each at the viewed ref. */
import { useViewRef } from "@/components/repo/use-ref";
import { useRepoSlug } from "@/components/repo/use-repo";
import { $api } from "@/lib/api/client";
import type { DepLevel, Deps } from "@/lib/api/deps";
import { SEARCH_LIMIT_MAX, searchBody } from "@/lib/api/search";

const STALE_MS = 5 * 60_000; // a commit's levels don't change

export function useDeps(level: DepLevel | null) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/deps",
    { params: { path: { org, repo }, query: { level: level ?? "module", ref } } },
    {
      enabled: level !== null,
      staleTime: STALE_MS,
      // JSON here (the endpoint answers MessagePack too, when asked).
      select: (data: unknown) => data as Deps,
    },
  );
}

/** The groups a matrix is drawn for at most (the API's cap: past it, 422 `dsm_too_large`). */
export const MAX_DSM = 1_500;

export function useDsm(level: DepLevel, enabled = true) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/deps/dsm",
    { params: { path: { org, repo }, query: { level, ref } } },
    { staleTime: STALE_MS, enabled, retry: false },
  );
}

export function useCycles(level: DepLevel) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/deps/cycles",
    { params: { path: { org, repo }, query: { level, ref } } },
    { staleTime: STALE_MS },
  );
}

export function useEdgeEvidence(level: DepLevel, src: string | null, dst: string | null) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/deps/edge-evidence",
    {
      params: {
        path: { org, repo },
        query: { level, src: src ?? "", dst: dst ?? "", limit: 50, ref },
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
