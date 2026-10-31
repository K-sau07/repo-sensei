"use client";

/**
 * Whether what an answer cited is still as it was (5.6): its citations checked against the index
 * the page is read at (`GET …/staleness`). Read again when a new index lands (the org stream's
 * `repo_updated` invalidates every check).
 */
import { useQuery } from "@tanstack/react-query";

import { useViewRef } from "@/components/repo/use-ref";
import { useRepoSlug } from "@/components/repo/use-repo";
import { api } from "@/lib/api/client";
import { citedKeys, reasons, type StaleReason } from "@/lib/api/refs";
import { queryKeys } from "@/lib/query/keys";

const STALE_MS = 60_000;

export function useStaleness(
  citations: readonly { sha: string; path: string }[],
  enabled = true,
): { checked: ReadonlyMap<string, StaleReason>; head: string | null } {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  const cited = citedKeys(citations);
  const query = useQuery({
    queryKey: queryKeys.staleness(org, repo, ref, cited),
    queryFn: async ({ signal }) => {
      const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/staleness", {
        params: { path: { org, repo }, query: { citations: cited, ...(ref ? { ref } : {}) } },
        signal,
      });
      if (!data) throw new Error("no staleness");
      return data;
    },
    enabled: enabled && cited.length > 0,
    staleTime: STALE_MS,
    retry: false, // a badge that can't be worked out is left off, not retried
  });
  return { checked: reasons(query.data), head: query.data?.sha ?? null };
}
