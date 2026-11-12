/** An architecture box's color (6.6): its row's; an outside system's the data's for a store,
 * else the API's. Kept apart from the boxes, which load with their view. */
import type { SpecNode } from "@/lib/api/diagrams";
import { isStore, rowOf, type Row } from "@/lib/diagrams/system-layout";

import { laneOf } from "./blueprint-node";

export function colorOf(node: SpecNode, row: Row = rowOf(node)): string {
  if (node.layer === "outside") return isStore(node) ? laneOf("db").color : laneOf("client").color;
  return laneOf(row.lane).color;
}
