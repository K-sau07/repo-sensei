/**
 * The mock `GET …/impact?symbol=` (or `files=`, repeated) over the mock snapshot's graph
 * (mocks/impact.ts walks it as the engine does), with the API's checks in its order: a symbol or
 * files, not both (422 `impact_change`); the snapshot and its graph (404, 409 `graph_not_ready`);
 * then the change (404 `impact_not_found`). A symbol is its key, else a name: each definition
 * called that.
 */
import { http, HttpResponse } from "msw";

import type { Affected, Impact, ImpactNode } from "@/lib/api/impact";
import { mockGraph, type FixtureNode, type MockGraph } from "@/mocks/graph";
import { CATEGORIES, changedNodes, impactOf, MIN_CONF, type MockAffected } from "@/mocks/impact";

import { apiError } from "./errors";
import { graphReady } from "./graph";

interface Params {
  org: string;
  repo: string;
}

const DEFINITIONS: ReadonlySet<string> = new Set([
  "class",
  "function",
  "method",
  "component",
  "hook",
  "variable",
  "orm_model",
  "table",
  "api_endpoint",
]);

let screens: Promise<ReadonlySet<string>> | null = null;

function pages(): Promise<ReadonlySet<string>> {
  screens ??= import("../fixtures/impact.json").then((m) => new Set(m.default.screens));
  return screens;
}

function nodeOut(node: FixtureNode): ImpactNode {
  return {
    key: node.symbol_key,
    name: node.qualified_name || node.name,
    kind: node.kind,
    path: node.path,
    start_line: node.start_line,
    end_line: node.end_line,
  };
}

function affectedOut(item: MockAffected): Affected {
  return {
    ...nodeOut(item.node),
    score: Math.round(item.score * 10_000) / 10_000,
    distance: item.distance,
    via: item.via,
    edge: item.edge,
  };
}

/** A symbol's key, else the definitions with that name (or qualified name). */
function symbolKeys(graph: MockGraph, all: readonly FixtureNode[], symbol: string): string[] {
  if (graph.node(symbol)) return [symbol];
  return all
    .filter((n) => DEFINITIONS.has(n.kind) && (n.name === symbol || n.qualified_name === symbol))
    .map((n) => n.symbol_key);
}

let everything: Promise<FixtureNode[]> | null = null;

function allNodes(): Promise<FixtureNode[]> {
  everything ??= import("../fixtures/graph.json").then(
    (m) => (m.default as { nodes: FixtureNode[] }).nodes,
  );
  return everything;
}

export const impactHandlers = [
  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/impact", async ({ params, request }) => {
    const url = new URL(request.url);
    const symbol = url.searchParams.get("symbol");
    const files = url.searchParams.getAll("files");
    if ((symbol === null) === (files.length === 0)) {
      return apiError(422, "impact_change", "Give a symbol or files, not both.");
    }
    const found = graphReady(params, request);
    if (found instanceof Response) return found;
    const graph = await mockGraph();
    const symbols = symbol !== null ? symbolKeys(graph, await allNodes(), symbol) : [];
    const changed = changedNodes(graph, { symbols, files });
    if (changed.length === 0) {
      return apiError(404, "impact_not_found", "Nothing in the code graph matches the change.");
    }
    const minConf = Number(url.searchParams.get("min_conf") ?? String(MIN_CONF));
    const result = impactOf(graph, await pages(), changed, minConf);
    const affected = Object.fromEntries(
      CATEGORIES.map((c) => [c, result.affected[c].map(affectedOut)]),
    ) as Impact["affected"];
    return HttpResponse.json<Impact>(
      {
        sha: found.commit,
        changed: result.changed.map(nodeOut),
        affected,
        heat: result.heat,
        truncated: result.truncated,
      },
      { headers: { "X-Commit-Sha": found.commit } },
    );
  }),
];
