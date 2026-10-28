/**
 * The mock snapshot's code graph (fixtures/graph.json, built by the engine's resolvers over the
 * files the mock serves: scripts/mock_graph.py), loaded on first use, and what the graph and
 * symbol endpoints answer from it, as the API does (3.6's `GraphSnapshot`): a node's neighbours,
 * nearest first, each once by its surest edge; the surest path between two nodes; the
 * definitions around a line; a file's outline.
 */
import type { EdgeType, GraphHop, GraphNode } from "@/lib/api/graph";
import type { SymbolAt, SymbolKind } from "@/lib/api/types";

import { SYMBOLS } from "./symbols";

interface FixtureNode extends GraphNode {
  docstring: string | null;
}

interface FixtureEdge {
  src: string;
  dst: string;
  type: GraphHop["edge"]["type"];
  provenance: GraphHop["edge"]["provenance"];
  confidence: number;
  evidence: { path: string; line: number | null } | null;
}

interface Fixture {
  nodes: FixtureNode[];
  edges: FixtureEdge[];
}

export interface MockGraph {
  node: (key: string) => FixtureNode | undefined;
  out: (key: string) => readonly FixtureEdge[];
  in: (key: string) => readonly FixtureEdge[];
  byPath: (path: string) => readonly FixtureNode[];
}

const MAX_DEPTH = 3;
const DEFINITION_KINDS: ReadonlySet<string> = new Set<SymbolKind>([
  "module",
  "class",
  "function",
  "method",
  "component",
  "hook",
  "variable",
]);
const SIGNATURES = new Map(SYMBOLS.map((symbol) => [symbol.symbol_key, symbol.signature]));

let loaded: Promise<MockGraph> | null = null;

function index(fixture: Fixture): MockGraph {
  const nodes = new Map(fixture.nodes.map((node) => [node.symbol_key, node]));
  const out = new Map<string, FixtureEdge[]>();
  const into = new Map<string, FixtureEdge[]>();
  const files = new Map<string, FixtureNode[]>();
  for (const edge of fixture.edges) {
    out.set(edge.src, [...(out.get(edge.src) ?? []), edge]);
    into.set(edge.dst, [...(into.get(edge.dst) ?? []), edge]);
  }
  for (const node of fixture.nodes) {
    if (node.path) files.set(node.path, [...(files.get(node.path) ?? []), node]);
  }
  return {
    node: (key) => nodes.get(key),
    out: (key) => out.get(key) ?? [],
    in: (key) => into.get(key) ?? [],
    byPath: (path) => files.get(path) ?? [],
  };
}

export function mockGraph(): Promise<MockGraph> {
  loaded ??= import("./fixtures/graph.json").then((module) => index(module.default as Fixture));
  return loaded;
}

/** A node as the endpoints send it (without the fixture's docstring). */
export function nodeOut(node: FixtureNode): GraphNode {
  const { docstring: _, ...out } = node;
  return out;
}

const allowed = (edge: FixtureEdge, types: ReadonlySet<EdgeType> | null, minConf: number) =>
  edge.confidence >= minConf && (types === null || types.has(edge.type));

function hopOf(
  graph: MockGraph,
  edge: FixtureEdge,
  direction: "out" | "in",
  depth: number,
  via: string,
): GraphHop | null {
  const node = graph.node(direction === "out" ? edge.dst : edge.src);
  if (!node) return null;
  return {
    node: nodeOut(node),
    edge: {
      type: edge.type,
      confidence: edge.confidence,
      provenance: edge.provenance,
      evidence: edge.evidence,
      direction,
    },
    depth,
    via,
  };
}

export interface NeighborsQuery {
  direction: "out" | "in" | "both";
  types: ReadonlySet<EdgeType> | null;
  depth: number;
  minConf: number;
  limit: number;
}

/** `GraphSnapshot.neighbors`: level by level, each node once (its surest edge at that level),
 * surest first then by key; and whether `limit` cut it short. */
export function neighbors(
  graph: MockGraph,
  key: string,
  query: NeighborsQuery,
): { hops: GraphHop[]; truncated: boolean } {
  const seen = new Set([key]);
  const hops: GraphHop[] = [];
  let frontier = [key];
  for (let level = 1; level <= Math.min(query.depth, MAX_DEPTH); level += 1) {
    const found = new Map<string, GraphHop>();
    for (const here of frontier) {
      const steps: (readonly [FixtureEdge, "out" | "in"])[] = [];
      if (query.direction !== "in") steps.push(...graph.out(here).map((e) => [e, "out"] as const));
      if (query.direction !== "out") steps.push(...graph.in(here).map((e) => [e, "in"] as const));
      for (const [edge, way] of steps) {
        const there = way === "out" ? edge.dst : edge.src;
        if (seen.has(there) || !allowed(edge, query.types, query.minConf)) continue;
        const held = found.get(there);
        if (held && held.edge.confidence >= edge.confidence) continue;
        const hop = hopOf(graph, edge, way, level, here);
        if (hop) found.set(there, hop);
      }
    }
    const ordered = [...found.entries()].sort(
      ([a, x], [b, y]) => y.edge.confidence - x.edge.confidence || (a < b ? -1 : a > b ? 1 : 0),
    );
    for (const [there, hop] of ordered) {
      if (hops.length >= query.limit) return { hops, truncated: true };
      seen.add(there);
      hops.push(hop);
    }
    frontier = ordered.map(([there]) => there);
    if (frontier.length === 0) break;
  }
  return { hops, truncated: false };
}

/** `GraphSnapshot.shortest_path`: the surest path along the edges (Dijkstra on -log(conf)). */
export function surestPath(
  graph: MockGraph,
  source: string,
  target: string,
  types: ReadonlySet<EdgeType> | null,
  minConf: number,
): GraphHop[] | null {
  if (source === target) return null;
  const cost = new Map([[source, 0]]);
  const previous = new Map<string, FixtureEdge>();
  const done = new Set<string>();
  for (;;) {
    let here: string | null = null;
    for (const [key, value] of cost) {
      if (!done.has(key) && (here === null || value < (cost.get(here) ?? Infinity))) here = key;
    }
    if (here === null || here === target) break;
    done.add(here);
    for (const edge of graph.out(here)) {
      if (!allowed(edge, types, minConf)) continue;
      const next = (cost.get(here) ?? 0) - Math.log(Math.max(edge.confidence, 1e-6)) + 1e-9;
      if (next < (cost.get(edge.dst) ?? Infinity)) {
        cost.set(edge.dst, next);
        previous.set(edge.dst, edge);
      }
    }
  }
  if (!previous.has(target)) return null;
  const edges: FixtureEdge[] = [];
  for (let at = target; at !== source;) {
    const edge = previous.get(at);
    if (!edge) return null;
    edges.unshift(edge);
    at = edge.src;
  }
  return edges.flatMap((edge, i) => hopOf(graph, edge, "out", i + 1, edge.src) ?? []);
}

function symbolAt(node: FixtureNode): SymbolAt {
  return {
    symbol_key: node.symbol_key,
    name: node.name,
    qualified_name: node.qualified_name,
    kind: node.kind as SymbolKind,
    start_line: node.start_line,
    end_line: node.end_line,
    signature: SIGNATURES.get(node.symbol_key) ?? null,
    docstring: node.docstring,
  };
}

const definitions = (graph: MockGraph, path: string) =>
  graph.byPath(path).filter((node) => DEFINITION_KINDS.has(node.kind));

/** `symbols_at`: the definitions whose lines hold `line`, innermost first, the module last. */
export function symbolsAt(graph: MockGraph, path: string, line: number): SymbolAt[] {
  return definitions(graph, path)
    .filter((node) => node.start_line <= line && node.end_line >= line)
    .sort(
      (a, b) =>
        Number(a.kind === "module") - Number(b.kind === "module") ||
        a.end_line - a.start_line - (b.end_line - b.start_line) ||
        b.start_line - a.start_line,
    )
    .map(symbolAt);
}

/** `symbols_in`: a file's definitions in order, the module left out. */
export function outline(graph: MockGraph, path: string): SymbolAt[] {
  return definitions(graph, path)
    .filter((node) => node.kind !== "module")
    .sort(
      (a, b) =>
        a.start_line - b.start_line ||
        b.end_line - a.end_line ||
        (a.symbol_key < b.symbol_key ? -1 : 1),
    )
    .map(symbolAt);
}
