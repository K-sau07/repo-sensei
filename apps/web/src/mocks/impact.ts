/**
 * The mock impact (6.8): what a change to a symbol, or to some files, may break in the mock
 * snapshot's graph, worked out as the engine does (6.4, `sensei_engine.impact`). Everything that
 * depends on the change, walked backwards along the graph's dependency edges, surest first: a
 * node's score is the product of its way's confidences, times 0.85 a hop (an endpoint and its
 * handler, a client call and its endpoint, are one step: no hop); edges under `minConf` and edges
 * a model inferred aren't followed. The tables the changed code reads, writes or maps
 * to (directly, or through its ORM models) are affected too. The graph's pages come from
 * fixtures/impact.json (scripts/mock_impact.py, the engine's trace index), which also holds the
 * engine's answer for a few changes: a test checks this walk gives the same.
 */
import type { FixtureEdge, FixtureNode, MockGraph } from "./graph";

export const DECAY = 0.85;
export const MIN_CONF = 0.5;
export const MAX_DEPTH = 8;
export const MAX_AFFECTED = 2_000;

/** One step of the code seen from two sides: reaching one from the other is no hop. */
const SAME_STEP: ReadonlySet<string> = new Set(["handled_by", "targets"]);

const DEPENDS_ON: ReadonlySet<string> = new Set([
  "calls",
  "references",
  "imports",
  "inherits",
  "implements",
  "renders",
  "uses_hook",
  "injects",
  "issues_request",
  "targets",
  "handled_by",
  "resolves",
  "tests",
  "reads",
  "writes",
  "maps_to",
  "consumes",
]);
const DATA: ReadonlySet<string> = new Set(["reads", "writes", "maps_to"]);
const ROUTE_KINDS: ReadonlySet<string> = new Set([
  "api_endpoint",
  "graphql_operation",
  "graphql_field",
  "rpc_method",
]);
const LEFT_OUT: ReadonlySet<string> = new Set([
  "external_symbol",
  "package",
  "column",
  "client_outcome",
  "http_client_call",
]);
const NEXT_PAGE = /(^|\/)(app\/(.+\/)?page|pages\/.+)\.(tsx|jsx|ts|js|mdx)$/;
const TEST_FILE =
  /(^|\/)(tests?\/|__tests__\/|test_[^/]+\.py$|[^/]+_test\.(py|go)$|[^/]+\.(test|spec)\.[jt]sx?$)/;

export const CATEGORIES = ["routes", "screens", "tests", "tables", "symbols"] as const;
export type Category = (typeof CATEGORIES)[number];

export interface MockAffected {
  node: FixtureNode;
  score: number;
  distance: number;
  via: string | null;
  edge: string | null;
}

export interface MockImpact {
  changed: FixtureNode[];
  affected: Record<Category, MockAffected[]>;
  heat: Record<string, number>;
  truncated: boolean;
}

function follows(edge: FixtureEdge, minConf: number): boolean {
  return (
    DEPENDS_ON.has(edge.type) && edge.confidence >= minConf && edge.provenance !== "llm_inferred"
  );
}

function round(score: number): number {
  return Math.round(score * 10_000) / 10_000;
}

export function categoryOf(node: FixtureNode, screens: ReadonlySet<string>): Category {
  if (ROUTE_KINDS.has(node.kind)) return "routes";
  if (node.kind === "table") return "tables";
  if (node.kind === "test" || (node.path && TEST_FILE.test(node.path))) return "tests";
  if (
    screens.has(node.symbol_key) ||
    (node.kind === "component" && node.path && NEXT_PAGE.test(node.path))
  ) {
    return "screens";
  }
  return "symbols";
}

/** The changed nodes: the symbols named (by key), then the definitions in the files. */
export function changedNodes(
  graph: MockGraph,
  { symbols = [], files = [] }: { symbols?: readonly string[]; files?: readonly string[] },
): FixtureNode[] {
  const found: FixtureNode[] = [];
  const seen = new Set<string>();
  for (const key of symbols) {
    const node = graph.node(key);
    if (node && !seen.has(key)) {
      seen.add(key);
      found.push(node);
    }
  }
  const byKey = (a: FixtureNode, b: FixtureNode) =>
    a.symbol_key < b.symbol_key ? -1 : a.symbol_key > b.symbol_key ? 1 : 0;
  for (const path of [...new Set(files)]) {
    for (const node of [...graph.byPath(path)].sort(byKey)) {
      if (!seen.has(node.symbol_key) && !LEFT_OUT.has(node.kind)) {
        seen.add(node.symbol_key);
        found.push(node);
      }
    }
  }
  return found;
}

type Best = Map<string, { score: number; depth: number; via: string | null; edge: string | null }>;

/** Before, in the walk's order: the surer, then the nearer, then by key (the graph's order). */
function before(a: [number, number, string], b: [number, number, string]): boolean {
  return a[0] !== b[0] ? a[0] > b[0] : a[1] !== b[1] ? a[1] < b[1] : a[2] < b[2];
}

export function impactOf(
  graph: MockGraph,
  screens: ReadonlySet<string>,
  changed: readonly FixtureNode[],
  minConf = MIN_CONF,
): MockImpact {
  const starts = changed.map((n) => n.symbol_key);
  const started = new Set(starts);
  const best: Best = new Map(starts.map((k) => [k, { score: 1, depth: 0, via: null, edge: null }]));
  let queue: [number, number, string][] = starts.map((k) => [1, 0, k]);
  // The fewest hops each node was walked from (as the engine: a node reached surest far off is
  // walked again when a less sure way reaches it nearer).
  const walked = new Map<string, number>();
  const queued = new Map<string, number>(starts.map((k) => [k, 0]));
  const beyond = MAX_DEPTH + 1;
  while (queue.length > 0) {
    // The surest left (a small graph: a scan, not a heap).
    let at = 0;
    for (let i = 1; i < queue.length; i++) {
      const [entry, held] = [queue[i], queue[at]];
      if (entry && held && before(entry, held)) at = i;
    }
    const popped = queue[at];
    queue = queue.filter((_, i) => i !== at);
    if (!popped) break;
    const [score, depth, here] = popped;
    if ((walked.get(here) ?? beyond) <= depth) continue;
    walked.set(here, depth);
    // Newest first, as the engine's graph (rustworkx) lists a node's edges: ties go the same way.
    for (const edge of [...graph.in(here)].reverse()) {
      const there = edge.src;
      const same = SAME_STEP.has(edge.type);
      const on = same ? depth : depth + 1; // at the depth limit, only the free hops
      if (on > MAX_DEPTH || !follows(edge, minConf) || (walked.get(there) ?? beyond) <= on) {
        continue;
      }
      const next = score * edge.confidence * (same ? 1 : DECAY);
      const held = best.get(there);
      if (held === undefined || next > held.score) {
        best.set(there, { score: next, depth: on, via: here, edge: edge.type });
      } else if ((queued.get(there) ?? beyond) <= on) {
        continue;
      }
      queued.set(there, Math.min(queued.get(there) ?? beyond, on));
      queue.push([next, on, there]);
    }
  }
  touchedTables(graph, starts, best, minConf);
  const items: MockAffected[] = [];
  for (const [key, { score, depth, via, edge }] of best) {
    const node = graph.node(key);
    if (!node || started.has(key) || LEFT_OUT.has(node.kind)) continue;
    items.push({ node, score, distance: depth, via, edge });
  }
  items.sort(
    (a, b) =>
      b.score - a.score ||
      a.distance - b.distance ||
      (a.node.symbol_key < b.node.symbol_key ? -1 : 1),
  );
  const truncated = items.length > MAX_AFFECTED;
  const affected = Object.fromEntries(CATEGORIES.map((c) => [c, [] as MockAffected[]])) as Record<
    Category,
    MockAffected[]
  >;
  for (const item of items.slice(0, MAX_AFFECTED)) {
    affected[categoryOf(item.node, screens)].push(item);
  }
  const heat: Record<string, number> = Object.fromEntries(starts.map((k) => [k, 1]));
  for (const list of Object.values(affected)) {
    for (const item of list) heat[item.node.symbol_key] = round(item.score);
  }
  return { changed: [...changed], affected, heat, truncated };
}

/** The tables the changed code touches, directly or through its ORM models. */
function touchedTables(
  graph: MockGraph,
  starts: readonly string[],
  best: Best,
  minConf: number,
): void {
  const started = new Set(starts);
  let frontier: [string, number][] = starts.map((k) => [k, 1]);
  for (const depth of [1, 2]) {
    const following: [string, number][] = [];
    for (const [here, carried] of frontier) {
      for (const edge of [...graph.out(here)].reverse()) {
        if (!DATA.has(edge.type) || !follows(edge, minConf)) continue;
        const score = carried * edge.confidence * DECAY;
        const there = graph.node(edge.dst);
        if (!there) continue;
        if (there.kind === "table") {
          const held = best.get(edge.dst);
          if (held === undefined || (!started.has(edge.dst) && score > held.score)) {
            best.set(edge.dst, { score, depth, via: here, edge: edge.type });
          }
        } else {
          following.push([edge.dst, score]);
        }
      }
    }
    frontier = following;
  }
}
