/**
 * A diagram's layout (6.6): ELK's layered algorithm, run in a Web Worker so a big diagram never
 * blocks the page (in the page itself where there are no workers: tests, old browsers). Nodes
 * flow left to right (or top to bottom). The architecture view (6.6.2) is drawn as an entity
 * diagram: a band per tier (frontend, API, services, data), each holding its parts' cards, as
 * tall as the members they list.
 */
import type { ELK, ElkNode } from "elkjs/lib/elk-api";

import { stepOrder, type Spec, type SpecEdge, type SpecNode } from "@/lib/api/diagrams";

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 76;
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
      ...(partitioned ? { "elk.partitioning.activate": "true" } : {}),
    },
    children: nodes.map((node) => ({
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

// ------------------------------------------------------------------ the architecture view

export const ENTITY_WIDTH = 248;
const ENTITY_HEAD = 72; // its layer, name and file
export const MEMBER_ROW = 20;
const ENTITY_FOOT = 10;
export const BAND_TOP = 40; // room for a band's name
const BAND_PAD = 16;

/** An entity card's height: its head, and a row per member listed (and one for "+N more"). */
export function entityHeight(node: SpecNode): number {
  const rows = (node.members?.length ?? 0) + ((node.more_members ?? 0) > 0 ? 1 : 0);
  return ENTITY_HEAD + (rows > 0 ? rows * MEMBER_ROW + ENTITY_FOOT : 0);
}

/** The architecture's tiers, in order, each with the trace layers it gathers and the layer whose
 * glyph and color it is drawn in. */
export const TIERS = [
  { tier: "frontend", label: "Frontend", lane: "ui", layers: ["ui", "state", "client"] },
  { tier: "api", label: "API", lane: "route", layers: ["route", "handler"] },
  { tier: "services", label: "Services", lane: "service", layers: ["service"] },
  { tier: "data", label: "Data", lane: "db", layers: ["db"] },
  { tier: "other", label: "Other", lane: "other", layers: [] as string[] },
] as const;
export type Tier = (typeof TIERS)[number];

/** A trace layer's tier (anything unknown is "other"). */
export function tierOf(layer: string): Tier {
  return TIERS.find((tier) => (tier.layers as readonly string[]).includes(layer)) ?? TIERS[4];
}

/** The tiers a diagram has, in their order (frontend first, data last). */
export function bandsOf(spec: Spec): Tier[] {
  const present = new Set(spec.nodes.map((node) => tierOf(node.layer).tier));
  return TIERS.filter((tier) => present.has(tier.tier));
}

export interface Band {
  tier: Tier;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface PlacedArchitecture {
  bands: Band[];
  /** Each card's top-left corner inside its band. */
  inside: Record<string, Placed>;
  /** And on the whole diagram (for the exports). */
  absolute: Record<string, Placed>;
}

const BAND_GAP = 96; // between two layers' bands: room for the lines across
const CARD_GAP = 20; // between two cards in a band

/** The entity diagram laid out as one reads it: a column per tier, frontend first and data last, each
 * band holding its parts' cards one under another (in ``order``: the flow's), the bands as tall
 * as the tallest. No layout engine: the same diagram is always drawn the same way. */
export function layoutArchitecture(
  spec: Spec,
  order: ReadonlyMap<string, number> = new Map(),
): PlacedArchitecture {
  const out: PlacedArchitecture = { bands: [], inside: {}, absolute: {} };
  const width = ENTITY_WIDTH + 2 * BAND_PAD;
  let x = 0;
  let tallest = 0;
  for (const tier of bandsOf(spec)) {
    const cards = spec.nodes
      .filter((node) => tierOf(node.layer).tier === tier.tier)
      .sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0));
    let y = BAND_TOP;
    for (const node of cards) {
      out.inside[node.id] = { x: BAND_PAD, y };
      out.absolute[node.id] = { x: x + BAND_PAD, y };
      y += entityHeight(node) + CARD_GAP;
    }
    const height = y - CARD_GAP + BAND_PAD;
    tallest = Math.max(tallest, height);
    out.bands.push({ tier, x, y: 0, width, height });
    x += width + BAND_GAP;
  }
  for (const band of out.bands) band.height = tallest;
  return out;
}
