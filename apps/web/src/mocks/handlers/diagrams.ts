/**
 * The mock diagrams (6.1, 6.6): drawn from the mock snapshot's Login trace (its path's hops,
 * each edge the trace's: verified), plus, for a question, one edge the graph can't show, marked
 * inferred with its reason, as the real validator would keep it. A question with "nothing" in it
 * finds nothing (`404 diagram_nothing_found`). Kept in the mock database per repository.
 */
import { http, HttpResponse } from "msw";

import type { Diagram, DiagramSummary, Spec, SpecEdge, SpecNode } from "@/lib/api/diagrams";
import type { Trace } from "@/lib/api/trace";
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

/** A flowchart's Mermaid, as the API compiles it. */
function mermaid(spec: Spec): string {
  const ids = new Map(spec.nodes.map((node, i) => [node.id, `n${String(i + 1)}`]));
  const label = (text: string) => text.replace(/["`<>{}[\]|#;]/g, " ").trim() || "?";
  const lines = [spec.kind === "sequence" ? "sequenceDiagram" : "flowchart LR"];
  if (spec.kind === "sequence") {
    for (const node of spec.nodes)
      lines.push(`  participant ${ids.get(node.id) ?? "?"} as ${label(node.label)}`);
    for (const edge of spec.edges) {
      const arrow = edge.verified ? "->>" : "-->>";
      lines.push(
        `  ${ids.get(edge.from) ?? "?"}${arrow}${ids.get(edge.to) ?? "?"}: ${label(edge.label ?? edge.type ?? "")}`,
      );
    }
    return `${lines.join("\n")}\n`;
  }
  for (const node of spec.nodes) lines.push(`  ${ids.get(node.id) ?? "?"}["${label(node.label)}"]`);
  for (const edge of spec.edges) {
    const arrow = edge.verified ? "-->" : "-.->";
    const text = edge.label ? `|${label(edge.label)}|` : "";
    lines.push(`  ${ids.get(edge.from) ?? "?"} ${arrow}${text} ${ids.get(edge.to) ?? "?"}`);
  }
  return `${lines.join("\n")}\n`;
}

/** The spec of a trace's first path: its hops (each once) and the edges along it. */
function specOfTrace(trace: Trace, title: string, kind: Spec["kind"], inferred: boolean): Spec {
  const path = trace.paths[0];
  const nodes: SpecNode[] = [];
  const seen = new Set<string>();
  const edges: SpecEdge[] = [];
  let previous: string | null = null;
  for (const hop of path?.hops ?? []) {
    const key = hop.node.symbol_key;
    if (!seen.has(key)) {
      seen.add(key);
      nodes.push({
        id: key,
        label: hop.node.name,
        layer: hop.layer,
        kind: hop.node.kind,
        citation:
          hop.node.path && hop.node.lines
            ? { path: hop.node.path, start_line: hop.node.lines[0], end_line: hop.node.lines[1] }
            : null,
      });
    }
    if (previous !== null && hop.edge && previous !== key) {
      edges.push({
        from: previous,
        to: key,
        type: hop.edge.type,
        label: hop.edge.type.replaceAll("_", " "),
        verified: true,
        confidence: hop.edge.confidence,
        provenance: hop.edge.provenance,
        inferred: null,
      });
    }
    previous = key;
  }
  const first = nodes[0];
  const last = nodes.at(-1);
  if (inferred && first && last && first.id !== last.id) {
    edges.push({
      from: last.id,
      to: first.id,
      type: null,
      label: "signs the user in",
      verified: false,
      confidence: null,
      provenance: null,
      inferred: { reason: "the session is set by middleware the graph can't see" },
    });
  }
  return { title, kind, nodes, edges };
}

function ratio(spec: Spec): number {
  return spec.edges.length ? spec.edges.filter((e) => e.verified).length / spec.edges.length : 1;
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
      kind?: Spec["kind"];
    };
    if (!body.question === !body.from_trace) {
      return apiError(422, "diagram_source", "Give from_trace or a question, not both.");
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
    const spec = specOfTrace(trace, title, kind, Boolean(body.question));
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
