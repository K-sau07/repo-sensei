/**
 * An ER diagram's layout (6.6): a card per table, as tall as its columns, laid out by ELK's
 * layered algorithm left to right along the foreign keys (a table holding a key before the one
 * it names), the lines run square between them. Each relation is drawn from its key's row to
 * the row of the column it names.
 */
import type { ElkNode } from "elkjs/lib/elk-api";

import type { Spec, SpecEdge, SpecNode } from "@/lib/api/diagrams";

export const TABLE_WIDTH = 272;
export const TABLE_HEAD = 44;
export const COLUMN_ROW = 26;
const TABLE_FOOT = 8;

/** A table card's height: its head, a row per column (and one for "+N more"). */
export function tableHeight(node: SpecNode): number {
  const rows = (node.members?.length ?? 0) + ((node.more_members ?? 0) > 0 ? 1 : 0);
  return TABLE_HEAD + Math.max(1, rows) * COLUMN_ROW + TABLE_FOOT;
}

/** The handle a relation leaves from (its key's row) and the one it reaches (the named
 * column's row, else the table's head). */
export function relationEnds(edge: SpecEdge, target: SpecNode | undefined) {
  const column = (edge.label ?? "").split(" → ")[0] ?? "";
  const named = (edge.label ?? "").split(" → ")[1] ?? "";
  const reaches = target?.members?.some((m) => m.name === named) ? `in:${named}` : "in:head";
  return { sourceHandle: `out:${column}`, targetHandle: reaches };
}

export function erGraph(spec: Spec, edges: readonly SpecEdge[]): ElkNode {
  return {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.layered.spacing.nodeNodeBetweenLayers": "96",
      "elk.spacing.nodeNode": "40",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.randomSeed": "7",
    },
    children: spec.nodes.map((node) => ({
      id: node.id,
      width: TABLE_WIDTH,
      height: tableHeight(node),
    })),
    edges: edges
      .filter((edge) => edge.from !== edge.to)
      .map((edge, i) => ({ id: `r${String(i)}`, sources: [edge.from], targets: [edge.to] })),
  };
}
