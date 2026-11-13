/** A diagram's focus (6.6): a picked box or line, and what it lights up on the canvas. */
import type { SpecEdge } from "@/lib/api/diagrams";

/** What a picked box or line lights up: itself, its lines and the boxes at their ends. */
export interface Focus {
  nodes: ReadonlySet<string>;
  edges: ReadonlySet<number>;
}

export function focusOf(
  edges: readonly SpecEdge[],
  node: string | null,
  edge: number | null,
): Focus | null {
  if (edge !== null && edges[edge]) {
    const picked = edges[edge];
    return { nodes: new Set([picked.from, picked.to]), edges: new Set([edge]) };
  }
  if (node === null) return null;
  const lit = new Set<number>();
  const near = new Set([node]);
  edges.forEach((e, i) => {
    if (e.from === node || e.to === node) {
      lit.add(i);
      near.add(e.from);
      near.add(e.to);
    }
  });
  return { nodes: near, edges: lit };
}
