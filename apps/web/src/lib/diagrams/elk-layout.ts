/**
 * A diagram's layout (6.6): ELK's layered algorithm, run in a Web Worker so a big diagram never
 * blocks the page (in the page itself where there are no workers: tests, old browsers). A
 * flowchart's steps flow top to bottom (or left to right); an ER diagram's tables are laid out
 * by it too (er-layout.ts). The architecture view is laid out by its rows (system-layout.ts).
 */
import type { ELK, ElkNode } from "elkjs/lib/elk-api";

import { stepOrder, type Spec, type SpecEdge } from "@/lib/api/diagrams";

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 76;
export type FlowDirection = "RIGHT" | "DOWN";

export interface Placed {
  x: number;
  y: number;
}

export function elkGraph(
  spec: Spec,
  edges: readonly SpecEdge[],
  direction: FlowDirection,
): ElkNode {
  // In reading order (from the flow's start, along its edges), and kept in it: a loop back (a
  // response, an inferred return) is the edge that is turned around, not the flow's first steps.
  const order = stepOrder(spec, edges);
  const nodes = [...spec.nodes].sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
  const place = (id: string) => order.get(id) ?? 0;
  const ordered = [...edges].sort((a, b) => place(a.from) - place(b.from));
  return {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": direction,
      "elk.layered.spacing.nodeNodeBetweenLayers": "72",
      "elk.spacing.nodeNode": "36",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      "elk.layered.cycleBreaking.strategy": "MODEL_ORDER",
      "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
      "elk.randomSeed": "7",
    },
    children: nodes.map((node) => ({ id: node.id, width: NODE_WIDTH, height: NODE_HEIGHT })),
    edges: ordered.map((edge, i) => ({
      id: `e${String(i)}`,
      sources: [edge.from],
      targets: [edge.to],
    })),
  };
}

/** Each node's top-left corner, from a laid-out graph. */
export function positionsOf(laid: ElkNode): Record<string, Placed> {
  const out: Record<string, Placed> = {};
  for (const child of laid.children ?? []) {
    out[child.id] = { x: child.x ?? 0, y: child.y ?? 0 };
  }
  return out;
}

type Engine = ELK;

let engine: Promise<Engine> | null = null;

/** ELK: its layout in a Web Worker where there are workers (elkjs's API on this side, its
 * worker script on the other), else in the page (tests, old browsers). */
function elk(): Promise<Engine> {
  engine ??= (async () => {
    if (typeof window !== "undefined" && typeof Worker !== "undefined") {
      try {
        const { default: Api } = await import("elkjs/lib/elk-api");
        return new Api({
          workerFactory: () =>
            new Worker(new URL("./elk.worker.ts", import.meta.url), {
              type: "module",
              name: "diagram-layout",
            }),
        });
      } catch {
        // No worker after all: in the page.
      }
    }
    const { default: Bundled } = await import("elkjs/lib/elk.bundled.js");
    return new Bundled();
  })();
  return engine;
}

/** The graph laid out (off the main thread when it can be). */
export async function layout(graph: ElkNode): Promise<ElkNode> {
  return (await elk()).layout(graph);
}
