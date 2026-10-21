"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { ConstellationPage } from "@/lib/api/types";
import { addPage, EMPTY_SKY, type Sky } from "@/lib/constellation";
import { queryKeys } from "@/lib/query/keys";

const MAX_PAGES = 200; // per refresh: far more than the largest repository needs
const POLL_MS = 5000; // while it forms, in case an event goes missing

/**
 * Read pages onto `sky` from its cursor until there is nothing more (or `MAX_PAGES`), handing
 * each new sky to `shown` as it arrives. A page of another generation (discovery started over)
 * starts it over from the beginning.
 */
export async function readPages(
  start: Sky,
  fetchPage: (since: number) => Promise<ConstellationPage | undefined>,
  shown: (sky: Sky) => void,
): Promise<Sky> {
  let sky = start;
  if (sky.status === "complete" || sky.status === "stopped") return sky; // nothing more comes
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const data = await fetchPage(sky.points.length);
    if (!data) break;
    const next = addPage(sky, data);
    if (next === null) {
      sky = EMPTY_SKY; // discovery started over: read it from the start
      continue;
    }
    sky = next;
    shown(sky); // each page shows as it arrives: a big sky fills in instead of appearing at the end
    if (data.points.length === 0 || sky.points.length >= sky.available) break;
  }
  return sky;
}

/**
 * A run's constellation (`job`) or the last index's (`job` null), gathered from its pages. Each
 * refresh reads only what is new (from the cursor it has) and shows each page as it comes, so
 * the run's `constellation` events (see `useJobEvents`) just invalidate this, without cancelling
 * a read in progress.
 */
export function useConstellation(org: string, repo: string, job: string | null) {
  const queryClient = useQueryClient();
  const key = queryKeys.constellation(org, repo, job);
  return useQuery({
    queryKey: key,
    queryFn: ({ signal }) =>
      readPages(
        queryClient.getQueryData<Sky>(key) ?? EMPTY_SKY,
        async (since) => {
          const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/constellation", {
            params: { path: { org, repo }, query: { since, ...(job ? { job } : {}) } },
            signal,
          });
          return data;
        },
        (sky) => {
          queryClient.setQueryData<Sky>(key, sky);
        },
      ),
    staleTime: Infinity,
    // Tens of thousands of points: compare by reference, not deeply.
    structuralSharing: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return job && (status === "pending" || status === "streaming") ? POLL_MS : false;
    },
  });
}
