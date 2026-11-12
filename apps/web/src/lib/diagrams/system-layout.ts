/**
 * The architecture view's layout (6.6): a system diagram, read top to bottom as one is drawn on
 * a whiteboard. Who uses it at the top, then its frontend, its API, its services and workers,
 * its data, and the databases and services outside it at the bottom: a row each, centered, the
 * boxes in a row ordered to keep the lines between rows from crossing (each placed by where
 * what it is joined to is). A wide row wraps. No layout engine: the same diagram is always
 * drawn the same way.
 *
 * A whole app's spec says each part's row (its layer: actor, frontend, api, service, worker,
 * data, other, outside); a flow's code nodes are placed by their trace layer (a component in
 * the frontend, a handler in the API, a table in the data row).
 */
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
const ROW_GAP = 88;
const COL_GAP = 40;
export const MAX_PER_ROW = 4;

export function sizeOf(node: SpecNode): { width: number; height: number } {
  return isStore(node)
    ? { width: STORE_WIDTH, height: STORE_HEIGHT }
    : { width: BOX_WIDTH, height: BOX_HEIGHT };
}

export interface SystemRow {
  row: Row;
  y: number;
  height: number;
  ids: string[];
}

export interface PlacedSystem {
  /** Each box's top-left corner. */
  at: Record<string, Placed>;
  rows: SystemRow[];
  width: number;
  height: number;
}

function neighbours(edges: readonly SpecEdge[]): Map<string, string[]> {
  const near = new Map<string, string[]>();
  for (const edge of edges) {
    if (edge.from === edge.to) continue;
    near.set(edge.from, [...(near.get(edge.from) ?? []), edge.to]);
    near.set(edge.to, [...(near.get(edge.to) ?? []), edge.from]);
  }
  return near;
}

/** ``ids`` ordered by where what they are joined to sits (its mean place, from 0 to 1, in the
 * rows already ordered); those joined to none keep their order, after. */
function byBarycenter(
  ids: string[],
  near: ReadonlyMap<string, string[]>,
  place: ReadonlyMap<string, number>,
): string[] {
  const keyed = ids.map((id, i) => {
    const known = (near.get(id) ?? []).flatMap((n) => {
      const at = place.get(n);
      return at === undefined ? [] : [at];
    });
    const mean = known.length ? known.reduce((a, b) => a + b, 0) / known.length : null;
    return { id, i, mean };
  });
  keyed.sort((a, b) => {
    if (a.mean === null && b.mean === null) return a.i - b.i;
    if (a.mean === null) return 1;
    if (b.mean === null) return -1;
    return a.mean - b.mean || a.i - b.i;
  });
  return keyed.map((k) => k.id);
}

function relative(rows: readonly string[][]): Map<string, number> {
  const place = new Map<string, number>();
  for (const ids of rows) {
    ids.forEach((id, i) => place.set(id, ids.length > 1 ? i / (ids.length - 1) : 0.5));
  }
  return place;
}

/** The system laid out (see the module). */
export function layoutSystem(spec: Spec, edges: readonly SpecEdge[]): PlacedSystem {
  const byRow = new Map<Row["row"], string[]>();
  for (const node of spec.nodes) {
    const row = rowOf(node).row;
    byRow.set(row, [...(byRow.get(row) ?? []), node.id]);
  }
  const present = ROWS.filter((r) => byRow.has(r.row));
  let order = present.map((r) => [...(byRow.get(r.row) ?? [])]);
  const near = neighbours(edges);
  // Down, up, down again: each row by the rows placed around it.
  for (const sweep of [1, -1, 1]) {
    const indices = order.map((_, i) => i);
    if (sweep < 0) indices.reverse();
    for (const i of indices.slice(1)) {
      const others = order.filter((_, j) => j !== i);
      order = order.map((ids, j) => (j === i ? byBarycenter(ids, near, relative(others)) : ids));
    }
  }
  const nodes = new Map(spec.nodes.map((node) => [node.id, node]));
  const at: Record<string, Placed> = {};
  const rows: SystemRow[] = [];
  let y = 0;
  let width = 0;
  present.forEach((row, i) => {
    const ids = order[i] ?? [];
    for (let start = 0; start < ids.length; start += MAX_PER_ROW) {
      const line = ids.slice(start, start + MAX_PER_ROW);
      const sizes = line.map((id) => {
        const node = nodes.get(id);
        return node ? sizeOf(node) : { width: BOX_WIDTH, height: BOX_HEIGHT };
      });
      const total = sizes.reduce((sum, s) => sum + s.width, 0) + COL_GAP * (line.length - 1);
      const height = Math.max(...sizes.map((s) => s.height));
      let x = -total / 2;
      line.forEach((id, k) => {
        const size = sizes[k] ?? { width: BOX_WIDTH, height: BOX_HEIGHT };
        at[id] = { x, y: y + (height - size.height) / 2 };
        x += size.width + COL_GAP;
      });
      rows.push({ row, y, height, ids: line });
      width = Math.max(width, total);
      y += height + ROW_GAP;
    }
  });
  return { at, rows, width, height: Math.max(0, y - ROW_GAP) };
}

/** Where a line leaves and reaches its boxes: down to the next row, from a bottom to a top;
 * within a row, side to side; past a row (or up), round the side the box it reaches is on, so
 * it never crosses a box between. */
export function sidesOf(
  placed: PlacedSystem,
  from: string,
  to: string,
): { sourceHandle: string; targetHandle: string } {
  const a = placed.at[from];
  const b = placed.at[to];
  if (!a || !b) return { sourceHandle: "bottom", targetHandle: "top" };
  const row = (id: string) => placed.rows.findIndex((r) => r.ids.includes(id));
  const [ra, rb] = [row(from), row(to)];
  const right = b.x > a.x;
  if (ra === rb) {
    return right
      ? { sourceHandle: "to-right", targetHandle: "left" }
      : { sourceHandle: "to-left", targetHandle: "right" };
  }
  if (rb === ra + 1) return { sourceHandle: "bottom", targetHandle: "top" };
  return right || b.x === a.x
    ? { sourceHandle: "to-right", targetHandle: "right" }
    : { sourceHandle: "to-left", targetHandle: "left" };
}
