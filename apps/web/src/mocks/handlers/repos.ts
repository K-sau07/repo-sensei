import { http, HttpResponse, sse } from "msw";

import type { AvailablePage, Estimate, IndexJob, Installation, Repo, Stage } from "@/lib/api/types";
import { atLeast, db, NOW, saveDb, type MockRepo } from "@/mocks/db";

import { apiError } from "./errors";
import { orgAccess } from "./orgs";
import { insufficientRole } from "./session";

/**
 * A mock index run takes a few seconds, worked out from the clock rather than timers, so it
 * carries on across reloads: queued, then clone, discover and merkle, then the repository is
 * ready. The org event stream reports each step as the real one does.
 */
const PLAN: { stage: Stage; at: number; progress: number }[] = [
  { stage: "clone", at: 0.4, progress: 0.0 },
  { stage: "discover", at: 1.6, progress: 0.05 },
  { stage: "merkle", at: 3.2, progress: 0.08 },
  { stage: "finalize", at: 4.2, progress: 0.98 },
];
const DONE_AT = 4.8;

const jobStarts = new Map<string, number>();

/** Public repositories the mock "GitHub" knows. */
const PUBLIC: Record<string, { stars: number; languages: Record<string, number>; size: number }> = {
  "langflow-ai/langflow": {
    stars: 120_000,
    languages: { Python: 31_500_000, TypeScript: 24_800_000, CSS: 400_000 },
    size: 1_600_000,
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

function estimateFor(fullName: string, isPrivate: boolean, languages: Record<string, number>) {
  const bytes = Object.values(languages).reduce((sum, value) => sum + value, 0);
  const files = Math.max(1, Math.round(bytes / 8000));
  return {
    full_name: fullName,
    private: isPrivate,
    size_kb: Math.round(bytes / 1024),
    languages,
    files,
    tokens: Math.round(bytes / 3.5),
    index_minutes: Math.round((0.5 + files / 2500) * 10) / 10,
    cost_usd: (Math.round((bytes / 3.5 / 1_000_000) * 0.12 * 100) / 100).toFixed(2),
    heuristic: true,
  } satisfies Estimate;
}

/** The repository as of now: a queued or running mock job moves on with the clock. */
export function current(repo: MockRepo): MockRepo {
  const job = repo.active_job;
  if (!job) return repo;
  const started = jobStarts.get(job.id) ?? Date.now();
  jobStarts.set(job.id, started);
  const elapsed = (Date.now() - started) / 1000;
  if (elapsed >= DONE_AT) {
    repo.active_job = null;
    repo.status = "ready";
    repo.last_indexed_sha = "c0ffee0000000000000000000000000000000001";
    repo.last_indexed_at = new Date().toISOString();
    repo.file_count = repo.file_count ?? 1234;
    repo.loc = repo.loc ?? 98_765;
    saveDb();
    return repo;
  }
  const step = PLAN.filter((s) => elapsed >= s.at).at(-1);
  repo.active_job = step
    ? {
        ...job,
        status: "running",
        stage: step.stage,
        progress: step.progress,
        eta_s: Math.ceil(DONE_AT - elapsed),
      }
    : job;
  repo.status = step ? "indexing" : repo.status;
  return repo;
}

const STAGE_ORDER: Stage[] = [
  "clone",
  "discover",
  "merkle",
  "parse",
  "symbols",
  "chunks",
  "embeddings",
  "summaries",
  "graph",
  "links",
  "finalize",
];

/** The repository's index runs, as the API lists them (newest first). */
function indexJobsOf(repo: MockRepo): IndexJob[] {
  const job = repo.active_job;
  const base = {
    repo_id: repo.id,
    snapshot_id: null,
    kind: "full" as const,
    error: null,
    created_at: repo.created_at,
    started_at: repo.created_at,
    finished_at: null,
    eta_s: null,
  };
  if (job) {
    const at = job.stage ? STAGE_ORDER.indexOf(job.stage) : -1;
    const done = STAGE_ORDER.slice(0, Math.max(0, at));
    return [
      {
        ...base,
        id: job.id,
        status: job.status,
        stage: job.stage,
        progress: job.progress,
        eta_s: job.eta_s,
        stages: Object.fromEntries(done.map((stage) => [stage, { status: "done" }])),
        features: done.includes("discover") ? ["code_browser"] : [],
      },
    ];
  }
  if (repo.status !== "ready") return [];
  return [
    {
      ...base,
      id: `${repo.id}-last`,
      status: "succeeded",
      stage: "finalize",
      progress: 1,
      finished_at: repo.last_indexed_at ?? null,
      stages: {},
      features: ["code_browser"],
    },
  ];
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
  const jobId = crypto.randomUUID();
  jobStarts.set(jobId, Date.now());
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
    index_job_id: jobId,
    file_count: null,
    loc: null,
    last_indexed_at: null,
    active_job: { id: jobId, status: "queued", stage: null, progress: 0, eta_s: null },
    created_at: NOW,
    updated_at: NOW,
  };
  reposOf(org).push(repo);
  saveDb();
  return repo;
}

function adminOf(org: string) {
  const access = orgAccess(org);
  if (access instanceof Response) return access;
  if (!atLeast(access.role, "admin")) return insufficientRole("admin", access.role);
  return access;
}

const orgEvents =
  typeof EventSource === "undefined"
    ? [] // Node (unit tests): no EventSource, so no SSE handler either
    : [
        sse<{ message: string }, { org: string }>(
          "*/api/v1/orgs/:org/events",
          ({ client, params }) => {
            // Report what changes, as the real org stream does: a stage starts, a run ends.
            const seen = new Map<string, string>();
            const tick = () => {
              for (const repo of reposOf(params.org)) {
                const before = repo.active_job;
                const now = current(repo);
                const key = now.active_job
                  ? `${now.active_job.status}:${now.active_job.stage ?? ""}`
                  : now.status;
                const jobId = before?.id ?? now.active_job?.id;
                if (!jobId || seen.get(repo.id) === key) continue;
                seen.set(repo.id, key);
                const type = now.active_job
                  ? now.active_job.stage
                    ? "stage_started"
                    : "queued"
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
            const timer = setInterval(tick, 300);
            setTimeout(() => {
              clearInterval(timer);
            }, 60_000);
          },
        ),
      ];

export const repoHandlers = [
  ...orgEvents,

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
    return HttpResponse.json<Repo[]>(reposOf(params.org).map(current));
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
    return HttpResponse.json(estimateFor(name, false, known.languages));
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
        ["chunks", 10, ["keyword_search"]],
        ["embeddings", 30, ["semantic_search", "ask"]],
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
      const access = orgAccess(params.org);
      if (access instanceof Response) return access;
      const repo = reposOf(params.org).find((r) => r.slug === params.repo);
      if (!repo) return apiError(404, "repo_not_found_or_no_access", "Repository not found.");
      return HttpResponse.json<IndexJob[]>(indexJobsOf(current(repo)));
    },
  ),

  http.get<{ org: string; repo: string }>("*/api/v1/orgs/:org/repos/:repo", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const repo = reposOf(params.org).find((r) => r.slug === params.repo);
    if (!repo) return apiError(404, "repo_not_found_or_no_access", "Repository not found.");
    return HttpResponse.json<Repo>(current(repo));
  }),
];
