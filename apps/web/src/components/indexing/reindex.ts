import type { QueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";

/** Index the repository again (admins; 409 `index_job_running` while a run is under way). */
export function startReindex(org: string, repo: string) {
  return api.POST("/api/v1/orgs/{org}/repos/{repo}/reindex", { params: { path: { org, repo } } });
}

/** After starting or cancelling a run: everything that shows the repository's index state. */
export function refreshIndexState(
  queryClient: QueryClient,
  org: string,
  repo: string,
  job?: string,
) {
  return Promise.all([
    ...(job
      ? [queryClient.invalidateQueries({ queryKey: queryKeys.indexJob(org, repo, job) })]
      : []),
    queryClient.invalidateQueries({ queryKey: queryKeys.indexJobs(org, repo) }),
    queryClient.invalidateQueries({ queryKey: queryKeys.repo(org, repo) }),
    queryClient.invalidateQueries({ queryKey: queryKeys.repos(org) }),
  ]);
}
