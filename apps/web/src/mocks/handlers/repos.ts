import { http, HttpResponse, sse } from "msw";

import type { AvailablePage, Estimate, IndexJob, Installation, Repo } from "@/lib/api/types";
import { atLeast, db, NOW, saveDb, type MockRepo } from "@/mocks/db";
import {
  cancelJob,
  eventsOf,
  isActive,
  jobsOf,
  jobView,
  onCancel,
  startJob,
  syncRepo,
  upcomingEvents,
  type RunFailure,
} from "@/mocks/jobs";

import { apiError } from "./errors";
import { orgAccess } from "./orgs";
import { insufficientRole } from "./session";

/** Imports that fail to index, for the failure states. */
const FAILING: Record<string, RunFailure> = {
  "big-corp/monorepo": {
    stage: "clone",
    code: "repo_too_large",
    message: "The repository is over 2.0 GB checked out, the limit.",
  },
};

/** Public repositories the mock "GitHub" knows. */
const PUBLIC: Record<string, { stars: number; languages: Record<string, number>; size: number }> = {
  "langflow-ai/langflow": {
    stars: 120_000,
    languages: { Python: 31_500_000, TypeScript: 24_800_000, CSS: 400_000 },
    size: 1_600_000,
  },
  "big-corp/monorepo": {
    stars: 1200,
    languages: { Java: 900_000_000, Kotlin: 400_000_000 },
    size: 3_000_000,
  },
  "fastapi/full-stack-fastapi-template": {
    stars: 40_000,
    languages: { TypeScript: 317_948, Python: 90_980, HTML: 21_286, CSS: 4187 },
    size: 7103,
  },
};

function parseUrl(raw: string): string | null {
  const match = /^(?:https?:\/\/)?(?:www\.)?github\.com\/([\w.-]+)\/([\w.-]+?)(?:\.git)?\/?$/i.exec(
    raw.trim(),
  );
  return match ? `${match[1]}/${match[2]}`.toLowerCase() : null;
}

export function estimateFor(
  fullName: string,
  isPrivate: boolean,
  languages: Record<string, number>,
  github: { stars?: number; sizeKb?: number } = {},
) {
  const bytes = Object.values(languages).reduce((sum, value) => sum + value, 0);
  const files = Math.max(1, Math.round(bytes / 8000));
  const searchReady = Math.round((0.5 + files / 2500) * 10) / 10;
  const summaries = Math.round((3.5 + files / 600) * 10) / 10; // the batch, in the background
  return {
    full_name: fullName,
    private: isPrivate,
    size_kb: github.sizeKb ?? Math.round(bytes / 1024),
    stars: github.stars ?? null,
    languages,
    files,
    tokens: Math.round(bytes / 3.5),
    index_minutes: searchReady + summaries,
    search_ready_minutes: searchReady,
    summaries_minutes: summaries,
    cost_usd: (Math.round((bytes / 3.5 / 1_000_000) * 0.12 * 100) / 100).toFixed(2),
    unpriced: [],
    heuristic: true,
  } satisfies Estimate;
}

export function repoOf(org: string, slug: string): MockRepo | Response {
  const access = orgAccess(org);
  if (access instanceof Response) return access;
  const repo = reposOf(org).find((r) => r.slug === slug);
  return repo
    ? syncRepo(repo)
    : apiError(404, "repo_not_found_or_no_access", "Repository not found.");
}

function reposOf(org: string): MockRepo[] {
  db.repos[org] ??= [];
  return db.repos[org];
}

function slugFor(org: string, owner: string, name: string): string {
  const taken = new Set(reposOf(org).map((r) => r.slug));
  const base = name.toLowerCase().replace(/[^a-z0-9._-]+/g, "-");
  const reserved = new Set(["new", "settings", "inbox", "activity", "insights", "repos"]);
  if (!taken.has(base) && !reserved.has(base)) return base;
  return `${owner}-${name}`.toLowerCase().replace(/[^a-z0-9._-]+/g, "-");
}

function connect(org: string, fields: Partial<Repo> & Pick<Repo, "full_name">): MockRepo {
  const [owner = "", name = ""] = fields.full_name.split("/");
  const repo: MockRepo = {
    id: crypto.randomUUID(),
    slug: slugFor(org, owner, name),
    full_name: fields.full_name,
    owner,
    name,
    source: fields.source ?? "github_app",
    visibility: fields.visibility ?? "private",
    default_branch: "main",
    status: "pending",
    size_kb: fields.size_kb ?? 1024,
    languages: fields.languages ?? {},
    settings: { tracked_branches: [], ignore_globs: [], auto_index: true },
    last_indexed_sha: null,
    index_job_id: null,
    file_count: null,
    loc: null,
    last_indexed_at: null,
    active_job: null,
    created_at: NOW,
    updated_at: NOW,
  };
  reposOf(org).push(repo);
  const job = startJob(repo.id, FAILING[fields.full_name]);
  repo.index_job_id = job.id;
  syncRepo(repo);
  saveDb();
  return repo;
}

function adminOf(org: string) {
  const access = orgAccess(org);
  if (access instanceof Response) return access;
  if (!atLeast(access.role, "admin")) return insufficientRole("admin", access.role);
  return access;
}

const TERMINAL = new Set(["done", "failed", "cancelled"]);
/** The org stream's event for a run that ended, by how it ended. */
const END_EVENT: Record<IndexJob["status"], string> = {
  queued: "queued",
  running: "stage_started",
  succeeded: "done",
  failed: "failed",
  cancelled: "cancelled",
};

const jobEvents =
  typeof EventSource === "undefined"
    ? []
    : [
        sse<{ message: string }, { org: string; repo: string; job: string }>(
          "*/api/v1/orgs/:org/repos/:repo/index-jobs/:job/events",
          ({ client, params, request }) => {
            const found = reposOf(params.org).find((r) => r.slug === params.repo);
            const job = found ? jobsOf(found.id).find((j) => j.id === params.job) : undefined;
            if (!job) {
              client.close();
              return;
            }
            // Resume after the last event the browser saw, as the real stream does.
            const after = request.headers.get("last-event-id");
            const past = eventsOf(job);
            const start = after ? past.findIndex((e) => e.id === after) + 1 : 0;
            const send = (event: (typeof past)[number]) => {
              const { at: _at, id, ...payload } = event;
              client.send({
                id,
                event: event.type,
                data: JSON.stringify({
                  ...payload,
                  id,
                  job_id: job.id,
                  ts: new Date(job.queued_ms + event.at).toISOString(),
                }),
              } as never);
              if (TERMINAL.has(event.type)) client.close();
            };
            for (const event of past.slice(start)) send(event);
            if (past.some((e) => TERMINAL.has(e.type))) return;
            const timers = upcomingEvents(job).map(({ delay, event }) =>
              setTimeout(() => {
                if (job.cancelled_after_ms === undefined) send(event);
              }, delay),
            );
            const clear = () => {
              for (const timer of timers) clearTimeout(timer);
            };
            const unsubscribe = onCancel(job, () => {
              clear();
              const cancelled = eventsOf(job).at(-1);
              if (cancelled?.type === "cancelled") send(cancelled);
            });
            // The request was aborted: nothing more to send. (MSW's EventSource stand-in doesn't
            // report the page calling close(), so a run hidden mid-way can still log "Failed to
            // write" for its remaining few seconds; the real stream has no such timers.)
            request.signal.addEventListener(
              "abort",
              () => {
                clear();
                unsubscribe();
              },
              { once: true },
            );
          },
        ),
      ];

const orgEvents =
  typeof EventSource === "undefined"
    ? [] // Node (unit tests): no EventSource, so no SSE handler either
    : [
        sse<{ message: string }, { org: string }>(
          "*/api/v1/orgs/:org/events",
          ({ client, params, request }) => {
            // Report what changes, as the real org stream does: a stage starts, a run ends.
            const seen = new Map<string, string>();
            const tick = () => {
              for (const repo of reposOf(params.org)) {
                const before = repo.active_job;
                const now = syncRepo(repo);
                const key = now.active_job
                  ? `${now.active_job.status}:${now.active_job.stage ?? ""}`
                  : now.status;
                const jobId = before?.id ?? now.active_job?.id;
                if (!jobId || seen.get(repo.id) === key) continue;
                seen.set(repo.id, key);
                const ended = jobsOf(repo.id).at(-1);
                const type = now.active_job
                  ? now.active_job.stage
                    ? "stage_started"
                    : "queued"
                  : ended
                    ? END_EVENT[jobView(ended).status]
                    : "done";
                client.send({
                  data: JSON.stringify({
                    type,
                    job_id: jobId,
                    repo_id: repo.id,
                    stage: now.active_job?.stage ?? "finalize",
                    progress: now.active_job?.progress ?? 1,
                    ts: new Date().toISOString(),
                  }),
                });
              }
            };
            tick();
            // Like the real stream, it stays open until the browser closes it.
            const timer = setInterval(tick, 300);
            request.signal.addEventListener(
              "abort",
              () => {
                clearInterval(timer);
              },
              { once: true },
            );
          },
        ),
      ];

export const repoHandlers = [
  ...orgEvents,
  ...jobEvents,

  http.get<{ org: string }>("*/api/v1/orgs/:org/github/installations", ({ params }) => {
    const access = adminOf(params.org);
    if (access instanceof Response) return access;
    return HttpResponse.json<Installation[]>([
      {
        id: "0199f000-0000-7000-8000-0000000000e1",
        installation_id: 555_001,
        account_login: "acme-gh",
        account_type: "Organization",
        repository_selection: "selected",
        suspended: false,
        manage_url: "https://github.com/organizations/acme-gh/settings/installations/555001",
        created_at: NOW,
        updated_at: NOW,
      },
    ]);
  }),

  http.get<{ org: string }>("*/api/v1/orgs/:org/github/repos", ({ params, request }) => {
    const access = adminOf(params.org);
    if (access instanceof Response) return access;
    const url = new URL(request.url);
    const q = (url.searchParams.get("q") ?? "").trim().toLowerCase();
    const page = Number(url.searchParams.get("page") ?? 1);
    const perPage = Number(url.searchParams.get("per_page") ?? 30);
    const connected = new Set(reposOf(params.org).map((r) => r.full_name));
    const all = (db.available[params.org] ?? [])
      .filter((r) => !q || r.full_name.toLowerCase().includes(q))
      .map((r) => ({ ...r, connected: connected.has(r.full_name) }));
    return HttpResponse.json<AvailablePage>({
      items: all.slice((page - 1) * perPage, page * perPage),
      page,
      per_page: perPage,
      total: all.length,
    });
  }),

  http.get<{ org: string }>("*/api/v1/orgs/:org/repos", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    return HttpResponse.json<Repo[]>(reposOf(params.org).map(syncRepo));
  }),

  http.post<{ org: string }>("*/api/v1/orgs/:org/repos/estimate", async ({ params, request }) => {
    const access = adminOf(params.org);
    if (access instanceof Response) return access;
    const body = (await request.json()) as { url?: string; github_repo_id?: number };
    if (body.github_repo_id !== undefined) {
      const found = (db.available[params.org] ?? []).find(
        (r) => r.github_repo_id === body.github_repo_id,
      );
      if (!found) {
        return apiError(
          404,
          "repo_not_in_installation",
          "None of this organization's GitHub installations can read that repository.",
        );
      }
      return HttpResponse.json(
        estimateFor(found.full_name, found.private, { [found.language ?? "Text"]: 240_000 }),
      );
    }
    const name = parseUrl(body.url ?? "");
    if (!name) {
      return apiError(
        422,
        "invalid_repo_url",
        "Paste a GitHub repository URL, like https://github.com/owner/name.",
      );
    }
    const known = PUBLIC[name];
    if (!known) {
      return apiError(
        422,
        "repo_not_public",
        `${name} isn't a public GitHub repository (or doesn't exist). Private repositories connect through the GitHub App.`,
      );
    }
    return HttpResponse.json(
      estimateFor(name, false, known.languages, { stars: known.stars, sizeKb: known.size }),
    );
  }),

  http.post<{ org: string }>("*/api/v1/orgs/:org/repos/import", async ({ params, request }) => {
    const access = adminOf(params.org);
    if (access instanceof Response) return access;
    const body = (await request.json()) as { url: string };
    const name = parseUrl(body.url);
    if (!name) {
      return apiError(
        422,
        "invalid_repo_url",
        "Paste a GitHub repository URL, like https://github.com/owner/name.",
      );
    }
    const known = PUBLIC[name];
    if (!known) {
      return apiError(
        422,
        "repo_not_public",
        `${name} isn't a public GitHub repository (or doesn't exist). Private repositories connect through the GitHub App.`,
      );
    }
    if (name === "langflow-ai/langflow" && body.url.includes("cap")) {
      return apiError(
        429,
        "cost_cap_reached",
        "Indexing this would go over the organization's daily spend limit.",
      );
    }
    const existing = reposOf(params.org).find((r) => r.full_name === name);
    if (existing) {
      return apiError(409, "repo_already_connected", `${name} is already connected.`, {
        slug: existing.slug,
      });
    }
    const repo = connect(params.org, {
      full_name: name,
      source: "public_url",
      visibility: "public",
      languages: known.languages,
      size_kb: known.size,
    });
    return HttpResponse.json<Repo>(repo, { status: 201 });
  }),

  http.post<{ org: string }>("*/api/v1/orgs/:org/repos", async ({ params, request }) => {
    const access = adminOf(params.org);
    if (access instanceof Response) return access;
    const body = (await request.json()) as { github_repo_id: number };
    const found = (db.available[params.org] ?? []).find(
      (r) => r.github_repo_id === body.github_repo_id,
    );
    if (!found) {
      return apiError(
        404,
        "repo_not_in_installation",
        "None of this organization's GitHub installations can read that repository.",
      );
    }
    const existing = reposOf(params.org).find((r) => r.full_name === found.full_name);
    if (existing) {
      return apiError(409, "repo_already_connected", `${found.full_name} is already connected.`, {
        slug: existing.slug,
      });
    }
    const repo = connect(params.org, {
      full_name: found.full_name,
      languages: { [found.language ?? "Text"]: 240_000 },
      visibility: found.private ? "private" : "public",
    });
    return HttpResponse.json<Repo>(repo, { status: 201 });
  }),

  http.get("*/api/v1/indexing/stages", () =>
    HttpResponse.json({
      stages: [
        ["clone", 5, []],
        ["discover", 3, ["code_browser"]],
        ["merkle", 2, []],
        ["parse", 20, []],
        ["symbols", 10, ["goto_definition"]],
        ["chunks", 10, ["keyword_search", "ask"]],
        ["embeddings", 30, ["semantic_search"]],
        ["summaries", 10, ["wiki"]],
        ["graph", 5, ["graph_view", "callers"]],
        ["links", 3, ["trace", "diagrams", "fixes", "changes"]],
        ["finalize", 2, []],
      ].map(([stage, weight, unlocks]) => ({ stage, weight, unlocks })),
    }),
  ),

  http.get<{ org: string; repo: string }>(
    "*/api/v1/orgs/:org/repos/:repo/index-jobs",
    ({ params }) => {
      const found = repoOf(params.org, params.repo);
      if (found instanceof Response) return found;
      return HttpResponse.json<IndexJob[]>(jobsOf(found.id).map(jobView).reverse());
    },
  ),

  http.get<{ org: string; repo: string; job: string }>(
    "*/api/v1/orgs/:org/repos/:repo/index-jobs/:job",
    ({ params }) => {
      const found = repoOf(params.org, params.repo);
      if (found instanceof Response) return found;
      const job = jobsOf(found.id).find((j) => j.id === params.job);
      if (!job) return apiError(404, "index_job_not_found", "Index job not found.");
      return HttpResponse.json<IndexJob>(jobView(job));
    },
  ),

  http.post<{ org: string; repo: string }>(
    "*/api/v1/orgs/:org/repos/:repo/reindex",
    ({ params }) => {
      const access = adminOf(params.org);
      if (access instanceof Response) return access;
      const found = repoOf(params.org, params.repo);
      if (found instanceof Response) return found;
      const latest = jobsOf(found.id).at(-1);
      if (latest && isActive(jobView(latest))) {
        return apiError(409, "index_job_running", "This repository is already being indexed.");
      }
      const job = startJob(found.id, FAILING[found.full_name]);
      syncRepo(found);
      return HttpResponse.json<IndexJob>(jobView(job), { status: 202 });
    },
  ),

  http.post<{ org: string; repo: string; job: string }>(
    "*/api/v1/orgs/:org/repos/:repo/index-jobs/:job/cancel",
    ({ params }) => {
      const access = adminOf(params.org);
      if (access instanceof Response) return access;
      const found = repoOf(params.org, params.repo);
      if (found instanceof Response) return found;
      const job = jobsOf(found.id).find((j) => j.id === params.job);
      if (!job) return apiError(404, "index_job_not_found", "Index job not found.");
      if (!isActive(jobView(job))) {
        return apiError(409, "index_job_finished", "This index job has already ended.");
      }
      cancelJob(job);
      syncRepo(found);
      return HttpResponse.json<IndexJob>(jobView(job));
    },
  ),

  http.get<{ org: string; repo: string }>("*/api/v1/orgs/:org/repos/:repo", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const repo = reposOf(params.org).find((r) => r.slug === params.repo);
    if (!repo) return apiError(404, "repo_not_found_or_no_access", "Repository not found.");
    return HttpResponse.json<Repo>(syncRepo(repo));
  }),
];
