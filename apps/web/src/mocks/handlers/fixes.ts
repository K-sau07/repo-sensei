/**
 * The mock fixes (8.11): a repository's fixes (seeded with one in each kind of state), a paste
 * parsed as the API would (Python tracebacks and JavaScript stacks, their frames mapped to the
 * mock snapshot's files by the longest matching end of the path), starting a fix from a paste
 * or a GitHub issue (#404 is one GitHub doesn't have), and starting one Sentry left for triage.
 * Kept in the mock database per repository.
 */
import { delay, http, HttpResponse } from "msw";

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
    run_id: null,
    ...fields,
  };
}

const ROOT_CAUSE = {
  claim: "count_items divides by per_page, which is 0 for an account with no items.",
  path: "backend/app/crud.py", start_line: 58, end_line: 61, confidence: "high",
  suspect_commit: "9f1c2d3e4b5a69788f7e6d5c4b3a291807f6e5d4", seen_in_repro: true,
  hypotheses: [
    { claim: "per_page is 0 when the account has no items", likelihood: 0.8,
      evidence: [{ path: "backend/app/crud.py", start_line: 58, end_line: 61 },
                 { path: "backend/app/api/routes/items.py", start_line: 30, end_line: 36 }] },
    { claim: "the route passes a stale count", likelihood: 0.15,
      evidence: [{ path: "backend/app/api/routes/items.py", start_line: 30, end_line: 36 }] },
  ],
  blast_radius: {
    changed: ["py backend/app/crud.py/count_items"],
    counts: { routes: 2, screens: 1, tests: 2, tables: 1, symbols: 3 },
    top: { routes: [{ key: "r1", name: "GET /api/v1/items/", path: "backend/app/api/routes/items.py",
                      score: 1, distance: 1 }] },
    truncated: false,
  },
  areas: {},
}; // prettier-ignore
const FAILING =
  "backend/app/crud.py:61: ZeroDivisionError\nE   ZeroDivisionError: integer division or modulo by zero\n1 failed in 0.12s";
const PASSING = "1 passed in 0.10s";
const REPRO = {
  framework: "pytest", project: "backend", test_path: "backend/app/tests/test_sensei_repro_f001.py",
  test: "def test_no_items(client, normal_user_token_headers):\n    r = client.get('/api/v1/items/', headers=normal_user_token_headers)\n    assert r.status_code == 200\n",
  explanation: "An account with no items gets a 500.", tries: [],
  runs: [1, 2, 3].map(() => ({ passed: false, exit_code: 1, for_reason: true, output: FAILING })),
}; // prettier-ignore

function planOf(summary: string, risk: "low" | "high") {
  return {
    summary, revision: 1,
    steps: [{ description: "Return 0 pages when per_page is 0", path: "backend/app/crud.py" }],
    files: [{ path: "backend/app/crud.py", change: "modify", why: "where the division is" }],
    risks: ["Callers that expected an error for an empty account."],
    risk: { level: risk, reasons: risk === "high" ? ["touches payments: backend/app/payments/refunds.py is payments code"] : ["1 file(s), a small change, no sensitive area"] },
  }; // prettier-ignore
}

const DIFF = `diff --git a/backend/app/crud.py b/backend/app/crud.py
index 1111111..2222222 100644
--- a/backend/app/crud.py
+++ b/backend/app/crud.py
@@ -58,4 +58,6 @@ def count_items(*, session, owner_id, per_page):
     total = session.exec(select(func.count()).where(Item.owner_id == owner_id)).one()
+    if not per_page:
+        return 0
     return total // per_page
diff --git a/backend/app/tests/test_sensei_repro_f001.py b/backend/app/tests/test_sensei_repro_f001.py
new file mode 100644
--- /dev/null
+++ b/backend/app/tests/test_sensei_repro_f001.py
@@ -0,0 +1,3 @@
+def test_no_items(client, normal_user_token_headers):
+    r = client.get('/api/v1/items/', headers=normal_user_token_headers)
+    assert r.status_code == 200
`;

function seed(repo: MockRepo): Fix[] {
  const id = (n: number) => `0199f000-0000-7000-8000-00000000f${String(n).padStart(3, "0")}`;
  return [
    made(repo, id(5), {
      title: "Refunds are applied twice when the request is retried",
      status: "awaiting_approval",
      stage: "awaiting_approval",
      source: "paste",
      risk: "high",
      repro_result: "reproduced",
      approvals_needed: 2,
      root_cause: {
        ...ROOT_CAUSE,
        claim: "The refund isn't keyed by the request id: a retry refunds again.",
        path: "backend/app/payments/refunds.py",
        start_line: 12,
        end_line: 20,
      },
      repro: REPRO,
      plan: planOf("Key refunds by the request id, so a retry finds the first.", "high"),
      run_id: "0199f000-0000-7000-8000-0000000000a5",
      created_at: "2026-10-18T11:50:00Z",
    }),
    made(repo, id(1), {
      title: "Items page crashes when an account has no items",
      status: "awaiting_approval",
      stage: "awaiting_approval",
      source: "github_issue",
      source_ref: "https://github.com/acme/api/issues/42",
      risk: "low",
      repro_result: "reproduced",
      approvals_needed: 1,
      root_cause: ROOT_CAUSE,
      repro: REPRO,
      plan: planOf("Return 0 pages for an account with no items instead of dividing by 0.", "low"),
      run_id: "0199f000-0000-7000-8000-0000000000a1",
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
      repro: {
        ...REPRO,
        explanation: "It needs a real browser to expire the cookie.",
        tries: [{ passed: true, exit_code: 0, output: "1 passed in 0.31s" }],
        runs: [],
      },
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
    made(repo, id(6), {
      title: "Search returns nothing for an empty query",
      status: "blocked",
      stage: "patch",
      source: "paste",
      risk: "low",
      repro_result: "reproduced",
      repro: REPRO,
      plan: planOf("Return every item for an empty query.", "low"),
      patch: {
        findings: [
          {
            rule: "broad_except",
            path: "backend/app/api/routes/items.py",
            detail: "except Exception:",
          },
        ],
        attempts: [],
      },
      stop_reason: "Anti-gaming: broad_except (backend/app/api/routes/items.py: except Exception:)",
      created_at: "2026-10-15T14:00:00Z",
      finished_at: "2026-10-15T14:20:00Z",
    }),
  ];
}

const PLAN_MS = 1_200;
const BUILD_MS = 2_400;

/** A fix moved on by the time passed since a person decided on it. */
function now(fix: Fix): Fix {
  db.fixPhases ??= {};
  const phase = db.fixPhases[fix.id];
  if (!phase) return fix;
  const passed = Date.now() - phase.since;
  const plan = fix.plan as {
    revision: number;
    steps: unknown[];
    requests?: { edits?: { steps?: unknown[] } | null }[];
  } | null;
  if (phase.phase === "replanning") {
    if (passed >= PLAN_MS && plan) {
      const edits = plan.requests?.at(-1)?.edits;
      fix.plan = { ...plan, revision: plan.revision + 1, steps: edits?.steps ?? plan.steps };
      Object.assign(fix, { status: "awaiting_approval", stage: "awaiting_approval" });
      Reflect.deleteProperty(db.fixPhases, fix.id);
      saveDb();
    } else {
      Object.assign(fix, { status: "running", stage: "plan" });
    }
  } else if (passed >= BUILD_MS) {
    Object.assign(fix, {
      status: "succeeded", stage: "done", merge_readiness: 4, pr_url: "https://github.com/acme/api/pull/60",
      finished_at: new Date().toISOString(),
      patch: { diff: DIFF, files: ["backend/app/crud.py"], lines: 2, summary: "Return 0 for no items.", attempts: [], findings: [] },
      verification: {
        repro_runs: [1, 2, 3].map(() => ({ passed: true, exit_code: 0, output: PASSING })),
        suite: { ok: true, verdict: "The whole suite passes." },
        verdict: { test_shaped: false, reason: "It guards the division at its cause.", concerns: [] },
        readiness: { score: 4, reason: "No extra tests on the changed lines." },
        unverified: ["Accounts created before the per_page setting existed."],
        findings: [], risk: fix.plan ? (fix.plan as { risk: unknown }).risk : null,
      },
    }); // prettier-ignore
    Reflect.deleteProperty(db.fixPhases, fix.id);
    saveDb();
  } else {
    Object.assign(fix, { status: "running", stage: passed >= BUILD_MS / 2 ? "verify" : "patch" });
  }
  return fix;
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
    if (fix) return now(fix);
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
      .map(now)
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

  http.get<Params>("*/api/v1/orgs/:org/fixes/:id/approvals", ({ params }) => {
    const fix = found(params.org, params.id);
    if (fix instanceof Response) return fix;
    db.fixApprovals ??= {};
    return HttpResponse.json(db.fixApprovals[fix.id] ?? []);
  }),

  http.post<Params>("*/api/v1/orgs/:org/fixes/:id/approval", async ({ params, request }) => {
    const who = member(params.org);
    if (who instanceof Response) return who;
    const fix = found(params.org, params.id);
    if (fix instanceof Response) return fix;
    const body = (await request.json()) as {
      decision: "approve" | "request_changes" | "reject";
      plan_revision: number;
      comment?: string | null;
      plan_edits?: { steps?: unknown[] } | null;
    };
    const plan = fix.plan as { revision: number; requests?: unknown[] } | null;
    if (fix.status !== "awaiting_approval")
      return apiError(409, "fix_not_awaiting_approval", "The fix isn't waiting for approval.");
    if (plan?.revision !== body.plan_revision)
      return apiError(409, "fix_plan_changed", "The plan changed: read it again.");
    db.fixApprovals ??= {};
    db.fixPhases ??= {};
    const decisions = (db.fixApprovals[fix.id] ??= []);
    const approved = decisions.filter(
      (a) => a.decision === "approve" && a.plan_revision === plan.revision,
    );
    if (body.decision === "approve" && approved.some((a) => a.user_id === who.id))
      return apiError(409, "fix_already_approved", "You have approved this plan already.");
    decisions.push({
      id: crypto.randomUUID(), user_id: who.id, login: who.login, decision: body.decision,
      comment: body.comment ?? null, plan_edits: body.plan_edits ?? null,
      plan_revision: plan.revision, created_at: new Date().toISOString(),
    }); // prettier-ignore
    if (body.decision === "reject") {
      const why = body.comment ? `: ${body.comment}` : ".";
      Object.assign(fix, { status: "rejected", stop_reason: `Rejected by @${who.login}${why}`,
                           finished_at: new Date().toISOString() }); // prettier-ignore
    } else if (body.decision === "request_changes") {
      const asked = {
        by: who.login,
        comment: body.comment ?? null,
        edits: body.plan_edits ?? null,
      };
      fix.plan = { ...plan, requests: [...(plan.requests ?? []), asked] };
      Object.assign(fix, { status: "queued", stage: "plan" });
      db.fixPhases[fix.id] = { phase: "replanning", since: Date.now() };
    } else if (approved.length + 1 >= (fix.approvals_needed ?? 1)) {
      Object.assign(fix, { status: "queued", stage: "patch" });
      db.fixPhases[fix.id] = { phase: "building", since: Date.now() };
    }
    saveDb();
    return HttpResponse.json(fix);
  }),

  http.post<Params>("*/api/v1/orgs/:org/fixes/:id/retry", async ({ params, request }) => {
    const who = member(params.org);
    if (who instanceof Response) return who;
    const fix = found(params.org, params.id);
    if (fix instanceof Response) return fix;
    if (fix.status !== "not_reproduced")
      return apiError(409, "fix_not_retryable", `The fix is ${fix.status}, not retryable.`);
    const body = (await request.json()) as { context: string };
    Object.assign(fix, {
      status: "queued", stage: "investigate", stop_reason: null, finished_at: null, repro: null,
      repro_result: null, root_cause: null,
      ticket: { ...fix.ticket, text: `${fix.ticket.text}\n\nFrom @${who.login}: ${body.context}` },
    }); // prettier-ignore
    saveDb();
    return HttpResponse.json(fix);
  }),

  http.post<Params>("*/api/v1/orgs/:org/fixes/:id/cancel", ({ params }) => {
    const who = member(params.org);
    if (who instanceof Response) return who;
    const fix = found(params.org, params.id);
    if (fix instanceof Response) return fix;
    Object.assign(fix, { status: "cancelled", stop_reason: `Cancelled by @${who.login}.` });
    saveDb();
    return HttpResponse.json(fix);
  }),

  http.get<Params>("*/api/v1/orgs/:org/fixes/:id/events", ({ params, request }) => {
    const fix = found(params.org, params.id);
    if (fix instanceof Response) return fix;
    const encoder = new TextEncoder();
    // A reconnect says where it was (the header, or the query the app's streams reopen with), as
    // the API reads it: nothing is sent twice.
    const again =
      request.headers.get("last-event-id") !== null ||
      new URL(request.url).searchParams.has("last_event_id");
    const steps = [
      { name: "turn 1", summary: "read_code, grep" },
      { name: "edit_file", summary: "edited backend/app/crud.py" },
      { name: "attempt 1 of 4", summary: "passed: The whole suite passes." },
    ];
    const stream = new ReadableStream({
      async start(controller) {
        if (!again) {
          steps.forEach((step, i) => {
            const data = { type: "step", fix_id: fix.id, ...step, ts: new Date().toISOString() };
            const text = `id: ${String(i + 1)}\nevent: step\ndata: ${JSON.stringify(data)}\n\n`;
            controller.enqueue(encoder.encode(text));
          });
        }
        await delay("infinite"); // open, as the API's is while the fix goes on
      },
    });
    return new HttpResponse(stream, { headers: { "content-type": "text/event-stream" } });
  }),

  http.get<Params>("*/api/v1/orgs/:org/integrations/sentry", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const role = roleIn(params.org, access.user.id);
    if (!atLeast(role, "admin")) return insufficientRole("admin", role ?? "none");
    const found = sentryOf(params.org);
    if (found === null) return apiError(404, "sentry_not_connected", "Sentry isn't connected.");
    return HttpResponse.json(found);
  }),

  http.put<Params>("*/api/v1/orgs/:org/integrations/sentry", async ({ params, request }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const role = roleIn(params.org, access.user.id);
    if (!atLeast(role, "admin")) return insufficientRole("admin", role ?? "none");
    const body = (await request.json()) as {
      sentry_org: string;
      api_url?: string;
      token?: string | null;
      client_secret?: string | null;
      projects?: Record<string, string>;
      auto_run: boolean;
      min_events: number;
    };
    if (sentryOf(params.org) === null && (!body.token || !body.client_secret)) {
      return apiError(
        422,
        "sentry_secrets_needed",
        "Connecting Sentry needs its token and client secret.",
      );
    }
    db.sentry ??= {};
    db.sentry[params.org] = {
      sentry_org: body.sentry_org,
      api_url: (body.api_url ?? "https://sentry.io").replace(/\/$/, ""),
      projects: body.projects ?? {},
      auto_run: body.auto_run,
      min_events: body.min_events,
      webhook_url: `https://sensei.example/api/v1/webhooks/sentry/${params.org}`,
    };
    saveDb();
    return HttpResponse.json(db.sentry[params.org]);
  }),

  http.delete<Params>("*/api/v1/orgs/:org/integrations/sentry", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const role = roleIn(params.org, access.user.id);
    if (!atLeast(role, "admin")) return insufficientRole("admin", role ?? "none");
    db.sentry ??= {};
    db.sentry[params.org] = null;
    saveDb();
    return new HttpResponse(null, { status: 204 });
  }),
];

/** The org's Sentry: the seed's for "acme" until it is changed (null once disconnected). */
function sentryOf(org: string) {
  const kept = db.sentry?.[org];
  if (kept !== undefined) return kept;
  return org === "acme"
    ? {
        sentry_org: "acme",
        api_url: "https://sentry.io",
        projects: { "acme-api": "api" },
        auto_run: true,
        min_events: 10,
        webhook_url: `https://sensei.example/api/v1/webhooks/sentry/${org}`,
      }
    : null;
}
