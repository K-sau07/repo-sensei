/**
 * The architecture view's layout (6.6): a system diagram, read top to bottom as one is drawn on
 * a whiteboard. Who uses it at the top, then its frontend, its API, its services and workers,
 * its data, and the databases and services outside it at the bottom: a row each (ELK's layered
 * algorithm with a partition per row, so a row is never above the one before it), the lines
 * routed square round the boxes, never through one. ELK says each line's bends; the canvas
 * draws them as they are.
 *
 * A whole app's spec says each part's row (its layer: actor, frontend, api, service, worker,
 * data, other, outside); a flow's code nodes are placed by their trace layer (a component in
 * the frontend, a handler in the API, a table in the data row).
 */
import type { ElkNode } from "elkjs/lib/elk-api";

import type { Spec, SpecEdge, SpecNode } from "@/lib/api/diagrams";

import type { Placed } from "./elk-layout";

export const ROWS = [
  { row: "actor", label: "Users", lane: "other" },
  { row: "frontend", label: "Frontend", lane: "ui" },
  { row: "api", label: "API", lane: "route" },
  { row: "service", label: "Services", lane: "service" },
  { row: "worker", label: "Workers", lane: "service" },
  { row: "data", label: "Data", lane: "db" },
  { row: "other", label: "Other", lane: "other" },
  { row: "outside", label: "Outside the app", lane: "db" },
] as const;
export type Row = (typeof ROWS)[number];

const ROW_OF: Record<string, Row["row"]> = {
  actor: "actor",
  frontend: "frontend",
  ui: "frontend",
  state: "frontend",
  client: "frontend",
  api: "api",
  route: "api",
  handler: "api",
  service: "service",
  worker: "worker",
  data: "data",
  db: "data",
  outside: "outside",
};

/** A node's row (anything unknown is "other"). */
export function rowOf(node: SpecNode): Row {
  const row = ROW_OF[node.layer] ?? "other";
  return ROWS.find((r) => r.row === row) ?? ROWS[6];
}

/** What is drawn as a cylinder: a database, a cache, a search index, storage; a table. */
const STORES = new Set(["database", "cache", "search", "storage", "table", "collection"]);

export function isStore(node: SpecNode): boolean {
  return STORES.has(node.kind);
}

export const BOX_WIDTH = 224;
export const BOX_HEIGHT = 76;
export const STORE_WIDTH = 168;
export const STORE_HEIGHT = 96;

export function sizeOf(node: SpecNode): { width: number; height: number } {
  return isStore(node)
    ? { width: STORE_WIDTH, height: STORE_HEIGHT }
    : { width: BOX_WIDTH, height: BOX_HEIGHT };
}

export interface SystemRow {
  row: Row;
  y: number;
  height: number;
}

export interface PlacedSystem {
  /** Each box's top-left corner. */
  at: Record<string, Placed>;
  /** Each line's points, start to end (by the line's index), as ELK routed it. */
  routes: Record<string, Placed[]>;
  rows: SystemRow[];
  /** The diagram's left edge (the rows' names go left of it). */
  left: number;
}

/** The layout's input: a partition per row, in the rows' order. */
export function systemGraph(spec: Spec, edges: readonly SpecEdge[]): ElkNode {
  const index = (node: SpecNode) => ROWS.findIndex((r) => r.row === rowOf(node).row);
  return {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "DOWN",
      "elk.partitioning.activate": "true",
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.layered.spacing.nodeNodeBetweenLayers": "72",
      "elk.spacing.nodeNode": "48",
      "elk.spacing.edgeNode": "28",
      "elk.spacing.edgeEdge": "14",
      "elk.layered.spacing.edgeNodeBetweenLayers": "28",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      "elk.randomSeed": "7",
    },
    children: spec.nodes.map((node) => ({
      id: node.id,
      ...sizeOf(node),
      layoutOptions: { "elk.partitioning.partition": String(index(node)) },
    })),
    edges: edges.flatMap((edge, i) =>
      edge.from === edge.to
        ? []
        : [{ id: `e${String(i)}`, sources: [edge.from], targets: [edge.to] }],
    ),
  };
}

/** The laid-out graph as the canvas draws it: boxes, lines' points, and each row's band. */
export function placedSystem(spec: Spec, laid: ElkNode): PlacedSystem {
  const at: Record<string, Placed> = {};
  for (const child of laid.children ?? []) at[child.id] = { x: child.x ?? 0, y: child.y ?? 0 };
  const routes: Record<string, Placed[]> = {};
  for (const edge of laid.edges ?? []) {
    const section = edge.sections?.[0];
    if (!section) continue;
    routes[edge.id] = [section.startPoint, ...(section.bendPoints ?? []), section.endPoint];
  }
  const rows = new Map<Row["row"], SystemRow>();
  let left = Infinity;
  for (const node of spec.nodes) {
    const place = at[node.id];
    if (!place) continue;
    const { height } = sizeOf(node);
    left = Math.min(left, place.x);
    const row = rowOf(node);
    const held = rows.get(row.row);
    const top = Math.min(held?.y ?? place.y, place.y);
    const bottom = Math.max(held ? held.y + held.height : place.y + height, place.y + height);
    rows.set(row.row, { row, y: top, height: bottom - top });
  }
  return {
    at,
    routes,
    rows: ROWS.flatMap((r) => rows.get(r.row) ?? []),
    left: Number.isFinite(left) ? left : 0,
  };
}

/** A path through ``points``, its corners rounded (``radius``). */
export function roundedPath(points: readonly Placed[], radius = 10): string {
  const [first, ...rest] = points;
  if (!first) return "";
  let d = `M${String(first.x)},${String(first.y)}`;
  for (let i = 0; i < rest.length; i++) {
    const here = rest[i];
    const next = rest[i + 1];
    const before = i === 0 ? first : rest[i - 1];
    if (!here || !before) continue;
    if (!next) {
      d += ` L${String(here.x)},${String(here.y)}`;
      continue;
    }
    const r = Math.min(
      radius,
      Math.hypot(here.x - before.x, here.y - before.y) / 2,
      Math.hypot(next.x - here.x, next.y - here.y) / 2,
    );
    const into = toward(here, before, r);
    const out = toward(here, next, r);
    d += ` L${String(into.x)},${String(into.y)} Q${String(here.x)},${String(here.y)} ${String(out.x)},${String(out.y)}`;
  }
  return d;
}

function toward(from: Placed, to: Placed, by: number): Placed {
  const length = Math.hypot(to.x - from.x, to.y - from.y) || 1;
  return {
    x: from.x + ((to.x - from.x) / length) * by,
    y: from.y + ((to.y - from.y) / length) * by,
  };
}

/** Where a line's label goes: the middle of its longest straight run. */
export function labelPoint(points: readonly Placed[]): Placed {
  let best: Placed = points[0] ?? { x: 0, y: 0 };
  let longest = -1;
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1];
    const b = points[i];
    if (!a || !b) continue;
    const length = Math.hypot(b.x - a.x, b.y - a.y);
    if (length > longest) {
      longest = length;
      best = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    }
  }
  return best;
}
