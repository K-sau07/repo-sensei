/**
 * The dependency graph's types (6.2–6.5) and what the graph explorer (6.7) works out from them:
 * each node's fan-in and size, a node's neighbourhood, the shortest path between two, where a
 * search hit is at a level, and a level with some of its nodes opened into the level below
 * (semantic zoom).
 */
import type { Schemas } from "./types";

export type Deps = Schemas["DepsOut"];
export type DepNode = Schemas["DepNodeOut"];
export type DepEdge = Schemas["DepEdgeOut"];
export type DepLevel = Deps["level"];
export type Dsm = Schemas["DsmOut"];
export type DsmNode = Schemas["DsmNodeOut"];
export type DsmCell = Schemas["DsmCellOut"];
export type Cycles = Schemas["CyclesOut"];
export type Cycle = Schemas["CycleOut"];
export type Packages = Schemas["PackagesOut"];
export type Package = Schemas["PackageOut"];
export type EdgeEvidence = Schemas["EdgeEvidenceOut"];
export type Evidence = Schemas["EvidenceOut"];

export const DEP_LEVELS: readonly DepLevel[] = ["service", "package", "module", "file", "symbol"];

export const LEVEL_LABELS: Record<DepLevel, string> = {
  service: "Services",
  package: "Packages",
  module: "Modules",
  file: "Files",
  symbol: "Symbols",
};

/** The level a node opens into, zoomed in (none below symbols). */
export function finer(level: DepLevel): DepLevel | null {
  return DEP_LEVELS[DEP_LEVELS.indexOf(level) + 1] ?? null;
}

/** The search lanes' five colors for the graph's layers (as the trace's lanes group them). */
export type Tint = "ui" | "api" | "svc" | "db" | "other";

export const LAYER_TINT: Record<string, Tint> = {
  ui: "ui",
  state: "ui",
  client: "api",
  route: "api",
  handler: "api",
  service: "svc",
  db: "db",
  other: "other",
};

export function tintOf(layer: string): Tint {
  return LAYER_TINT[layer] ?? "other";
}

export function edgeKey(source: string, target: string): string {
  return `${source}\u0000${target}`;
}

/** How much depends on each node: its incoming edges' weights. */
export function fanIn(edges: readonly DepEdge[]): Map<string, number> {
  const found = new Map<string, number>();
  for (const edge of edges) found.set(edge.target, (found.get(edge.target) ?? 0) + edge.weight);
  return found;
}

export const MIN_SIZE = 3;
export const MAX_SIZE = 16;

/** A node's drawn size (px) by its fan-in, square-root scaled to the most depended on. */
export function sizeOf(fan: number, most: number): number {
  if (most <= 0) return MIN_SIZE;
  return MIN_SIZE + (MAX_SIZE - MIN_SIZE) * Math.sqrt(Math.max(0, fan) / most);
}

/** The nodes within `degree` hops of `id`, either way (`id` itself included). */
export function neighborhood(
  edges: readonly Pick<DepEdge, "source" | "target">[],
  id: string,
  degree: number,
): Set<string> {
  const near = new Map<string, string[]>();
  const link = (a: string, b: string) => {
    const listed = near.get(a);
    if (listed) listed.push(b);
    else near.set(a, [b]);
  };
  for (const edge of edges) {
    link(edge.source, edge.target);
    link(edge.target, edge.source);
  }
  const seen = new Set([id]);
  let frontier = [id];
  for (let hop = 0; hop < degree && frontier.length > 0; hop++) {
    const next: string[] = [];
    for (const at of frontier) {
      for (const other of near.get(at) ?? []) {
        if (!seen.has(other)) {
          seen.add(other);
          next.push(other);
        }
      }
    }
    frontier = next;
  }
  return seen;
}

/** The fewest hops from `from` to `to` along the edges' direction (what depends on what), as
 * the nodes in order; null when `to` can't be reached. */
export function shortestPath(
  edges: readonly Pick<DepEdge, "source" | "target">[],
  from: string,
  to: string,
): string[] | null {
  if (from === to) return [from];
  const out = new Map<string, string[]>();
  for (const edge of edges) {
    const listed = out.get(edge.source);
    if (listed) listed.push(edge.target);
    else out.set(edge.source, [edge.target]);
  }
  const previous = new Map<string, string>([[from, from]]);
  let frontier = [from];
  while (frontier.length > 0) {
    const next: string[] = [];
    for (const at of frontier) {
      for (const other of out.get(at) ?? []) {
        if (previous.has(other)) continue;
        previous.set(other, at);
        if (other === to) {
          const path = [to];
          let step = to;
          while (step !== from) {
            step = previous.get(step) ?? from;
            path.push(step);
          }
          return path.reverse();
        }
        next.push(other);
      }
    }
    frontier = next;
  }
  return null;
}

function folderOf(path: string): string {
  const at = path.lastIndexOf("/");
  return at < 0 ? "." : path.slice(0, at);
}

/** The node a file (or a symbol in it) is in at `level`: the file, its folder, or the deepest
 * package or service holding it. Null when the level has no such node. */
export function nodeOfPath(
  ids: ReadonlySet<string>,
  level: DepLevel,
  path: string,
  symbolKey: string | null = null,
): string | null {
  if (level === "symbol") return symbolKey !== null && ids.has(symbolKey) ? symbolKey : null;
  if (level === "file") return ids.has(path) ? path : null;
  if (level === "module") {
    const folder = folderOf(path);
    return ids.has(folder) ? folder : null;
  }
  let best: string | null = null;
  for (const id of ids) {
    if (id.startsWith("~")) continue;
    const inside = id === "." || path.startsWith(`${id}/`);
    if (inside && (best === null || best === "." || id.length > best.length)) best = id;
  }
  return best;
}

/** The nodes whose name or id has `query` in it (case aside). */
export function matching(nodes: readonly DepNode[], query: string): string[] {
  const wanted = query.trim().toLowerCase();
  if (!wanted) return [];
  return nodes
    .filter((n) => n.label.toLowerCase().includes(wanted) || n.id.toLowerCase().includes(wanted))
    .map((n) => n.id);
}

/** How far an opened node's children spread around where it was (layout units, of 2). */
export const OPEN_RADIUS = 0.14;

/** A node drawn in the explorer: a level's, or one opened into from the level above. */
export interface ShownNode extends DepNode {
  level: DepLevel;
}

/** The explorer's graph: `coarse` with the nodes in `open` replaced in place by their children
 * in `fine` (the level below), each child spread around where its parent was, as `fine` places
 * the siblings. An edge between a child and a node not opened goes to that node; edges are
 * summed by type and kept once per pair. */
export function expand(
  coarse: Deps,
  fine: Deps | null,
  open: ReadonlySet<string>,
): { nodes: ShownNode[]; edges: DepEdge[] } {
  if (fine === null || open.size === 0) {
    return { nodes: coarse.nodes.map((n) => ({ ...n, level: coarse.level })), edges: coarse.edges };
  }
  const parents = new Map(coarse.nodes.map((n) => [n.id, n]));
  const opened = new Set([...open].filter((id) => parents.has(id)));
  const children = new Map<string, DepNode[]>();
  for (const node of fine.nodes) {
    if (node.cluster === null || !opened.has(node.cluster)) continue;
    const listed = children.get(node.cluster);
    if (listed) listed.push(node);
    else children.set(node.cluster, [node]);
  }
  const nodes: ShownNode[] = [];
  const shown = new Set<string>(); // the fine nodes drawn as themselves
  const shownAs = new Map<string, string>(); // a fine node → what stands for it
  for (const parent of coarse.nodes) {
    const kids = children.get(parent.id);
    if (!opened.has(parent.id) || !kids) {
      nodes.push({ ...parent, level: coarse.level });
      continue;
    }
    const cx = kids.reduce((s, k) => s + k.x, 0) / kids.length;
    const cy = kids.reduce((s, k) => s + k.y, 0) / kids.length;
    const spread = Math.max(...kids.map((k) => Math.hypot(k.x - cx, k.y - cy))) || 1;
    for (const kid of kids) {
      const scale = kids.length === 1 ? 0 : OPEN_RADIUS / spread;
      nodes.push({
        ...kid,
        x: parent.x + (kid.x - cx) * scale,
        y: parent.y + (kid.y - cy) * scale,
        level: fine.level,
      });
      shown.add(kid.id);
      shownAs.set(kid.id, kid.id);
    }
  }
  for (const node of fine.nodes) {
    if (!shown.has(node.id) && node.cluster !== null && parents.has(node.cluster)) {
      shownAs.set(node.id, node.cluster);
    }
  }
  const merged = new Map<string, DepEdge>();
  const add = (edge: DepEdge) => {
    if (edge.source === edge.target) return;
    const key = edgeKey(edge.source, edge.target);
    const held = merged.get(key);
    if (!held) {
      merged.set(key, { ...edge, types: { ...edge.types }, samples: [...edge.samples] });
      return;
    }
    held.weight += edge.weight;
    for (const [type, n] of Object.entries(edge.types)) {
      held.types[type] = (held.types[type] ?? 0) + n;
    }
    held.confidence = Math.max(held.confidence, edge.confidence);
    held.samples = [...held.samples, ...edge.samples].slice(0, 3);
  };
  // Between nodes neither opened: the coarse level's own edge.
  for (const edge of coarse.edges) {
    if (!opened.has(edge.source) && !opened.has(edge.target)) add(edge);
  }
  // Touching an opened node: the fine level's, each end as it is shown.
  for (const edge of fine.edges) {
    const source = shownAs.get(edge.source);
    const target = shownAs.get(edge.target);
    if (source === undefined || target === undefined) continue;
    if (shown.has(edge.source) || shown.has(edge.target)) add({ ...edge, source, target });
  }
  return { nodes, edges: [...merged.values()] };
}

/** The edges that close a cycle, by key. */
export function cycleEdges(cycles: readonly Cycle[]): Set<string> {
  const found = new Set<string>();
  for (const cycle of cycles) {
    for (const edge of cycle.edges) found.add(edgeKey(edge.source, edge.target));
  }
  return found;
}

/** What an edge counts, as words: "182 calls, 12 imports". */
export function typesLabel(types: Readonly<Record<string, number>>): string {
  return Object.entries(types)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([type, n]) => `${String(n)} ${type.replaceAll("_", " ")}`)
    .join(", ");
}
