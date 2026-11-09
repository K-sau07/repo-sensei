"use client";

/**
 * A diagram's canvas (6.6): its nodes as blueprint cards on a dot grid, laid out by ELK (off the
 * main thread), its edges solid where the graph proves them and dashed amber where they are
 * inferred, each with its label. A floating toolbar fits the view, zooms, turns the flow (left
 * to right or top to bottom) and exports it (SVG, PNG, Mermaid); a minimap shows where the view
 * is. A click on a node picks it (its code opens beside the canvas).
 */
import "@xyflow/react/dist/style.css";

import {
  Background,
  BackgroundVariant,
  MarkerType,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeTypes,
} from "@xyflow/react";
import { ArrowDown, ArrowRight, Download, Maximize, Minus, Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Spec, SpecEdge, SpecNode } from "@/lib/api/diagrams";
import {
  elkGraph,
  layout,
  positionsOf,
  type FlowDirection,
  type Placed,
} from "@/lib/diagrams/elk-layout";
import { exportPng, exportSvg, download } from "@/lib/diagrams/export";

import { BlueprintNode, laneOf, type BlueprintData } from "./blueprint-node";

const NODE_TYPES: NodeTypes = { blueprint: BlueprintNode };
const VERIFIED = "var(--text-muted)";
const INFERRED = "var(--warn)";

export function edgeStyle(edge: SpecEdge) {
  return edge.verified
    ? { stroke: VERIFIED, strokeWidth: 1.5 }
    : { stroke: INFERRED, strokeWidth: 1.5, strokeDasharray: "6 4" };
}

/** The spec laid out: each node's place (none until ELK is done). */
export function useLaidOut(
  spec: Spec,
  edges: readonly SpecEdge[],
  direction: FlowDirection,
): Record<string, Placed> | null {
  const [placed, setPlaced] = useState<Record<string, Placed> | null>(null);
  const key = useMemo(
    () =>
      JSON.stringify([spec.nodes.map((n) => n.id), edges.map((e) => [e.from, e.to]), direction]),
    [spec.nodes, edges, direction],
  );
  useEffect(() => {
    let live = true;
    void layout(elkGraph(spec, edges, direction)).then(
      (laid) => {
        if (live) setPlaced(positionsOf(laid));
      },
      () => {
        if (live) setPlaced({});
      },
    );
    return () => {
      live = false;
    };
    // The key says when the layout's input changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return placed;
}

interface CanvasProps {
  spec: Spec;
  edges: readonly SpecEdge[];
  mermaid: string;
  title: string;
  selected: string | null;
  onSelect: (node: SpecNode | null) => void;
  added?: ReadonlySet<string> | undefined;
  /** Which way it is laid out first (left to right unless said). */
  initialDirection?: FlowDirection;
}

export function DiagramCanvas(props: CanvasProps) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}

function Canvas({
  spec,
  edges,
  mermaid,
  title,
  selected,
  onSelect,
  added,
  initialDirection = "RIGHT",
}: CanvasProps) {
  const [direction, setDirection] = useState<FlowDirection>(initialDirection);
  const placed = useLaidOut(spec, edges, direction);
  const flow = useReactFlow();
  const vertical = direction === "DOWN";
  const nodes: Node<BlueprintData>[] = useMemo(
    () =>
      spec.nodes.map((node) => ({
        id: node.id,
        type: "blueprint",
        position: placed?.[node.id] ?? { x: 0, y: 0 },
        data: { node, added: added?.has(node.id) ?? false, vertical },
        selected: node.id === selected,
        draggable: false,
        connectable: false,
      })),
    [spec.nodes, placed, added, vertical, selected],
  );
  const lines: Edge[] = useMemo(
    () =>
      edges.map((edge, i) => ({
        id: `e${String(i)}`,
        source: edge.from,
        target: edge.to,
        label: edge.label ?? (edge.verified ? undefined : edge.inferred?.reason),
        style: edgeStyle(edge),
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: edge.verified ? VERIFIED : INFERRED,
        },
        labelStyle: { fill: "var(--text-muted)", fontSize: 11 },
        labelBgStyle: { fill: "var(--surface-1)" },
        data: { verified: edge.verified },
        className: edge.verified ? "verified" : "inferred",
      })),
    [edges],
  );
  useEffect(() => {
    // Again whenever the layout changes (turned), once the nodes are drawn where it put them.
    if (!placed) return;
    const frame = requestAnimationFrame(() => void flow.fitView({ padding: 0.15 }));
    return () => {
      cancelAnimationFrame(frame);
    };
  }, [placed, flow]);

  const exports = {
    svg: () => {
      if (placed) download(`${title}.svg`, exportSvg(spec, edges, placed), "image/svg+xml");
    },
    png: () => {
      if (placed)
        void exportPng(spec, edges, placed).then((blob) => {
          download(`${title}.png`, blob);
        });
    },
    mermaid: () => {
      download(`${title}.mmd`, mermaid, "text/plain");
    },
  };

  if (placed === null) {
    // Laid out first: drawn before ELK has placed them, every node would sit on the first.
    return (
      <div
        className="flex h-full min-h-[28rem] w-full items-center justify-center text-sm text-fg-muted"
        role="status"
      >
        Laying out the diagram…
      </div>
    );
  }
  return (
    <div className="relative h-full min-h-[28rem] w-full" data-testid="diagram-canvas">
      <ReactFlow
        nodes={nodes}
        edges={lines}
        nodeTypes={NODE_TYPES}
        onNodeClick={(_, node) => {
          onSelect(spec.nodes.find((n) => n.id === node.id) ?? null);
        }}
        onPaneClick={() => {
          onSelect(null);
        }}
        nodesDraggable={false}
        nodesConnectable={false}
        // An edge does nothing when focused (the outline lists every one): Tab goes node to node.
        edgesFocusable={false}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        proOptions={{ hideAttribution: true }}
        minZoom={0.1}
        aria-label="Diagram"
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--line-strong)" />
        <MiniMap
          pannable
          zoomable
          nodeColor={(node) => laneOf((node.data as BlueprintData).node.layer).color}
          className="!bg-surface-2"
          // What is outside the view, dimmed in the theme's own background (React Flow's default
          // is a light grey, a pale block on the dark theme).
          maskColor="color-mix(in oklch, var(--bg) 65%, transparent)"
          ariaLabel="Minimap"
        />
      </ReactFlow>
      <div
        className="absolute top-3 left-1/2 flex -translate-x-1/2 items-center gap-0.5 rounded-lg border border-line bg-surface-1/80 p-1 shadow-sm backdrop-blur"
        role="toolbar"
        aria-label="Diagram tools"
      >
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label={vertical ? "Flow left to right" : "Flow top to bottom"}
          onClick={() => {
            setDirection(vertical ? "RIGHT" : "DOWN");
          }}
        >
          {vertical ? <ArrowRight aria-hidden /> : <ArrowDown aria-hidden />}
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Fit to view"
          onClick={() => void flow.fitView({ padding: 0.15 })}
        >
          <Maximize aria-hidden />
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Zoom in"
          onClick={() => void flow.zoomIn()}
        >
          <Plus aria-hidden />
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Zoom out"
          onClick={() => void flow.zoomOut()}
        >
          <Minus aria-hidden />
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button size="icon-sm" variant="ghost" aria-label="Export" />}
          >
            <Download aria-hidden />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={exports.svg}>SVG</DropdownMenuItem>
            <DropdownMenuItem onClick={exports.png}>PNG</DropdownMenuItem>
            <DropdownMenuItem onClick={exports.mermaid}>Mermaid</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
