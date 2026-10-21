"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import { addPage, EMPTY_SKY, type Sky } from "@/lib/constellation";
import { queryKeys } from "@/lib/query/keys";

const MAX_PAGES = 200; // per refresh: far more than the largest repository needs
const POLL_MS = 5000; // while it forms, in case an event goes missing

/**
 * A run's constellation (`job`) or the last index's (`job` null), gathered from its pages. Each
 * refresh reads only what is new (from the cursor it has), so the run's `constellation` events
 * (see `useJobEvents`) just invalidate this; a page of another generation starts it over.
 */
export function useConstellation(org: string, repo: string, job: string | null) {
  const queryClient = useQueryClient();
  const key = queryKeys.constellation(org, repo, job);
  return useQuery({
    queryKey: key,
    queryFn: async ({ signal }) => {
      let sky = queryClient.getQueryData<Sky>(key) ?? EMPTY_SKY;
      if (sky.status === "complete" || sky.status === "stopped") return sky; // nothing more comes
      for (let page = 0; page < MAX_PAGES; page += 1) {
        const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/constellation", {
          params: {
            path: { org, repo },
            query: { since: sky.points.length, ...(job ? { job } : {}) },
          },
          signal,
        });
        if (!data) break;
        const next = addPage(sky, data);
        if (next === null) {
          sky = EMPTY_SKY; // discovery started over: read it from the start
          continue;
        }
        sky = next;
        if (data.points.length === 0 || sky.points.length >= sky.available) break;
      }
      return sky;
    },
    staleTime: Infinity,
    // Tens of thousands of points: compare by reference, not deeply.
    structuralSharing: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return job && (status === "pending" || status === "streaming") ? POLL_MS : false;
    },
  });
}
