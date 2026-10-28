/** The mock graph endpoints (`…/graph/neighbors`, `…/graph/path`) over the mock snapshot's graph
 * (mocks/graph.ts), with the API's checks in its order: the query's fields and edge types, the
 * snapshot (404 before any index), whether its graph is built (409 `graph_not_ready` during the
 * first run), then the keys (404 `symbol_not_found`). */
import { http, HttpResponse } from "msw";

import type { EdgeType, GraphNeighbors, GraphPath } from "@/lib/api/graph";
import { REFERENCE_TYPES } from "@/lib/api/graph";
import { mockGraph, neighbors, nodeOut, surestPath } from "@/mocks/graph";

import { target, validationFailed } from "./browse";
import { apiError } from "./errors";

interface Params {
  org: string;
  repo: string;
}

const MAX_DEPTH = 3;
const MAX_LIMIT = 500;
const KNOWN: ReadonlySet<string> = new Set<EdgeType>([...REFERENCE_TYPES, "contains", "defines"]);

/** `types` (comma-separated; none: every type), or the 422 for an unknown one. */
function typesOf(raw: string | null): ReadonlySet<EdgeType> | null | Response {
  const names = (raw ?? "")
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
  if (names.length === 0) return null;
  const unknown = names.filter((name) => !KNOWN.has(name));
  if (unknown.length > 0) {
    return apiError(
      422,
      "invalid_types",
      `Unknown edge types: ${unknown.join(", ")} (known: ${[...KNOWN].join(", ")}).`,
    );
  }
  return new Set(names as EdgeType[]);
}

function number(raw: string | null, fallback: number): number {
  return raw === null ? fallback : Number(raw);
}

/** The repository's graph, or why there's none to read. */
async function graphFor(params: Params, request: Request) {
  const found = target(params, request);
  if (found instanceof Response) return found;
  if (!found.repo.last_indexed_sha) {
    return apiError(
      409,
      "graph_not_ready",
      "The code graph appears once this commit's indexing has linked its files.",
    );
  }
  return { ...found, graph: await mockGraph() };
}

const notFound = () => apiError(404, "symbol_not_found", "No such symbol in this commit's graph.");

export const graphHandlers = [
  http.get<Params>(
    "*/api/v1/orgs/:org/repos/:repo/graph/neighbors",
    async ({ params, request }) => {
      const search = new URL(request.url).searchParams;
      const symbol = search.get("symbol");
      if (!symbol) return validationFailed("symbol", "Field required");
      const direction = search.get("direction") ?? "out";
      if (direction !== "out" && direction !== "in" && direction !== "both") {
        return validationFailed("direction", "Input should be 'out', 'in' or 'both'");
      }
      const depth = number(search.get("depth"), 1);
      if (!Number.isInteger(depth) || depth < 1 || depth > MAX_DEPTH) {
        return validationFailed("depth", "Input should be between 1 and 3");
      }
      const limit = number(search.get("limit"), 50);
      if (!Number.isInteger(limit) || limit < 1 || limit > MAX_LIMIT) {
        return validationFailed("limit", "Input should be between 1 and 500");
      }
      const minConf = number(search.get("min_conf"), 0);
      if (!(minConf >= 0 && minConf <= 1)) {
        return validationFailed("min_conf", "Input should be between 0 and 1");
      }
      const types = typesOf(search.get("types"));
      if (types instanceof Response) return types;
      const found = await graphFor(params, request);
      if (found instanceof Response) return found;
      const root = found.graph.node(symbol);
      if (!root) return notFound();
      const result = neighbors(found.graph, symbol, { direction, types, depth, minConf, limit });
      return HttpResponse.json<GraphNeighbors>(
        { root: nodeOut(root), ...result },
        { headers: { "Cache-Control": "private, no-cache", "X-Commit-Sha": found.commit } },
      );
    },
  ),
  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/graph/path", async ({ params, request }) => {
    const search = new URL(request.url).searchParams;
    const source = search.get("from");
    const destination = search.get("to");
    if (!source) return validationFailed("from", "Field required");
    if (!destination) return validationFailed("to", "Field required");
    const types = typesOf(search.get("types"));
    if (types instanceof Response) return types;
    const minConf = number(search.get("min_conf"), 0);
    const found = await graphFor(params, request);
    if (found instanceof Response) return found;
    if (!found.graph.node(source) || !found.graph.node(destination)) return notFound();
    const hops = surestPath(found.graph, source, destination, types, minConf);
    const confidence = hops ? hops.reduce((product, hop) => product * hop.edge.confidence, 1) : 0;
    return HttpResponse.json<GraphPath>(
      {
        found: hops !== null,
        hops: hops ?? [],
        confidence: Math.round(confidence * 10_000) / 10_000,
      },
      { headers: { "Cache-Control": "private, no-cache", "X-Commit-Sha": found.commit } },
    );
  }),
];
