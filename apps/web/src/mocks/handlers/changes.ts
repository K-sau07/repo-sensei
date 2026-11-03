/**
 * The mock requested changes (9.3): a repository's changes (seeded: one waiting for approval,
 * one built to a draft pull request, one rejected), and a change moved on by the clock as the
 * pipeline would: planned (a canned plan, its files by layer, its tests, the patterns it
 * follows, its blast radius), planned again after edits (the edits applied), built and
 * verified once approved, to a draft pull request (the mock's stand-in for the PR stage).
 * Kept in the mock database per repository.
 */
import { http, HttpResponse } from "msw";

import type { Change, ChangePlan, PlanEdits, PlannedFile } from "@/lib/api/changes";
import type { Ticket } from "@/lib/api/fixes";
import { atLeast, db, HUBOT, NOW, OCTO, roleIn, saveDb, type MockRepo } from "@/mocks/db";

import { apiError } from "./errors";
import { orgAccess } from "./orgs";
import { repoOf, reposOf } from "./repos";
import { insufficientRole } from "./session";

interface Params {
  org: string;
  repo: string;
  id: string;
}

/** Where a change is, and since when (the mock moves it on as time passes). */
export interface MockChange {
  change: Change;
  phase: "planning" | "replanning" | "waiting" | "building" | "done";
  since: number;
}

const PLAN_MS = 1_200;
const BUILD_MS = 2_400;

function planFor(title: string): ChangePlan {
  return {
    summary: `Limit each client's requests to the items API (${title.toLowerCase()}).`,
    files: [
      { path: "backend/app/api/routes/items.py", change: "modify", layer: "api",
        why: "The item routes depend on the limiter." },
      { path: "backend/app/api/limits.py", change: "create", layer: "api",
        why: "The limiter: a sliding window per client, 60 a minute." },
      { path: "backend/app/core/config.py", change: "modify", layer: "svc",
        why: "The limit and window as settings." },
      { path: "frontend/src/client/core/request.ts", change: "modify", layer: "ui",
        why: "Show 'Too many requests' when the API answers 429." },
    ],
    tests: [{ path: "backend/app/tests/api/routes/test_rate_limit.py",
              checks: "The 61st request in a minute gets 429; another client isn't limited." }],
    patterns: [{ claim: "Routes take their dependencies this way (SessionDep, CurrentUser)",
                 path: "backend/app/api/deps.py", start_line: 21, end_line: 57 }],
    steps: [
      { description: "Add the limiter", path: "backend/app/api/limits.py" },
      { description: "Add the settings", path: "backend/app/core/config.py" },
      { description: "Depend on it in the item routes", path: "backend/app/api/routes/items.py" },
      { description: "Say so in the client", path: "frontend/src/client/core/request.ts" },
      { description: "Test the limit", path: "backend/app/tests/api/routes/test_rate_limit.py" },
    ],
    risks: ["Clients behind one proxy share a limit.", "The window is per process, not shared."],
    risk: { level: "medium", reasons: ["4 files (over 2)", "4 routes depend on it"] },
    blast_radius: {
      changed: ["py backend/app/api/routes/items.py/read_items"],
      counts: { routes: 4, screens: 2, tests: 3, tables: 1, symbols: 6 },
      top: {
        routes: [{ key: "r1", name: "GET /api/v1/items/", path: "backend/app/api/routes/items.py",
                   score: 1, distance: 1 }],
        screens: [{ key: "s1", name: "Items", path: "frontend/src/routes/_layout/items.tsx",
                    score: 0.72, distance: 2 }],
        tables: [{ key: "t1", name: "item", path: null, score: 0.85, distance: 1 }],
      },
      truncated: false,
    },
    revision: 1,
    requests: [],
  }; // prettier-ignore
}

function ticketOf(request: string): Ticket {
  const text = request.trim();
  const first = text.split("\n")[0] ?? "";
  return {
    title: first.length <= 120 ? first : `${first.slice(0, 119).trimEnd()}…`,
    language: null,
    error_type: null,
    message: null,
    frames: [],
    versions: {},
    culprit: null,
    text,
  };
}

function made(repo: MockRepo, id: string, request: string, fields: Partial<Change>): Change {
  const ticket = ticketOf(request);
  return {
    id,
    kind: "change",
    repo: repo.slug,
    title: ticket.title,
    source: "request",
    source_ref: null,
    status: "queued",
    stage: "intake",
    risk: null,
    merge_readiness: null,
    pr_url: null,
    created_by: OCTO,
    author: "octo",
    created_at: NOW,
    finished_at: null,
    ticket,
    repro_result: null,
    root_cause: null,
    repro: null,
    plan: null,
    patch: null,
    verification: null,
    stop_reason: null,
    approvals_needed: null,
    run_id: null,
    ...fields,
  };
}

function seed(repo: MockRepo): MockChange[] {
  const id = (n: number) => `0199f000-0000-7000-8000-00000000c${String(n).padStart(3, "0")}`;
  const waiting = planFor("60 a minute per client");
  return [
    {
      phase: "waiting",
      since: 0,
      change: made(repo, id(1), "Add rate limiting to the items API.\n\n60 requests a minute per client, 429 after that.", {
        status: "awaiting_approval", stage: "awaiting_approval", risk: "medium",
        plan: waiting as unknown as Change["plan"], approvals_needed: 1,
        created_at: "2026-10-18T11:30:00Z",
      }),
    },
    {
      phase: "done",
      since: 0,
      change: made(repo, id(2), "Add a created_at sort to the items table.", {
        status: "succeeded", stage: "done", risk: "low", merge_readiness: 4,
        pr_url: "https://github.com/acme/api/pull/57", created_by: HUBOT, author: "hubot",
        created_at: "2026-10-17T15:00:00Z", finished_at: "2026-10-17T15:21:00Z",
      }),
    },
    {
      phase: "done",
      since: 0,
      change: made(repo, id(3), "Move the items API to GraphQL.", {
        status: "rejected", stage: "awaiting_approval",
        stop_reason: "Rejected by @octo: too big for one change; plan it per endpoint.",
        created_at: "2026-10-16T10:00:00Z", finished_at: "2026-10-16T10:40:00Z",
      }),
    },
  ]; // prettier-ignore
}

function changesOf(repo: MockRepo): MockChange[] {
  db.changes ??= {};
  db.changes[repo.id] ??= seed(repo);
  return db.changes[repo.id] ?? [];
}

function applied(plan: ChangePlan, edits: PlanEdits | null | undefined): ChangePlan {
  if (!edits) return plan;
  const layerOf = (path: string) => plan.files.find((f) => f.path === path)?.layer ?? "other";
  return {
    ...plan,
    summary: edits.summary ?? plan.summary,
    files: edits.files
      ? edits.files.map((f): PlannedFile => ({ ...f, layer: layerOf(f.path) }))
      : plan.files,
    tests: edits.tests ?? plan.tests,
    steps: edits.steps ?? plan.steps,
  };
}

/** The change as it is now: moved on by the time passed since its phase began. */
function now(mock: MockChange): Change {
  const passed = Date.now() - mock.since;
  const { change } = mock;
  if (mock.phase === "planning" || mock.phase === "replanning") {
    if (passed >= PLAN_MS) {
      const before = change.plan as unknown as ChangePlan | null;
      const last = before?.requests.at(-1);
      const plan = before
        ? { ...applied(before, last?.edits), revision: before.revision + 1 }
        : planFor(change.title);
      Object.assign(change, {
        status: "awaiting_approval", stage: "awaiting_approval", risk: plan.risk?.level ?? null,
        plan, approvals_needed: plan.risk?.level === "high" ? 2 : 1,
      }); // prettier-ignore
      mock.phase = "waiting";
      saveDb();
    } else if (passed >= PLAN_MS / 2) {
      Object.assign(change, { status: "running", stage: "plan" });
    }
  } else if (mock.phase === "building") {
    if (passed >= BUILD_MS) {
      const plan = change.plan as unknown as ChangePlan;
      Object.assign(change, {
        status: "succeeded", stage: "done", merge_readiness: 4, finished_at: new Date().toISOString(),
        pr_url: "https://github.com/acme/api/pull/58",
        patch: { files: plan.files.map((f) => f.path), tests: plan.tests.map((t) => t.path),
                 summary: "The limiter, its settings, the routes depending on it, and its test." },
        verification: {
          readiness: { score: 4, reason: "Medium risk." },
          unverified: ["Limits shared across processes (one process in the sandbox)."],
        },
      }); // prettier-ignore
      mock.phase = "done";
      saveDb();
    } else {
      const stage = passed >= BUILD_MS / 2 ? "verify" : "patch";
      Object.assign(change, { status: "running", stage });
    }
  }
  return change;
}

function summary(change: Change) {
  const {
    ticket: _t,
    repro_result: _r,
    root_cause: _c,
    repro: _p,
    plan: _l,
    patch: _a,
    verification: _v,
    stop_reason: _s,
    approvals_needed: _n,
    ...rest
  } = change;
  return rest;
}

function found(org: string, id: string): MockChange | Response {
  const access = orgAccess(org);
  if (access instanceof Response) return access;
  for (const repo of reposOf(org)) {
    const mock = changesOf(repo).find((m) => m.change.id === id);
    if (mock) return mock;
  }
  return apiError(404, "change_not_found", "No such change.");
}

function writer(org: string): Response | { login: string; id: string } {
  const access = orgAccess(org);
  if (access instanceof Response) return access;
  const role = roleIn(org, access.user.id);
  if (!atLeast(role, "member")) return insufficientRole("member", role ?? "none");
  return { login: access.user.login, id: access.user.id };
}

function stale(mock: MockChange, revision: number): Response | null {
  const plan = mock.change.plan as unknown as ChangePlan | null;
  if (mock.change.status !== "awaiting_approval")
    return apiError(409, "fix_not_awaiting_approval", "The change isn't waiting for approval.");
  if (plan?.revision !== revision)
    return apiError(409, "fix_plan_changed", "The plan changed: read it again.");
  return null;
}

export const changeHandlers = [
  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/changes", ({ params }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const changes = changesOf(repo)
      .map(now)
      .sort((a, b) => b.created_at.localeCompare(a.created_at));
    return HttpResponse.json(changes.map(summary));
  }),

  http.post<Params>("*/api/v1/orgs/:org/repos/:repo/changes", async ({ params, request }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const who = writer(params.org);
    if (who instanceof Response) return who;
    const body = (await request.json()) as { request?: string };
    const text = (body.request ?? "").trim();
    if (!text) return apiError(422, "validation_failed", "Describe the change.");
    const change = made(repo, crypto.randomUUID(), text, {
      created_by: who.id,
      author: who.login,
      created_at: new Date().toISOString(),
    });
    changesOf(repo).push({ change, phase: "planning", since: Date.now() });
    saveDb();
    return HttpResponse.json(change, { status: 201 });
  }),

  http.get<Params>("*/api/v1/orgs/:org/changes/:id", ({ params }) => {
    const mock = found(params.org, params.id);
    return mock instanceof Response ? mock : HttpResponse.json(now(mock));
  }),

  http.patch<Params>("*/api/v1/orgs/:org/changes/:id/plan", async ({ params, request }) => {
    const who = writer(params.org);
    if (who instanceof Response) return who;
    const mock = found(params.org, params.id);
    if (mock instanceof Response) return mock;
    const body = (await request.json()) as {
      plan_revision: number;
      edits: PlanEdits;
      comment?: string | null;
    };
    now(mock);
    const refused = stale(mock, body.plan_revision);
    if (refused) return refused;
    const plan = mock.change.plan as unknown as ChangePlan;
    const asked = { by: who.login, comment: body.comment ?? null, edits: body.edits,
                    revision: plan.revision }; // prettier-ignore
    mock.change.plan = {
      ...plan,
      requests: [...plan.requests, asked],
    };
    Object.assign(mock.change, { status: "queued", stage: "plan" });
    mock.phase = "replanning";
    mock.since = Date.now();
    saveDb();
    return HttpResponse.json(mock.change);
  }),

  http.post<Params>("*/api/v1/orgs/:org/changes/:id/approval", async ({ params, request }) => {
    const who = writer(params.org);
    if (who instanceof Response) return who;
    const mock = found(params.org, params.id);
    if (mock instanceof Response) return mock;
    const body = (await request.json()) as {
      decision: "approve" | "request_changes" | "reject";
      plan_revision: number;
      comment?: string | null;
    };
    now(mock);
    const refused = stale(mock, body.plan_revision);
    if (refused) return refused;
    if (body.decision === "reject") {
      const why = body.comment ? `: ${body.comment}` : ".";
      Object.assign(mock.change, { status: "rejected", stop_reason: `Rejected by @${who.login}${why}`,
                                   finished_at: new Date().toISOString() }); // prettier-ignore
      mock.phase = "done";
    } else if (body.decision === "request_changes") {
      const plan = mock.change.plan as unknown as ChangePlan;
      const asked = { by: who.login, comment: body.comment ?? null, edits: null,
                      revision: plan.revision }; // prettier-ignore
      mock.change.plan = {
        ...plan,
        requests: [...plan.requests, asked],
      };
      Object.assign(mock.change, { status: "queued", stage: "plan" });
      mock.phase = "replanning";
    } else {
      Object.assign(mock.change, { status: "queued", stage: "patch" });
      mock.phase = "building";
    }
    mock.since = Date.now();
    saveDb();
    return HttpResponse.json(mock.change);
  }),

  http.post<Params>("*/api/v1/orgs/:org/changes/:id/cancel", ({ params }) => {
    const who = writer(params.org);
    if (who instanceof Response) return who;
    const mock = found(params.org, params.id);
    if (mock instanceof Response) return mock;
    Object.assign(mock.change, { status: "cancelled", stop_reason: `Cancelled by @${who.login}.` });
    mock.phase = "done";
    saveDb();
    return HttpResponse.json(mock.change);
  }),
];
