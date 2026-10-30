"use client";

/**
 * The repository's modules as a map (4.13): a node per module, in a column by the layer most of
 * its files are in (UI, API, service, data, the rest), with a bar of its files by layer; a line
 * from module to module where the graph has code depending across them, wider for more edges. A
 * click opens the module in the code browser. React Flow, loaded with the overview in its own
 * chunk; the table beside it says the same for keyboards and screen readers.
 */
import "@xyflow/react/dist/style.css";

import {
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import { cn } from "cn";
import { useEffect, useRef, type CSSProperties } from "react";

import {
  layoutModules,
  linkWidth,
  MAP_LAYERS,
  moduleLabel,
  type MapLayer,
  type Module,
  type ModuleLink,
} from "@/lib/api/overview";

const FILL: Record<MapLayer, string> = {
  ui: "bg-layer-ui",
  api: "bg-layer-api",
  svc: "bg-layer-svc",
  db: "bg-layer-db",
  other: "bg-fg-faint",
};

const BORDER: Record<MapLayer, string> = {
  ui: "border-layer-ui/60",
  api: "border-layer-api/60",
  svc: "border-layer-svc/60",
  db: "border-layer-db/60",
  other: "border-line-strong",
};

interface ModuleData extends Record<string, unknown> {
  module: Module;
  layer: MapLayer;
}

function ModuleNode({ data }: NodeProps<Node<ModuleData>>) {
  const { module, layer } = data;
  return (
    <div
      className={cn(
        "flex w-48 cursor-pointer flex-col gap-1.5 rounded-md border bg-surface-2 px-2.5 py-2 hover:bg-surface-3",
        BORDER[layer],
      )}
      title={module.path}
    >
      <Handle type="target" position={Position.Left} className="opacity-0" />
      <span className="flex items-baseline justify-between gap-2">
        <span className="truncate font-mono text-xs text-fg">{moduleLabel(module.path)}</span>
        <span className="shrink-0 text-[11px] text-fg-muted tabular-nums">{module.files}</span>
      </span>
      <span className="flex h-1.5 overflow-hidden rounded-full bg-surface-1">
        {MAP_LAYERS.map((l) =>
          module.layers[l] ? (
            <span
              key={l}
              className={FILL[l]}
              style={{ width: `${String((100 * (module.layers[l] ?? 0)) / module.files)}%` }}
            />
          ) : null,
        )}
      </span>
      <Handle type="source" position={Position.Right} className="opacity-0" />
    </div>
  );
}

const NODE_TYPES: NodeTypes = { module: ModuleNode };

export function ArchitectureMap({
  modules,
  links,
  onOpen,
}: {
  modules: readonly Module[];
  links: readonly ModuleLink[];
  /** A module was clicked: open it. */
  onOpen: (module: Module) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  // The map is hidden from assistive tech, so nothing in it may take focus: React Flow's
  // attribution link stays on screen, out of the tab order.
  useEffect(() => {
    for (const link of box.current?.querySelectorAll("a") ?? []) link.tabIndex = -1;
  });
  const placed = layoutModules(modules);
  const nodes: Node<ModuleData>[] = placed.map(({ module, layer, x, y }) => ({
    id: module.path,
    type: "module",
    position: { x, y },
    data: { module, layer },
  }));
  const most = Math.max(1, ...links.map((link) => link.edges));
  const edges: Edge[] = links.map((link) => ({
    id: `${link.source}->${link.target}`,
    source: link.source,
    target: link.target,
    markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
    style: { stroke: "var(--color-line-strong)", strokeWidth: linkWidth(link.edges, most) },
  }));
  return (
    <div
      ref={box}
      className="h-96 rounded-md border border-line bg-surface-1"
      // Hidden from assistive tech: the table beside it carries the same (and is reachable).
      aria-hidden
      data-testid="architecture-map"
      style={
        {
          "--xy-background-color": "transparent",
          "--xy-node-background-color": "transparent",
          "--xy-attribution-background-color": "transparent",
        } as CSSProperties
      }
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.12 }}
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
          onOpen(node.data.module);
        }}
      />
    </div>
  );
}
