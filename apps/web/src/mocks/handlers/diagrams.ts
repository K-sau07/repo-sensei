/**
 * The mock diagrams (6.1, 6.6): drawn from the mock snapshot's Login trace (its path's hops,
 * each edge the trace's: verified), plus, for a question, one edge the graph can't show, marked
 * inferred with its reason, as the real validator would keep it. A question with "nothing" in it
 * finds nothing (`404 diagram_nothing_found`). The whole app's architecture and schema are the
 * template's (app-architecture.ts), as is an ER diagram asked of a question. Kept in the mock
 * database per repository.
 */
import { http, HttpResponse } from "msw";

import type { Diagram, DiagramSummary, Spec } from "@/lib/api/diagrams";
import { mermaid, ratio, specOfTrace, TEMPLATE_MEMBERS } from "@/lib/diagrams/from-trace";
import { appArchitecture, appSchema } from "@/mocks/app-architecture";
import { db, saveDb, type MockRepo } from "@/mocks/db";
import { LOGIN_ENTRY, mockTraces } from "@/mocks/trace";

import { apiError } from "./errors";
import { repoOf } from "./repos";

interface Params {
  org: string;
  repo: string;
  id?: string;
}

const SHARED = new Map<string, { repoId: string; id: string }>();

function diagramsOf(repo: MockRepo): Diagram[] {
  db.diagrams ??= {};
  db.diagrams[repo.id] ??= [];
  return db.diagrams[repo.id] ?? [];
}

function summary(d: Diagram): DiagramSummary {
  const { prompt: _prompt, spec: _spec, mermaid: _mermaid, ...rest } = d;
  return rest;
}

export const diagramHandlers = [
  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/diagrams", ({ params }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const listed = [...diagramsOf(repo)].reverse().map(summary);
    return HttpResponse.json({ diagrams: listed });
  }),

  http.post<Params>("*/api/v1/orgs/:org/repos/:repo/diagrams", async ({ params, request }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const body = (await request.json()) as {
      question?: string | null;
      from_trace?: { from: string } | null;
      whole_app?: boolean;
      whole_schema?: boolean;
      kind?: Spec["kind"];
    };
    const given = [
      Boolean(body.question),
      Boolean(body.from_trace),
      Boolean(body.whole_app),
      Boolean(body.whole_schema),
    ];
    if (given.filter(Boolean).length !== 1) {
      return apiError(
        422,
        "diagram_source",
        "Give one of from_trace, a question, whole_app or whole_schema.",
      );
    }
    const schema = body.whole_schema === true || (body.kind === "er" && !body.whole_app);
    if (body.whole_app || schema) {
      const spec = body.whole_app
        ? appArchitecture(`Architecture of ${repo.full_name}`)
        : appSchema(body.question ? `Tables of ${body.question}` : `Schema of ${repo.full_name}`);
      const made: Diagram = {
        id: crypto.randomUUID(),
        title: spec.title,
        kind: spec.kind,
        source: body.question ? "question" : "repo",
        commit_sha: repo.last_indexed_sha ?? "",
        verified_ratio: ratio(spec),
        nodes: spec.nodes.length,
        edges: spec.edges.length,
        created_by: null,
        shared: false,
        created_at: new Date().toISOString(),
        prompt: body.whole_app
          ? { whole_app: true }
          : body.question
            ? { question: body.question }
            : { whole_schema: true },
        spec: spec as unknown as Diagram["spec"],
        mermaid: mermaid(spec),
      };
      diagramsOf(repo).push(made);
      saveDb();
      return HttpResponse.json(made, { status: 201 });
    }
    if (body.question?.toLowerCase().includes("nothing")) {
      return apiError(
        404,
        "diagram_nothing_found",
        "Nothing in the code graph matches the question.",
      );
    }
    const traces = await mockTraces();
    const entry = body.from_trace?.from ?? LOGIN_ENTRY;
    const trace = traces.find(entry, true) ?? traces.find(LOGIN_ENTRY, true);
    if (!trace)
      return apiError(404, "trace_entry_not_found", "Nothing in the graph is that entry.");
    const kind = body.kind ?? "flowchart";
    const title = body.question ?? `The flow from ${trace.entries[0]?.name ?? entry}`;
    const spec = specOfTrace(trace, title, kind, Boolean(body.question), TEMPLATE_MEMBERS);
    const made: Diagram = {
      id: crypto.randomUUID(),
      title,
      kind,
      source: body.question ? "question" : "trace",
      commit_sha: repo.last_indexed_sha ?? "",
      verified_ratio: ratio(spec),
      nodes: spec.nodes.length,
      edges: spec.edges.length,
      created_by: null,
      shared: false,
      created_at: new Date().toISOString(),
      prompt: body.question ? { question: body.question } : { from_trace: body.from_trace },
      spec: spec as unknown as Diagram["spec"],
      mermaid: mermaid(spec),
    };
    diagramsOf(repo).push(made);
    saveDb();
    return HttpResponse.json(made, { status: 201 });
  }),

  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/diagrams/:id", ({ params }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const found = diagramsOf(repo).find((d) => d.id === params.id);
    return found
      ? HttpResponse.json(found)
      : apiError(404, "diagram_not_found", "No such diagram.");
  }),

  http.patch<Params>("*/api/v1/orgs/:org/repos/:repo/diagrams/:id", async ({ params, request }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const found = diagramsOf(repo).find((d) => d.id === params.id);
    if (!found) return apiError(404, "diagram_not_found", "No such diagram.");
    const { title } = (await request.json()) as { title: string };
    found.title = title.split(/\s+/).filter(Boolean).join(" ");
    found.spec = { ...found.spec, title: found.title };
    saveDb();
    return HttpResponse.json(found);
  }),

  http.delete<Params>("*/api/v1/orgs/:org/repos/:repo/diagrams/:id", ({ params }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const all = diagramsOf(repo);
    const at = all.findIndex((d) => d.id === params.id);
    if (at < 0) return apiError(404, "diagram_not_found", "No such diagram.");
    all.splice(at, 1);
    saveDb();
    return new HttpResponse(null, { status: 204 });
  }),

  http.post<Params>("*/api/v1/orgs/:org/repos/:repo/diagrams/:id/share", ({ params }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const found = diagramsOf(repo).find((d) => d.id === params.id);
    if (!found) return apiError(404, "diagram_not_found", "No such diagram.");
    const token = crypto.randomUUID().replaceAll("-", "");
    SHARED.set(token, { repoId: repo.id, id: found.id });
    found.shared = true;
    saveDb();
    return HttpResponse.json({ token, path: `/d/${token}` });
  }),

  http.get<{ token: string }>("*/api/v1/d/:token", ({ params }) => {
    const held = SHARED.get(params.token);
    const found = held ? db.diagrams?.[held.repoId]?.find((d) => d.id === held.id) : undefined;
    if (!found) return apiError(404, "share_not_found", "This link doesn't open anything.");
    return HttpResponse.json({ repo: "acme-gh/api", redacted: false, diagram: found });
  }),
];
