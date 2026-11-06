/**
 * The mock staff admin console (10.6.3): the service today, people, orgs and index runs across
 * orgs, a failed run retried (a new queued run) and a queued or running one cancelled, and the
 * read-only switch. Staff only (Octo); anyone else gets a 404, as from the API.
 */
import { http, HttpResponse } from "msw";

import type { Schemas } from "@/lib/api/types";
import { db, OCTO } from "@/mocks/db";

import { apiError } from "./errors";
import { currentUser } from "./session";

type Job = Schemas["AdminJobOut"];

const at = (hours: number) => new Date(Date.now() - hours * 3600_000).toISOString();
let readOnly = false;

function job(n: number, status: Job["status"], hours: number, repo: string, org: string): Job {
  return {
    id: `0199f000-0000-7000-8000-0000000a${String(n).padStart(4, "0")}`,
    repo_id: `0199f000-0000-7000-8000-0000000b${String(n).padStart(4, "0")}`,
    snapshot_id: null,
    kind: "full",
    status,
    stage: status === "failed" ? "parse" : status === "running" ? "embeddings" : null,
    stages: {},
    progress: status === "running" ? 0.4 : 0,
    eta_s: null,
    error: status === "failed" ? { message: "the clone timed out after 600 s" } : null,
    features: [],
    partial: {},
    started_at: at(hours),
    finished_at: status === "failed" ? at(hours - 0.2) : null,
    created_at: at(hours),
    org_slug: org,
    repo_full_name: `${org}-gh/${repo}`,
    repo_slug: repo,
  };
}

let jobs: Job[] = [];
function seed(): Job[] {
  return [
    job(1, "failed", 2, "api", "acme"),
    job(2, "running", 1, "web", "globex"),
    job(3, "failed", 30, "billing", "initech"),
  ];
}

function staff(): Response | null {
  const user = currentUser();
  if (user instanceof Response) return user;
  return user.id === OCTO ? null : apiError(404, "not_found", "Not Found");
}

function overview(): Schemas["AdminOverviewOut"] {
  return {
    spent_today_usd: "12.40",
    cap_usd: "50",
    read_only: readOnly,
    users: db.users.length,
    orgs: db.orgs.length,
    repos: Object.values(db.repos).flat().length,
    failed_runs_today: jobs.filter(
      (j) =>
        j.status === "failed" &&
        j.created_at.slice(0, 10) === new Date().toISOString().slice(0, 10),
    ).length,
  };
}

/** The runs as they stand (stories). */
export function adminJobs(): readonly Job[] {
  return jobs;
}

/** Back to the seeded runs and switch (stories, between tests). */
export function resetAdminMocks(): void {
  jobs = seed();
  readOnly = false;
}
resetAdminMocks();

export const adminHandlers = [
  http.get("*/api/v1/internal/admin", () => staff() ?? HttpResponse.json(overview())),

  http.get("*/api/v1/internal/admin/jobs", ({ request }) => {
    const refused = staff();
    if (refused) return refused;
    const status = new URL(request.url).searchParams.get("status") ?? "failed";
    return HttpResponse.json(jobs.filter((j) => status === "any" || j.status === status));
  }),

  http.post<{ job: string }>("*/api/v1/internal/admin/jobs/:job/retry", ({ params }) => {
    const refused = staff();
    if (refused) return refused;
    const found = jobs.find((j) => j.id === params.job);
    if (!found) return apiError(404, "not_found", "Index run not found.");
    if (found.status !== "failed")
      return apiError(409, "not_failed", "Only a failed run is retried.");
    // As the API: one run at a time per repository.
    if (
      jobs.some((j) => j.repo_slug === found.repo_slug && ["queued", "running"].includes(j.status))
    ) {
      return apiError(409, "index_job_running", "This repository is already being indexed.");
    }
    const started: Job = {
      ...found,
      id: crypto.randomUUID(),
      status: "queued",
      stage: null,
      error: null,
      finished_at: null,
      created_at: new Date().toISOString(),
    };
    jobs = [started, ...jobs];
    return HttpResponse.json(started);
  }),

  http.post<{ job: string }>("*/api/v1/internal/admin/jobs/:job/cancel", ({ params }) => {
    const refused = staff();
    if (refused) return refused;
    const found = jobs.find((j) => j.id === params.job);
    if (!found) return apiError(404, "not_found", "Index run not found.");
    if (found.status !== "queued" && found.status !== "running") {
      return apiError(409, "index_job_finished", "That run has already finished.");
    }
    const cancelled: Job = { ...found, status: "cancelled", finished_at: new Date().toISOString() };
    jobs = jobs.map((j) => (j.id === found.id ? cancelled : j));
    return HttpResponse.json(cancelled);
  }),

  http.put("*/api/v1/internal/admin/read-only", async ({ request }) => {
    const refused = staff();
    if (refused) return refused;
    readOnly = ((await request.json()) as { on: boolean }).on;
    return HttpResponse.json(overview());
  }),

  http.get("*/api/v1/internal/admin/orgs", ({ request }) => {
    const refused = staff();
    if (refused) return refused;
    const q = (new URL(request.url).searchParams.get("q") ?? "").toLowerCase();
    return HttpResponse.json(
      db.orgs
        .filter((o) => !q || o.slug.includes(q) || o.name.toLowerCase().includes(q))
        .map((o) => ({
          id: o.id,
          slug: o.slug,
          name: o.name,
          plan: "free",
          members: (db.members[o.slug] ?? []).length,
          repos: (db.repos[o.slug] ?? []).length,
          spent_30d_usd: "8.20",
          deleted_at: null,
          created_at: "2026-10-01T12:00:00Z",
        })),
    );
  }),

  http.get("*/api/v1/internal/admin/users", ({ request }) => {
    const refused = staff();
    if (refused) return refused;
    const q = (new URL(request.url).searchParams.get("q") ?? "").toLowerCase();
    return HttpResponse.json(
      db.users
        .filter((u) => !q || u.login.includes(q) || (u.name ?? "").toLowerCase().includes(q))
        .map((u) => ({
          id: u.id,
          login: u.login,
          name: u.name,
          is_staff: u.id === OCTO,
          deleted: u.deleted === true,
          orgs: Object.values(db.members).filter((ms) => ms.some((m) => m.user_id === u.id)).length,
          created_at: "2026-10-01T12:00:00Z",
        })),
    );
  }),
];
