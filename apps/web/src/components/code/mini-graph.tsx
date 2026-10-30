"use client";

/**
 * A symbol's callers and callees, Sourcetrail's way: the symbol in the middle, what calls it on
 * the left, what it calls on the right; a click on one re-centres on it. React Flow, loaded with
 * the rail (in its own chunk); the lists below it say the same for keyboards and screen readers.
 */
import "@xyflow/react/dist/style.css";

import {
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import { cn } from "cn";
import { useEffect, useRef, type CSSProperties } from "react";

import { LAYERS } from "@/components/primitives/layer-badge";
import { badgeLayer, provenanceTag, type GraphHop, type GraphNode } from "@/lib/api/graph";

/** At most this many on each side; the rest are a count. */
export const SIDE_MAX = 6;

const LANE_TEXT = {
  ui: "text-layer-ui",
  api: "text-layer-api",
  svc: "text-layer-svc",
  db: "text-layer-db",
} as const;

const COLUMN = 210;
const ROW = 40;

interface SymbolData extends Record<string, unknown> {
  node: GraphNode;
  centre: boolean;
  more?: number;
}

function SymbolNode({ data }: NodeProps<Node<SymbolData>>) {
  const lane = badgeLayer(data.node.lane);
  return (
    <div
      className={cn(
        "flex h-8 w-44 items-center gap-1.5 rounded-md border px-2 font-mono text-xs",
        data.centre
          ? "border-accent bg-surface-3 text-fg"
          : "cursor-pointer border-line-strong bg-surface-2 text-fg-muted hover:text-fg",
      )}
      title={data.node.qualified_name}
    >
      <Handle type="target" position={Position.Left} className="opacity-0" />
      {lane && (
        <span aria-hidden className={cn("shrink-0", LANE_TEXT[lane])}>
          {LAYERS[lane].glyph}
        </span>
      )}
      <span className="truncate">{data.more ? `${String(data.more)} more…` : data.node.name}</span>
      <Handle type="source" position={Position.Right} className="opacity-0" />
    </div>
  );
}

const NODE_TYPES: NodeTypes = { symbol: SymbolNode };

function column(
  hops: readonly GraphHop[],
  x: number,
  side: "in" | "out",
): { nodes: Node<SymbolData>[]; edges: Edge[] } {
  const shown = hops.slice(0, SIDE_MAX);
  const rest = hops.length - shown.length;
  const count = shown.length + (rest > 0 ? 1 : 0);
  const top = (-(count - 1) * ROW) / 2;
  const nodes: Node<SymbolData>[] = shown.map((hop, i) => ({
    id: `${side}:${hop.node.symbol_key}`,
    type: "symbol",
    position: { x, y: top + i * ROW },
    data: { node: hop.node, centre: false },
  }));
  const edges: Edge[] = shown.map((hop) => {
    const tag = provenanceTag(hop.edge.provenance);
    const id = `${side}:${hop.node.symbol_key}`;
    return {
      id: `e-${id}`,
      source: side === "in" ? id : "centre",
      target: side === "in" ? "centre" : id,
      style: {
        stroke: "var(--color-line-strong)",
        strokeDasharray: tag === "precise" ? undefined : "4 3",
        opacity: 0.4 + 0.6 * hop.edge.confidence,
      },
    };
  });
  const last = hops[SIDE_MAX];
  if (rest > 0 && last) {
    nodes.push({
      id: `${side}:more`,
      type: "symbol",
      position: { x, y: top + shown.length * ROW },
      data: { node: last.node, centre: false, more: rest },
      selectable: false,
    });
  }
  return { nodes, edges };
}

export function MiniGraph({
  centre,
  callers,
  callees,
  onSelect,
}: {
  centre: GraphNode;
  callers: readonly GraphHop[];
  callees: readonly GraphHop[];
  /** A caller or callee was clicked: centre on it. */
  onSelect: (node: GraphNode) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  // Hidden from assistive tech, so nothing in it may take focus: React Flow's attribution link
  // stays on screen, out of the tab order.
  useEffect(() => {
    for (const link of box.current?.querySelectorAll("a") ?? []) link.tabIndex = -1;
  });
  const left = column(callers, -COLUMN, "in");
  const right = column(callees, COLUMN, "out");
  const nodes: Node<SymbolData>[] = [
    {
      id: "centre",
      type: "symbol",
      position: { x: 0, y: 0 },
      data: { node: centre, centre: true },
    },
    ...left.nodes,
    ...right.nodes,
  ];
  return (
    <div
      ref={box}
      className="h-64 rounded-md border border-line bg-surface-1"
      // Hidden from assistive tech: the lists beside it carry the same (and are reachable).
      aria-hidden
      data-testid="mini-graph"
      style={
        {
          "--xy-background-color": "transparent",
          "--xy-node-background-color": "transparent",
          "--xy-attribution-background-color": "transparent",
        } as CSSProperties
      }
    >
      <ReactFlow
        key={centre.symbol_key}
        nodes={nodes}
        edges={[...left.edges, ...right.edges]}
        nodeTypes={NODE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        nodesFocusable={false}
        edgesFocusable={false}
        zoomOnScroll={false}
        zoomOnDoubleClick={false}
        panOnScroll={false}
        minZoom={0.3}
        onNodeClick={(_, node) => {
          const { data } = node;
          if (!data.centre && !data.more) onSelect(data.node);
        }}
      />
    </div>
  );
}
