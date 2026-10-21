/** The mock `GET …/constellation`: the fixture commit's layout (mocks/fixtures/constellation.json),
 * as much of it as the mock run has found (see mocks/index-run.ts), in pages like the API's. */
import { http, HttpResponse } from "msw";

import type { ConstellationPage } from "@/lib/api/types";
import { db } from "@/mocks/db";
import { SKY } from "@/mocks/index-run";
import { eventsOf, isActive, jobsOf, jobView } from "@/mocks/jobs";

import { apiError } from "./errors";
import { orgAccess } from "./orgs";

const PAGE = 10_000;

function page(
  status: ConstellationPage["status"],
  found: number,
  since: number,
  limit: number,
): ConstellationPage {
  if (found === 0) {
    return {
      ...SKY,
      status,
      generation: null,
      total: null,
      available: 0,
      since,
      next: since,
      dirs: [],
      points: [],
    };
  }
  const points = SKY.points.slice(since, Math.min(found, since + limit));
  return {
    ...SKY,
    status,
    available: found,
    since,
    next: since + points.length,
    dirs: since === 0 ? SKY.dirs : [],
    points,
  };
}

export const constellationHandlers = [
  http.get<{ org: string; repo: string }>(
    "*/api/v1/orgs/:org/repos/:repo/constellation",
    ({ params, request }) => {
      const access = orgAccess(params.org);
      if (access instanceof Response) return access;
      const repo = db.repos[params.org]?.find((r) => r.slug === params.repo);
      if (!repo) return apiError(404, "repo_not_found_or_no_access", "Repository not found.");
      const url = new URL(request.url);
      const since = Math.max(0, Number(url.searchParams.get("since") ?? 0));
      const limit = Math.max(1, Number(url.searchParams.get("limit") ?? PAGE));
      const jobId = url.searchParams.get("job");
      if (!jobId) {
        if (!repo.last_indexed_sha) {
          return apiError(404, "repo_not_indexed", "This repository hasn't been indexed yet.");
        }
        return HttpResponse.json(page("complete", SKY.points.length, since, limit));
      }
      const job = jobsOf(repo.id).find((j) => j.id === jobId);
      if (!job) return apiError(404, "index_job_not_found", "Index job not found.");
      const view = jobView(job);
      const discover = (view.stages as Record<string, { status?: string }>).discover;
      if (discover?.status === "done") {
        return HttpResponse.json(page("complete", SKY.points.length, since, limit));
      }
      const told = eventsOf(job).findLast((event) => event.type === "constellation");
      const found = typeof told?.cursor === "number" ? told.cursor : 0;
      const status = isActive(view) ? (found ? "streaming" : "pending") : "stopped";
      return HttpResponse.json(page(status, found, since, limit));
    },
  ),
];
