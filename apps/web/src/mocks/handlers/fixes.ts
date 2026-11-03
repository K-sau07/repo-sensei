/**
 * The mock fixes (8.11): a repository's fixes (seeded with one in each kind of state), a paste
 * parsed as the API would (Python tracebacks and JavaScript stacks, their frames mapped to the
 * mock snapshot's files by the longest matching end of the path), starting a fix from a paste
 * or a GitHub issue (#404 is one GitHub doesn't have), and starting one Sentry left for triage.
 * Kept in the mock database per repository.
 */
import { http, HttpResponse } from "msw";

import type { Fix, FixSummary, Frame, Ticket } from "@/lib/api/fixes";
import { atLeast, db, HUBOT, NOW, OCTO, roleIn, saveDb, type MockRepo } from "@/mocks/db";
import tree from "@/mocks/fixtures/browse-tree.json";

import { apiError } from "./errors";
import { orgAccess } from "./orgs";
import { repoOf, reposOf } from "./repos";
import { insufficientRole } from "./session";

interface Params {
  org: string;
  repo: string;
  id: string;
}

const PATHS: string[] = tree.files.map((file) => file.path);
const PY_FRAME = /^\s*File "(.+?)", line (\d+)(?:, in (\S+))?/;
const JS_FRAME = /^\s*at (?:(.+?) \()?(.+?):(\d+):\d+\)?\s*$/;
const ERROR_LINE = /^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)):\s?(.*)$/;
const LIBRARY = /site-packages|node_modules|\/lib\/python\d|<frozen|node:internal/;

/** The repository's file a frame's path ends with (the longest end that matches one). */
function repoPath(file: string): string | null {
  const parts = file.replace(/\\/g, "/").split("/").filter(Boolean);
  for (let start = 0; start < parts.length; start++) {
    const tail = parts.slice(start).join("/");
    const found = PATHS.filter((path) => path === tail || path.endsWith(`/${tail}`));
    if (found.length === 1) return found[0] ?? null;
    if (found.length > 1 && start === parts.length - 1) return null; // a bare name, ambiguous
  }
  return null;
}

/** A paste as the API's parser reads it (simplified: Python and JavaScript, no versions). */
export function mockParse(text: string, title?: string | null): Ticket {
  const frames: Frame[] = [];
  let errorType: string | null = null;
  let message: string | null = null;
  let language: Ticket["language"] = null;
  for (const line of text.split("\n")) {
    const py = PY_FRAME.exec(line);
    const js = py ? null : JS_FRAME.exec(line);
    if (py ?? js) {
      const file = (py ? py[1] : js?.[2]) ?? "";
      const at = Number((py ? py[2] : js?.[3]) ?? "0");
      const fn = (py ? py[3] : js?.[1]) ?? null;
      language = py ? "python" : "javascript";
      const library = LIBRARY.test(file);
      const path = library ? null : repoPath(file);
      frames.push({ file, line: at, function: fn, path, in_repo: path !== null, library });
      continue;
    }
    const error = ERROR_LINE.exec(line.trim());
    if (error) {
      errorType = error[1] ?? null;
      message = error[2] ?? null;
    }
  }
  // JavaScript stacks list the innermost frame first: outermost first, as Python's.
  if (language === "javascript") frames.reverse();
  const culprit = [...frames].reverse().find((frame) => frame.in_repo) ?? null;
  const first = text.trim().split("\n")[0]?.slice(0, 120) ?? "";
  const given = title?.trim() ?? "";
  const said = errorType ? `${errorType}: ${message ?? ""}`.replace(/: $/, "") : first;
  return {
    title: given.length > 0 ? given : said,
    language,
    error_type: errorType,
    message,
    frames,
    versions: {},
    culprit,
    text: text.trim(),
  };
}

const TRACE = `Traceback (most recent call last):
  File "/app/backend/app/api/routes/items.py", line 34, in read_items
    count = crud.count_items(session=session, owner_id=current_user.id)
  File "/app/backend/app/crud.py", line 61, in count_items
    return total // per_page
ZeroDivisionError: integer division or modulo by zero
`;

function made(
  repo: MockRepo,
  id: string,
  fields: Partial<Fix> & Pick<Fix, "title" | "status" | "stage" | "source">,
): Fix {
  const ticket = mockParse(TRACE, fields.title);
  return {
    id,
    kind: "fix",
    repo: repo.slug,
    source_ref: null,
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
    ...fields,
  };
}

function seed(repo: MockRepo): Fix[] {
  const id = (n: number) => `0199f000-0000-7000-8000-00000000f${String(n).padStart(3, "0")}`;
  return [
    made(repo, id(1), {
      title: "Items page crashes when an account has no items",
      status: "awaiting_approval",
      stage: "awaiting_approval",
      source: "github_issue",
      source_ref: "https://github.com/acme/api/issues/42",
      risk: "low",
      repro_result: "reproduced",
      approvals_needed: 1,
      created_at: "2026-10-18T11:20:00Z",
    }),
    made(repo, id(2), {
      title: "ZeroDivisionError: integer division or modulo by zero",
      status: "needs_triage",
      stage: "intake",
      source: "sentry",
      source_ref: "4815162342",
      created_by: null,
      author: null,
      created_at: "2026-10-18T10:05:00Z",
    }),
    made(repo, id(3), {
      title: "Login redirects to a blank page after the session expires",
      status: "not_reproduced",
      stage: "reproduce",
      source: "paste",
      repro_result: "not_reproduced",
      stop_reason: "No reproducing test: it needs a real browser to expire the cookie.",
      created_by: HUBOT,
      author: "hubot",
      created_at: "2026-10-17T16:40:00Z",
      finished_at: "2026-10-17T16:52:00Z",
    }),
    made(repo, id(4), {
      title: "Invoice totals are a cent short with a discount",
      status: "succeeded",
      stage: "done",
      source: "github_issue",
      source_ref: "https://github.com/acme/api/issues/37",
      risk: "high",
      merge_readiness: 3,
      repro_result: "reproduced",
      pr_url: "https://github.com/acme/api/pull/51",
      created_at: "2026-10-16T09:10:00Z",
      finished_at: "2026-10-16T09:41:00Z",
    }),
  ];
}

function fixesOf(repo: MockRepo): Fix[] {
  db.fixes ??= {};
  db.fixes[repo.id] ??= seed(repo);
  return db.fixes[repo.id] ?? [];
}

function summary(fix: Fix): FixSummary {
  const {
    ticket: _ticket,
    repro_result: _r,
    root_cause: _c,
    repro: _p,
    plan: _l,
    patch: _a,
    verification: _v,
    stop_reason: _s,
    approvals_needed: _n,
    ...rest
  } = fix;
  return rest;
}

/** The fix ``id`` in one of the org's repositories (or the 404 the API sends). */
function found(org: string, id: string): Fix | Response {
  const access = orgAccess(org);
  if (access instanceof Response) return access;
  for (const repo of reposOf(org)) {
    const fix = fixesOf(repo).find((f) => f.id === id);
    if (fix) return fix;
  }
  return apiError(404, "fix_not_found", "No such fix.");
}

function member(org: string): Response | { login: string; id: string } {
  const access = orgAccess(org);
  if (access instanceof Response) return access;
  const role = roleIn(org, access.user.id);
  if (!atLeast(role, "member")) return insufficientRole("member", role ?? "none");
  return { login: access.user.login, id: access.user.id };
}

export const fixHandlers = [
  http.post<Params>("*/api/v1/orgs/:org/repos/:repo/fixes/parse", async ({ params, request }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const body = (await request.json()) as { text?: string };
    return HttpResponse.json(mockParse(body.text ?? ""));
  }),

  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/fixes", ({ params, request }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const status = new URL(request.url).searchParams.get("status");
    const fixes = fixesOf(repo)
      .filter((fix) => !status || fix.status === status)
      .sort((a, b) => b.created_at.localeCompare(a.created_at));
    return HttpResponse.json(fixes.map(summary));
  }),

  http.post<Params>("*/api/v1/orgs/:org/repos/:repo/fixes", async ({ params, request }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const who = member(params.org);
    if (who instanceof Response) return who;
    const body = (await request.json()) as {
      source: "paste" | "github_issue";
      text?: string;
      title?: string;
      issue?: string;
    };
    const fixes = fixesOf(repo);
    let ticket: Ticket;
    let ref: string | null = null;
    if (body.source === "github_issue") {
      const number = /(\d+)\/?$/.exec((body.issue ?? "").trim())?.[1];
      if (!number) return apiError(422, "bad_issue", "Give the issue's number or URL.");
      if (number === "404") return apiError(404, "issue_not_found", "GitHub has no issue #404.");
      ref = `https://github.com/${repo.full_name}/issues/${number}`;
      const going = fixes.find((f) => f.source_ref === ref && !f.finished_at);
      if (going) return HttpResponse.json(going, { status: 200 });
      ticket = { ...mockParse(TRACE), title: `Issue #${number}: the items count crashes` };
    } else {
      ticket = mockParse(body.text ?? "", body.title);
    }
    const fix: Fix = {
      ...made(repo, crypto.randomUUID(), {
        title: ticket.title,
        status: "queued",
        stage: "intake",
        source: body.source,
        source_ref: ref,
      }),
      ticket,
      created_by: who.id,
      author: who.login,
      created_at: new Date().toISOString(),
    };
    fixes.push(fix);
    saveDb();
    return HttpResponse.json(fix, { status: 201 });
  }),

  http.get<Params>("*/api/v1/orgs/:org/fixes/:id", ({ params }) => {
    const fix = found(params.org, params.id);
    return fix instanceof Response ? fix : HttpResponse.json(fix);
  }),

  http.post<Params>("*/api/v1/orgs/:org/fixes/:id/start", ({ params }) => {
    const who = member(params.org);
    if (who instanceof Response) return who;
    const fix = found(params.org, params.id);
    if (fix instanceof Response) return fix;
    if (fix.status !== "needs_triage") {
      return apiError(409, "fix_not_waiting", `The fix is ${fix.status}, not waiting for triage.`);
    }
    fix.status = "queued";
    saveDb();
    return HttpResponse.json(fix);
  }),

  http.get<Params>("*/api/v1/orgs/:org/integrations/sentry", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const role = roleIn(params.org, access.user.id);
    if (!atLeast(role, "admin")) return insufficientRole("admin", role ?? "none");
    return HttpResponse.json({
      sentry_org: "acme",
      api_url: "https://sentry.io",
      projects: { "acme-api": "api" },
      auto_run: true,
      min_events: 10,
      webhook_url: `https://sensei.example/api/v1/webhooks/sentry/${params.org}`,
    });
  }),
];
