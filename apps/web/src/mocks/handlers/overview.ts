/**
 * The mock overview and org home (4.13). The home's live agents are the mock run while
 * `sensei.mock.runs` is "live"; its recent questions, the conversations asked in the mocks.
 */
import { http, HttpResponse } from "msw";

import type { Home, Overview } from "@/lib/api/overview";
import { syncRepo } from "@/mocks/jobs";
import { mockOverview } from "@/mocks/overview";
import { mockRun, mockRunSteps } from "@/mocks/runs";

import { storedConversations } from "./ask";
import { apiError } from "./errors";
import { orgAccess } from "./orgs";
import { reposOf } from "./repos";

const MAX_RECENT = 8;

function liveRuns(): boolean {
  return typeof localStorage !== "undefined" && localStorage.getItem("sensei.mock.runs") === "live";
}

export const overviewHandlers = [
  http.get<{ org: string; repo: string }>(
    "*/api/v1/orgs/:org/repos/:repo/overview",
    ({ params }) => {
      const access = orgAccess(params.org);
      if (access instanceof Response) return access;
      const repo = reposOf(params.org).find((r) => r.slug === params.repo);
      if (!repo) return apiError(404, "repo_not_found_or_no_access", "Repository not found.");
      if (!repo.last_indexed_sha) {
        return apiError(404, "snapshot_not_found", "This repository hasn't been indexed yet.");
      }
      return HttpResponse.json<Overview>(mockOverview());
    },
  ),
  http.get<{ org: string }>("*/api/v1/orgs/:org/home", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const repos = reposOf(params.org).map(syncRepo);
    const ids = new Map(repos.map((r) => [r.slug, r.id]));
    const first = repos[0];
    const { steps: _, ...running } = mockRun(mockRunSteps().slice(0, 6), "running");
    return HttpResponse.json<Home>({
      repos,
      live: liveRuns() && first ? [{ ...running, repo_id: first.id }] : [],
      recent: storedConversations()
        .filter((c) => ids.has(c.repo))
        .slice(0, MAX_RECENT)
        .map(({ repo, ...c }) => ({ ...c, repo_id: ids.get(repo) ?? c.repo_id })),
    });
  }),
];
