"use client";

/**
 * A diagram's canvas (6.6), in the view asked for. A flow (its checked spec) is drawn in one of
 * three views:
 * - **flowchart**: its steps top to bottom, numbered in reading order, where it starts and ends;
 * - **sequence**: who takes part as columns with lifelines, the messages numbered down the page
 *   (sequence-view.tsx);
 * - **architecture**: a system diagram, as drawn on a whiteboard: who uses it at the top, then
 *   its frontend, API, services and data, the databases and services outside it at the bottom,
 *   each part a box with its icon (a store a cylinder), the lines square (system-node.tsx);
 * and an ER diagram (a schema's) as its tables' cards, each relation from its key's row to the
 * column it names, its ends crow's feet (table-node.tsx).
 * Flowcharts and ER diagrams are laid out by ELK (off the main thread), the architecture by its
 * rows; edges solid where the graph proves them and dashed amber where they are inferred. A
 * floating toolbar fits the view, zooms, turns a flowchart, and exports (SVG, PNG, Mermaid); a
 * minimap shows where the view is. A click on a node picks it (it opens beside the canvas).
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
import type { ElkNode } from "elkjs/lib/elk-api";
import { ArrowDown, ArrowRight, Download, Maximize, Minus, Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  flowEnds,
  stepOrder,
  type DiagramKind,
  type Spec,
  type SpecEdge,
  type SpecNode,
} from "@/lib/api/diagrams";
import {
  elkGraph,
  layout,
  positionsOf,
  type FlowDirection,
  type Placed,
} from "@/lib/diagrams/elk-layout";
import { erGraph, relationEnds } from "@/lib/diagrams/er-layout";
import { layoutSystem, sidesOf } from "@/lib/diagrams/system-layout";

import { BlueprintNode, laneOf, type BlueprintData } from "./blueprint-node";
import {
  RowLabelNode,
  SystemNode,
  TableNode,
  type RowLabelData,
  type SystemData,
  type TableData,
} from "./diagram-nodes";
import { RelationMarkers, relationMarkers } from "./er-markers";
import { SequenceView } from "./sequence-view";
import { colorOf } from "./system-color";

const NODE_TYPES: NodeTypes = {
  blueprint: BlueprintNode,
  system: SystemNode,
  row: RowLabelNode,
  table: TableNode,
};
// The exports are loaded when one is asked for: no page pays for them until then.
const exporting = () => import("@/lib/diagrams/export");
const VERIFIED = "var(--text-muted)";
const INFERRED = "var(--warn)";
const ROW_LABEL_WIDTH = 120;
const ROW_LABEL_GAP = 48;
const MINIMAP_FROM = 12; // nodes

export function edgeStyle(edge: SpecEdge) {
  return edge.verified
    ? { stroke: VERIFIED, strokeWidth: 1.5 }
    : { stroke: INFERRED, strokeWidth: 1.5, strokeDasharray: "6 4" };
}

/** A graph laid out by ELK: each node's place (none until it is done). ``key`` says when the
 * input changed. */
function useElk(key: string, graph: () => ElkNode, on: boolean): Record<string, Placed> | null {
  const [placed, setPlaced] = useState<Record<string, Placed> | null>(null);
  useEffect(() => {
    if (!on) return;
    let live = true;
    void layout(graph()).then(
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
  }, [key, on]);
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
  /** Which view (the diagram's own kind unless said). */
  view?: DiagramKind | undefined;
  /** Which way a flowchart is laid out first (top to bottom unless said). */
  initialDirection?: FlowDirection;
}

export function DiagramCanvas(props: CanvasProps) {
  const view = props.view ?? props.spec.kind;
  if (view === "sequence") return <Sequence {...props} />;
  return (
    <ReactFlowProvider>
      <Canvas {...props} view={view} />
    </ReactFlowProvider>
  );
}

function Sequence({ spec, edges, mermaid, title, selected, onSelect, added }: CanvasProps) {
  return (
    <div className="relative h-full min-h-[28rem] w-full" data-testid="diagram-canvas">
      <SequenceView
        spec={spec}
        edges={edges}
        selected={selected}
        onSelect={onSelect}
        added={added}
      />
      <div
        className="absolute top-3 right-3 flex items-center gap-0.5 rounded-lg border border-line bg-surface-1/80 p-1 shadow-sm backdrop-blur"
        role="toolbar"
        aria-label="Diagram tools"
      >
        <ExportMenu
          svg={() =>
            void exporting().then(({ download, exportSequenceSvg }) => {
              download(`${title}.svg`, exportSequenceSvg(spec, edges), "image/svg+xml");
            })
          }
          png={() =>
            void exporting().then(async ({ download, exportSequenceSvg, svgToPng }) => {
              download(`${title}.png`, await svgToPng(exportSequenceSvg(spec, edges)));
            })
          }
          mermaid={() =>
            void exporting().then(({ download }) => {
              download(`${title}.mmd`, mermaid, "text/plain");
            })
          }
        />
      </div>
    </div>
  );
}

function ExportMenu({
  svg,
  png,
  mermaid,
}: {
  svg: () => void;
  png: () => void;
  mermaid: () => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button size="icon-sm" variant="ghost" aria-label="Export" />}>
        <Download aria-hidden />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onClick={svg}>SVG</DropdownMenuItem>
        <DropdownMenuItem onClick={png}>PNG</DropdownMenuItem>
        <DropdownMenuItem onClick={mermaid}>Mermaid</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

const FIXED = { draggable: false, connectable: false } as const;

/** The architecture view's nodes and lines (see the module). */
function useSystem(
  spec: Spec,
  edges: readonly SpecEdge[],
  on: boolean,
  added?: ReadonlySet<string>,
) {
  const placed = useMemo(() => (on ? layoutSystem(spec, edges) : null), [on, spec, edges]);
  const nodes = useMemo((): Node[] => {
    if (!placed) return [];
    const left = -placed.width / 2 - ROW_LABEL_WIDTH - ROW_LABEL_GAP;
    const labels: Node<RowLabelData>[] = placed.rows
      .filter((row, i, all) => all.findIndex((r) => r.row.row === row.row.row) === i)
      .map((row) => ({
        id: `row:${row.row.row}`,
        type: "row",
        position: { x: left, y: row.y },
        data: { row: row.row, height: row.height },
        selectable: false,
        focusable: false,
        ...FIXED,
      }));
    const boxes: Node<SystemData>[] = spec.nodes.map((node) => ({
      id: node.id,
      type: "system",
      position: placed.at[node.id] ?? { x: 0, y: 0 },
      data: { node, added: added?.has(node.id) ?? false },
      ...FIXED,
    }));
    return [...labels, ...boxes];
  }, [placed, spec.nodes, added]);
  const lines = useMemo(
    (): Edge[] =>
      edges.map((edge, i) => ({
        id: `e${String(i)}`,
        source: edge.from,
        target: edge.to,
        ...(placed ? sidesOf(placed, edge.from, edge.to) : {}),
        type: "smoothstep",
        pathOptions: { borderRadius: 10, offset: 18 },
        label: edge.label ?? (edge.verified ? undefined : edge.inferred?.reason),
        style: edgeStyle(edge),
        markerEnd: { type: MarkerType.ArrowClosed, color: edge.verified ? VERIFIED : INFERRED },
        labelStyle: { fill: "var(--text-muted)", fontSize: 10 },
        labelBgStyle: { fill: "var(--surface-1)" },
        className: edge.verified ? "verified" : "inferred",
      })),
    [edges, placed],
  );
  return { placed: placed?.at ?? null, system: placed, nodes, lines };
}

/** The ER view's tables and relations. */
function useEr(spec: Spec, edges: readonly SpecEdge[], on: boolean, added?: ReadonlySet<string>) {
  const key = useMemo(
    () =>
      JSON.stringify([
        spec.nodes.map((n) => [n.id, n.members?.length]),
        edges.map((e) => [e.from, e.to]),
      ]),
    [spec.nodes, edges],
  );
  const placed = useElk(key, () => erGraph(spec, edges), on);
  const byId = useMemo(() => new Map(spec.nodes.map((n) => [n.id, n])), [spec.nodes]);
  const nodes = useMemo(
    (): Node<TableData>[] =>
      spec.nodes.map((node) => ({
        id: node.id,
        type: "table",
        position: placed?.[node.id] ?? { x: 0, y: 0 },
        data: { node, added: added?.has(node.id) ?? false },
        ...FIXED,
      })),
    [placed, spec.nodes, added],
  );
  const lines = useMemo(
    (): Edge[] =>
      edges.map((edge, i) => {
        const ends = relationEnds(edge, byId.get(edge.to));
        const column = ends.sourceHandle.slice("out:".length);
        const key = byId.get(edge.from)?.members?.find((m) => m.name === column);
        return {
          id: `r${String(i)}`,
          source: edge.from,
          target: edge.to,
          ...ends,
          type: "smoothstep",
          pathOptions: { borderRadius: 8, offset: 24 },
          ...relationMarkers(edge.type, key?.nullable ?? false),
          style: { stroke: "var(--text-muted)", strokeWidth: 1.5 },
          className: "relation",
        };
      }),
    [edges, byId],
  );
  return { placed, nodes, lines };
}

/** The flowchart's steps and lines. */
function useFlow(
  spec: Spec,
  edges: readonly SpecEdge[],
  direction: FlowDirection,
  on: boolean,
  added?: ReadonlySet<string>,
) {
  const key = useMemo(
    () =>
      JSON.stringify([spec.nodes.map((n) => n.id), edges.map((e) => [e.from, e.to]), direction]),
    [spec.nodes, edges, direction],
  );
  const placed = useElk(key, () => elkGraph(spec, edges, direction), on);
  const steps = useMemo(() => stepOrder(spec, edges), [spec, edges]);
  const ends = useMemo(() => flowEnds(spec, edges), [spec, edges]);
  const vertical = direction === "DOWN";
  const nodes = useMemo(
    (): Node<BlueprintData>[] =>
      spec.nodes.map((node) => ({
        id: node.id,
        type: "blueprint",
        position: placed?.[node.id] ?? { x: 0, y: 0 },
        data: {
          node,
          added: added?.has(node.id) ?? false,
          vertical,
          step: steps.get(node.id),
          end: ends.get(node.id),
        },
        ...FIXED,
      })),
    [placed, spec.nodes, added, vertical, steps, ends],
  );
  const lines = useMemo(
    (): Edge[] =>
      edges.map((edge, i) => ({
        id: `e${String(i)}`,
        source: edge.from,
        target: edge.to,
        label: edge.label ?? (edge.verified ? undefined : edge.inferred?.reason),
        style: edgeStyle(edge),
        markerEnd: { type: MarkerType.ArrowClosed, color: edge.verified ? VERIFIED : INFERRED },
        labelStyle: { fill: "var(--text-muted)", fontSize: 11 },
        labelBgStyle: { fill: "var(--surface-1)" },
        className: edge.verified ? "verified" : "inferred",
      })),
    [edges],
  );
  return { placed, nodes, lines };
}

function Canvas({
  spec,
  edges,
  mermaid,
  title,
  selected,
  onSelect,
  added,
  view = "flowchart",
  initialDirection = "DOWN",
}: CanvasProps) {
  const architecture = view === "architecture";
  const er = view === "er";
  const [direction, setDirection] = useState<FlowDirection>(initialDirection);
  const system = useSystem(spec, edges, architecture, added);
  const schema = useEr(spec, edges, er, added);
  const flowchart = useFlow(spec, edges, direction, !architecture && !er, added);
  const shown = architecture ? system : er ? schema : flowchart;
  const placed = shown.placed;
  const flow = useReactFlow();
  const vertical = direction === "DOWN";
  const nodes = useMemo(
    () => shown.nodes.map((node) => (node.id === selected ? { ...node, selected: true } : node)),
    [shown.nodes, selected],
  );
  useEffect(() => {
    // Again whenever the layout changes (turned), once the nodes are drawn where it put them.
    if (!placed) return;
    const frame = requestAnimationFrame(() => void flow.fitView({ padding: 0.15 }));
    return () => {
      cancelAnimationFrame(frame);
    };
  }, [placed, flow]);

  const svgOf = async (at: Readonly<Record<string, Placed>>) => {
    const made = await exporting();
    if (architecture && system.system) return made.exportSystemSvg(spec, edges, system.system);
    if (er) return made.exportErSvg(spec, edges, at);
    return made.exportSvg(spec, edges, at);
  };
  const exports = {
    svg: () => {
      if (placed)
        void Promise.all([exporting(), svgOf(placed)]).then(([{ download }, svg]) => {
          download(`${title}.svg`, svg, "image/svg+xml");
        });
    },
    png: () => {
      if (placed)
        void Promise.all([exporting(), svgOf(placed)]).then(
          async ([{ download, svgToPng }, svg]) => {
            download(`${title}.png`, await svgToPng(svg));
          },
        );
    },
    mermaid: () => {
      void exporting().then(({ download }) => {
        download(`${title}.mmd`, mermaid, "text/plain");
      });
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
      {er ? <RelationMarkers /> : null}
      <ReactFlow
        nodes={nodes}
        edges={shown.lines}
        nodeTypes={NODE_TYPES}
        onNodeClick={(_, node) => {
          if (node.type === "row") return;
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
        {/* A small diagram is all in view: no minimap over its boxes. */}
        {spec.nodes.length > MINIMAP_FROM ? (
          <MiniMap
            pannable
            zoomable
            nodeColor={(node) => {
              if (node.type === "row") return "transparent";
              const held = (node.data as { node: SpecNode }).node;
              if (node.type === "system") return colorOf(held);
              if (node.type === "table") return laneOf("db").color;
              return laneOf(held.layer).color;
            }}
            className="!bg-surface-2"
            // What is outside the view, dimmed in the theme's own background (React Flow's default
            // is a light grey, a pale block on the dark theme).
            maskColor="color-mix(in oklch, var(--bg) 65%, transparent)"
            ariaLabel="Minimap"
          />
        ) : null}
      </ReactFlow>
      <div
        className="absolute top-3 left-1/2 flex -translate-x-1/2 items-center gap-0.5 rounded-lg border border-line bg-surface-1/80 p-1 shadow-sm backdrop-blur"
        role="toolbar"
        aria-label="Diagram tools"
      >
        {architecture || er ? null : (
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
        )}
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
        <ExportMenu svg={exports.svg} png={exports.png} mermaid={exports.mermaid} />
      </div>
    </div>
  );
}
