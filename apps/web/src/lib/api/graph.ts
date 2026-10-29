// The graph endpoints' types (3.6) and what the code viewer reads from them.
import type { Provenance } from "@/components/primitives/provenance-tag";

import { LAYERS, type Layer } from "./search";
import type { Schemas } from "./types";

export type EdgeType = Schemas["EdgeType"];
export type EdgeProvenance = Schemas["Provenance"];
export type GraphNode = Schemas["GraphNodeOut"];
export type GraphHop = Schemas["GraphHopOut"];
export type GraphNeighbors = Schemas["GraphNeighborsOut"];
export type GraphPath = Schemas["GraphPathOut"];

/** What "callers" and "callees" follow (the graph's own CALL_EDGES). */
export const CALL_TYPES: readonly EdgeType[] = ["calls", "uses_hook", "renders", "injects"];

/** What a reference is: every edge into a symbol but the structural ones. */
export const REFERENCE_TYPES: readonly EdgeType[] = [
  "imports",
  "calls",
  "references",
  "inherits",
  "implements",
  "renders",
  "uses_hook",
  "injects",
  "issues_request",
  "targets",
  "handled_by",
  "resolves",
  "publishes",
  "consumes",
  "maps_to",
  "reads",
  "writes",
  "tests",
  "produces",
];

/** The most hops one request asks for (the API's cap). */
export const NEIGHBORS_LIMIT = 500;

// Resolved from the code itself (a compiler, a run, a contract, an import's binding); the rest
// are matched by rules (a receiver's type, a URL, a unique name) or suggested by a model.
const PRECISE: ReadonlySet<EdgeProvenance> = new Set([
  "scip",
  "lsp",
  "runtime",
  "contract",
  "import_resolved",
]);

/** How an edge was found, as the provenance tag says it. */
export function provenanceTag(provenance: EdgeProvenance): Provenance {
  if (PRECISE.has(provenance)) return "precise";
  return provenance === "llm_inferred" ? "inferred" : "heuristic";
}

/** How a reference reads, from the referring side ("calls", "imports", ...). */
export const EDGE_VERBS: Record<EdgeType, string> = {
  contains: "contains",
  defines: "defines",
  imports: "imports",
  calls: "calls",
  references: "references",
  inherits: "inherits from",
  implements: "implements",
  renders: "renders",
  uses_hook: "uses",
  injects: "injects",
  issues_request: "requests",
  targets: "targets",
  handled_by: "is handled by",
  resolves: "resolves",
  publishes: "publishes",
  consumes: "consumes",
  maps_to: "maps to",
  reads: "reads",
  writes: "writes",
  tests: "tests",
  produces: "produces",
};

/** The kinds a definition has (a node of another kind is one of the graph's own). */
const DEFINITION_KINDS = new Set([
  "module",
  "class",
  "function",
  "method",
  "component",
  "hook",
  "variable",
]);

export const isDefinition = (node: GraphNode) =>
  DEFINITION_KINDS.has(node.kind) && node.path !== null;

/** The lanes a layer badge shows ("other" has none). */
export const badgeLayer = (lane: Layer): Exclude<Layer, "other"> | null =>
  lane === "other" ? null : lane;

/** The last part of a name: `Session.add` and `sqlmodel/Session.add` are `add`. */
function lastPart(name: string): string {
  return name.slice(Math.max(name.lastIndexOf("."), name.lastIndexOf("/")) + 1);
}

/**
 * What `word` on `line` can name, from the hops out of a definition around it, surest first:
 * those whose edge is written on that very line (several when the graph can't tell which:
 * the reader picks), else the likeliest elsewhere in the definition. A hop's node matches by its
 * name or its qualified name's last part; a definition (it can be opened) outranks an external,
 * then the surer edge.
 */
export function candidatesOf(
  hops: readonly GraphHop[],
  word: string,
  line: number,
  path: string,
): GraphHop[] {
  const best = new Map<string, { hop: GraphHop; score: number; here: boolean }>();
  for (const hop of hops) {
    if (hop.edge.direction !== "out" || hop.depth !== 1) continue;
    if (hop.node.name !== word && lastPart(hop.node.qualified_name) !== word) continue;
    const evidence = hop.edge.evidence;
    const here = evidence?.path === path && evidence.line === line;
    const score = (here ? 4 : 0) + (isDefinition(hop.node) ? 2 : 0) + hop.edge.confidence;
    const held = best.get(hop.node.symbol_key);
    if (!held || score > held.score) best.set(hop.node.symbol_key, { hop, score, here });
  }
  const ranked = [...best.values()].sort(
    (a, b) => b.score - a.score || (a.hop.node.symbol_key < b.hop.node.symbol_key ? -1 : 1),
  );
  const onLine = ranked.filter((found) => found.here);
  return (onLine.length > 0 ? onLine : ranked.slice(0, 1)).map((found) => found.hop);
}

/** The likeliest of `candidatesOf`, for the hover card. */
export function targetOf(
  hops: readonly GraphHop[],
  word: string,
  line: number,
  path: string,
): GraphHop | null {
  return candidatesOf(hops, word, line, path)[0] ?? null;
}

export interface OutlineSymbol {
  symbol_key: string;
  name: string;
  kind: string;
  start_line: number;
  end_line: number;
}

export interface OutlineRow<T extends OutlineSymbol> {
  symbol: T;
  /** How many symbols hold it (a method of a class: 1). */
  depth: number;
}

/** The outline's rows (in order, as the API sends them), each with how deep it's nested. */
export function outlineRows<T extends OutlineSymbol>(symbols: readonly T[]): OutlineRow<T>[] {
  const open: T[] = [];
  return symbols.map((symbol) => {
    while (open.length > 0) {
      const top = open[open.length - 1];
      if (top && top.end_line >= symbol.end_line && top.start_line <= symbol.start_line) break;
      open.pop();
    }
    const row = { symbol, depth: open.length };
    open.push(symbol);
    return row;
  });
}

/** The innermost of `symbols` whose lines hold `line`. */
export function innermostAt<T extends OutlineSymbol>(
  symbols: readonly T[],
  line: number,
): T | null {
  let found: T | null = null;
  for (const symbol of symbols) {
    if (symbol.start_line > line || symbol.end_line < line) continue;
    if (!found || symbol.end_line - symbol.start_line <= found.end_line - found.start_line) {
      found = symbol;
    }
  }
  return found;
}

export interface LaneGroup {
  lane: Layer;
  hops: GraphHop[];
}

/** References by lane (in the swimlanes' order, empty ones left out), surest first in each. */
export function byLane(hops: readonly GraphHop[]): LaneGroup[] {
  const groups = new Map<Layer, GraphHop[]>();
  for (const hop of hops) {
    const list = groups.get(hop.node.lane) ?? [];
    list.push(hop);
    groups.set(hop.node.lane, list);
  }
  return LAYERS.flatMap((lane) => {
    const list = groups.get(lane);
    if (!list) return [];
    list.sort(
      (a, b) =>
        b.edge.confidence - a.edge.confidence ||
        (a.node.path ?? "").localeCompare(b.node.path ?? "") ||
        a.node.start_line - b.node.start_line,
    );
    return [{ lane, hops: list }];
  });
}

/** Where a hop's reference is: the line that proves the edge, else its node. */
export function hopLocation(hop: GraphHop): { path: string; line: number } | null {
  const evidence = hop.edge.evidence;
  if (evidence) return { path: evidence.path, line: evidence.line ?? 1 };
  return hop.node.path ? { path: hop.node.path, line: Math.max(hop.node.start_line, 1) } : null;
}

/** A count that `limit` may have cut: "500+". */
export function countLabel(count: number, truncated: boolean): string {
  return truncated ? `${count.toLocaleString("en-US")}+` : count.toLocaleString("en-US");
}
