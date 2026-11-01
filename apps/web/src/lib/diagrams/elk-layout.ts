/**
 * A diagram's layout (6.6): ELK's layered algorithm, run in a Web Worker so a big diagram never
 * blocks the page (in the page itself where there are no workers: tests, old browsers). Nodes
 * flow left to right (or top to bottom); an architecture diagram's are partitioned into columns
 * by layer, UI first, data last.
 */
import type { ELK, ElkNode } from "elkjs/lib/elk-api";

import type { Spec, SpecEdge } from "@/lib/api/diagrams";

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 64;
export type FlowDirection = "RIGHT" | "DOWN";

const LAYER_ORDER = ["ui", "state", "client", "route", "handler", "service", "db", "other"];

/** An architecture diagram's column for a layer: UI first, data last, the rest after. */
export function layerColumn(layer: string): number {
  const at = LAYER_ORDER.indexOf(layer);
  return at < 0 ? LAYER_ORDER.length : at;
}

export interface Placed {
  x: number;
  y: number;
}

export function elkGraph(
  spec: Spec,
  edges: readonly SpecEdge[],
  direction: FlowDirection,
): ElkNode {
  const partitioned = spec.kind === "architecture";
  return {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": direction,
      "elk.layered.spacing.nodeNodeBetweenLayers": "72",
      "elk.spacing.nodeNode": "36",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      "elk.randomSeed": "7",
      ...(partitioned ? { "elk.partitioning.activate": "true" } : {}),
    },
    children: spec.nodes.map((node) => ({
      id: node.id,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      ...(partitioned
        ? {
            layoutOptions: {
              "elk.partitioning.partition": String(layerColumn(node.layer)),
            },
          }
        : {}),
    })),
    edges: edges.map((edge, i) => ({
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
