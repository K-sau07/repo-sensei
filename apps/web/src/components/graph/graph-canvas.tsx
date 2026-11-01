"use client";

/**
 * The graph explorer's WebGL canvas (6.7): Sigma over a graphology graph of the level shown,
 * each node where the level's layout put it, colored by its layer and sized by its fan-in.
 * Loaded in the browser only (`next/dynamic`, `ssr: false`).
 *
 * - Hovering a node lights it and its neighbours and mutes the rest.
 * - A focus (a node and its neighbours some hops out) hides everything else; a path lights its
 *   nodes and edges; search hits are lit, the rest muted; edges that close a cycle are red.
 * - Zooming reports the camera's ratio and the nodes in view (semantic zoom opens them).
 * - A minimap shows the whole level and the view; clicking it moves there.
 */
import { SigmaContainer, useRegisterEvents, useSigma } from "@react-sigma/core";
import type Graph from "graphology";
import { Maximize2, Minus, Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { extremityArrow, layerPlain, pathLine, pathLoop } from "sigma/rendering";
import type { NodeDisplayData, EdgeDisplayData } from "sigma/types";

import { Button } from "@/components/ui/button";
import { edgeKey, tintOf, type DepEdge, type ShownNode } from "@/lib/api/deps";

import type { GraphColors } from "./graph-colors";

export interface GraphView {
  nodes: readonly ShownNode[];
  edges: readonly DepEdge[];
  sizes: ReadonlyMap<string, number>;
  colors: GraphColors;
  /** The focus's nodes: the rest are hidden. */
  focus: ReadonlySet<string> | null;
  /** Search hits: lit, the rest muted (when any). */
  found: ReadonlySet<string>;
  /** A path's nodes, in order: lit, with the edges between them. */
  path: readonly string[] | null;
  /** The edges that close a cycle, by key. */
  cycles: ReadonlySet<string>;
  selected: string | null;
}

export interface GraphCanvasProps {
  view: GraphView;
  label: string;
  /** No camera animations. */
  still: boolean;
  onSelectNode: (id: string | null) => void;
  onSelectEdge: (source: string, target: string) => void;
  /** The camera's zoom ratio (1: the whole level), the nodes in view and the one nearest the
   * view's center, as it settles. */
  onZoom?: (ratio: number, visible: string[], nearest: string | null) => void;
}

const BOX = 1.15; // the layouts are within [-1, 1]: a fixed frame keeps the camera still
const MUTED = 0.12;
const MAX_PICKED_EDGES = 5_000;
const ZOOM_SETTLE_MS = 150;
const MINIMAP = { width: 168, height: 120 };

interface NodeAttributes {
  x: number;
  y: number;
  size: number;
  label: string;
  color: string;
}
interface EdgeAttributes {
  size: number;
  color: string;
}

const OPTIONS = {
  primitives: {
    edges: {
      paths: [pathLine(), pathLoop()],
      extremities: [extremityArrow()],
      layers: [layerPlain()],
      defaultHead: "arrow",
    },
  },
};

interface Live {
  view: GraphView;
  hovered: string | null;
  near: ReadonlySet<string>;
  onPath: ReadonlySet<string>;
  pathEdges: ReadonlySet<string>;
}

export default function GraphCanvas(props: GraphCanvasProps) {
  const liveRef = useRef<Live>({
    view: props.view,
    hovered: null,
    near: new Set(),
    onPath: new Set(),
    pathEdges: new Set(),
  });
  const options = useMemo(
    () => ({
      ...OPTIONS,
      settings: {
        // Mounted in a tab panel still being laid out: sized when it is (see `Controller`).
        allowInvalidContainer: true,
        itemSizesReference: "screen" as const, // sizes in pixels (Sigma 4's default: layout units)
        // Picking 40,000 edges each frame costs a big level its smooth panning: there, an
        // edge's code is reached from its node's panel.
        enableEdgeEvents: props.view.edges.length <= MAX_PICKED_EDGES,
        enableNodeDrag: false,
        renderEdgeLabels: false,
        labelRenderedSizeThreshold: 7,
        minCameraRatio: 0.01,
        maxCameraRatio: 3,
        stagePadding: 24,
        zoomDuration: props.still ? 0 : 250,
        inertiaDuration: props.still ? 0 : 200,
      },
      nodeReducer: (key: string, data: NodeDisplayData): Partial<NodeDisplayData> =>
        nodeLook(liveRef.current, key, data),
      edgeReducer: (
        key: string,
        data: EdgeDisplayData,
        _attributes: unknown,
        _state: unknown,
        _graphState: unknown,
        graph: Graph,
      ): Partial<EdgeDisplayData> => edgeLook(liveRef.current, key, data, graph),
    }),
    // Made once: Sigma is rebuilt when its options change. The look reads `liveRef`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  return (
    <SigmaContainer
      options={options}
      // Sized here (the package's stylesheet, with its white theme, isn't used).
      className="relative size-full [&_.sigma-container]:size-full"
    >
      <Controller {...props} liveRef={liveRef} />
    </SigmaContainer>
  );
}

function nodeLook(live: Live, key: string, data: NodeDisplayData): Partial<NodeDisplayData> {
  const { view, hovered, near, onPath } = live;
  if (view.focus && !view.focus.has(key)) return { ...data, visibility: "hidden" };
  const lit =
    key === view.selected ||
    (hovered !== null && near.has(key)) ||
    onPath.has(key) ||
    view.found.has(key);
  const muted =
    (hovered !== null && !near.has(key)) ||
    (onPath.size > 0 && !onPath.has(key)) ||
    (view.found.size > 0 && !view.found.has(key) && hovered === null);
  return {
    ...data,
    opacity: muted ? MUTED : 1,
    labelColor: view.colors.label,
    labelVisibility: muted ? "hidden" : lit ? "visible" : "auto",
    highlighted: key === view.selected || key === hovered,
    zIndex: lit ? 2 : muted ? 0 : 1,
    cursor: "pointer",
  };
}

function edgeLook(
  live: Live,
  key: string,
  data: EdgeDisplayData,
  graph: Graph,
): Partial<EdgeDisplayData> {
  const { view, hovered, pathEdges } = live;
  const [source, target] = graph.extremities(key);
  if (view.focus && !(view.focus.has(source) && view.focus.has(target))) {
    return { ...data, visibility: "hidden" };
  }
  const touched = hovered !== null && (source === hovered || target === hovered);
  const onPath = pathEdges.has(key);
  const inCycle = view.cycles.has(key);
  const muted =
    (hovered !== null && !touched) ||
    (pathEdges.size > 0 && !onPath) ||
    (view.found.size > 0 &&
      hovered === null &&
      !(view.found.has(source) && view.found.has(target)));
  return {
    ...data,
    color: inCycle ? view.colors.cycle : touched || onPath ? view.colors.lit : data.color,
    size: onPath || touched ? data.size + 1 : data.size,
    opacity: muted ? MUTED / 2 : onPath || touched || inCycle ? 1 : 0.5,
    zIndex: onPath || touched ? 2 : inCycle ? 1 : 0,
    cursor: "pointer",
  };
}

function Controller({
  view,
  label,
  still,
  onSelectNode,
  onSelectEdge,
  onZoom,
  liveRef,
}: GraphCanvasProps & { liveRef: RefObject<Live> }) {
  const sigma = useSigma<NodeAttributes, EdgeAttributes>();
  const register = useRegisterEvents<NodeAttributes, EdgeAttributes>();

  // Its box changes (the panel beside it, the window, a tab shown): drawn to the new size.
  useEffect(() => {
    const container = sigma.getContainer();
    const observer = new ResizeObserver(() => {
      sigma.resize();
      sigma.refresh({ skipIndexation: true });
    });
    observer.observe(container);
    return () => {
      observer.disconnect();
    };
  }, [sigma]);

  // The level shown: drawn again when its nodes or edges change.
  useEffect(() => {
    const graph = sigma.getGraph();
    graph.clear();
    for (const node of view.nodes) {
      graph.addNode(node.id, {
        x: node.x,
        y: -node.y, // the layouts' y grows down, Sigma's up
        size: view.sizes.get(node.id) ?? 3,
        label: node.label,
        color: view.colors.tints[tintOf(node.layer)],
      });
    }
    for (const edge of view.edges) {
      if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
      const key = edgeKey(edge.source, edge.target);
      if (graph.hasEdge(key)) continue;
      graph.addDirectedEdgeWithKey(key, edge.source, edge.target, {
        size: Math.min(4, 0.6 + Math.log10(edge.weight + 1)),
        color: view.colors.edge,
      });
    }
    sigma.setCustomBBox({ x: [-BOX, BOX], y: [-BOX, BOX] });
    sigma.refresh();
  }, [sigma, view.nodes, view.edges, view.sizes, view.colors]);

  // How it looks: read by the reducers.
  useEffect(() => {
    const onPath = new Set(view.path ?? []);
    const pathEdges = new Set<string>();
    const steps = view.path ?? [];
    for (let i = 1; i < steps.length; i++) {
      pathEdges.add(edgeKey(steps[i - 1] ?? "", steps[i] ?? ""));
    }
    liveRef.current = { ...liveRef.current, view, onPath, pathEdges };
    sigma.refresh({ skipIndexation: true });
  }, [sigma, view, liveRef]);

  const settle = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const report = () => {
      if (!onZoom) return;
      const { width, height } = sigma.getDimensions();
      const graph = sigma.getGraph();
      const visible: string[] = [];
      let nearest: string | null = null;
      let closest = Infinity;
      graph.forEachNode((key, attributes) => {
        const at = sigma.graphToViewport({ x: attributes.x, y: attributes.y });
        if (at.x >= 0 && at.y >= 0 && at.x <= width && at.y <= height) visible.push(key);
        const away = Math.hypot(at.x - width / 2, at.y - height / 2);
        if (away < closest) {
          closest = away;
          nearest = key;
        }
      });
      onZoom(sigma.getCamera().ratio, visible, nearest);
    };
    register({
      enterNode: ({ node, event }) => {
        if (dragging(event.original)) return; // panning: no relighting mid-drag
        const graph = sigma.getGraph();
        liveRef.current = {
          ...liveRef.current,
          hovered: node,
          near: new Set([node, ...graph.neighbors(node)]),
        };
        sigma.refresh({ skipIndexation: true });
      },
      leaveNode: () => {
        if (liveRef.current.hovered === null) return;
        liveRef.current = { ...liveRef.current, hovered: null, near: new Set() };
        sigma.refresh({ skipIndexation: true });
      },
      clickNode: ({ node }) => {
        onSelectNode(node);
      },
      clickStage: () => {
        onSelectNode(null);
      },
      clickEdge: ({ edge }) => {
        const [source, target] = sigma.getGraph().extremities(edge);
        onSelectEdge(source, target);
      },
    });
    const camera = sigma.getCamera();
    const updated = () => {
      if (settle.current) clearTimeout(settle.current);
      settle.current = setTimeout(report, ZOOM_SETTLE_MS);
    };
    camera.on("updated", updated);
    return () => {
      camera.off("updated", updated);
      if (settle.current) clearTimeout(settle.current);
    };
  }, [sigma, register, onSelectNode, onSelectEdge, onZoom, liveRef]);

  // A node chosen elsewhere (search, the table, a cycle): the camera goes to it.
  useEffect(() => {
    if (view.selected === null) return;
    const data = sigma.getNodeDisplayData(view.selected);
    if (!data) return;
    const camera = sigma.getCamera();
    void camera.animate(
      { x: data.x, y: data.y, ratio: Math.min(camera.ratio, 0.5) },
      { duration: still ? 0 : 400 },
    );
  }, [sigma, view.selected, still]);

  return (
    <>
      <div role="img" aria-label={label} className="sr-only" />
      <CameraControls still={still} />
      <Minimap nodes={view.nodes} colors={view.colors} still={still} />
    </>
  );
}

function dragging(event: MouseEvent | TouchEvent): boolean {
  return "buttons" in event && event.buttons !== 0;
}

function CameraControls({ still }: { still: boolean }) {
  const sigma = useSigma();
  const duration = still ? 0 : 250;
  return (
    <div className="absolute top-2 right-2 flex flex-col gap-1">
      <Button
        size="icon"
        variant="outline"
        aria-label="Zoom in"
        onClick={() => void sigma.getCamera().zoomIn({ duration })}
      >
        <Plus aria-hidden />
      </Button>
      <Button
        size="icon"
        variant="outline"
        aria-label="Zoom out"
        onClick={() => void sigma.getCamera().zoomOut({ duration })}
      >
        <Minus aria-hidden />
      </Button>
      <Button
        size="icon"
        variant="outline"
        aria-label="Fit the graph"
        onClick={() => void sigma.getCamera().reset({ duration })}
      >
        <Maximize2 aria-hidden />
      </Button>
    </div>
  );
}

/** Graph coordinates (y up, within the frame) to the minimap's pixels. */
function toMinimap(x: number, y: number): [number, number] {
  return [((x + BOX) / (2 * BOX)) * MINIMAP.width, (1 - (y + BOX) / (2 * BOX)) * MINIMAP.height];
}

function Minimap({
  nodes,
  colors,
  still,
}: {
  nodes: readonly ShownNode[];
  colors: GraphColors;
  still: boolean;
}) {
  const sigma = useSigma();
  const canvas = useRef<HTMLCanvasElement>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const camera = sigma.getCamera();
    let frame = 0;
    const updated = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        setTick((t) => t + 1);
      });
    };
    camera.on("updated", updated);
    return () => {
      camera.off("updated", updated);
      cancelAnimationFrame(frame);
    };
  }, [sigma]);

  // The level's dots, drawn once (per level and theme): a camera move only copies them.
  const dots = useMemo(() => {
    if (typeof document === "undefined") return null;
    const ratio = window.devicePixelRatio || 1;
    const sheet = document.createElement("canvas");
    sheet.width = MINIMAP.width * ratio;
    sheet.height = MINIMAP.height * ratio;
    const context = sheet.getContext("2d");
    if (!context) return null;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    for (const node of nodes) {
      const [x, y] = toMinimap(node.x, -node.y);
      context.fillStyle = colors.tints[tintOf(node.layer)];
      context.fillRect(x - 0.75, y - 0.75, 1.5, 1.5);
    }
    return sheet;
  }, [nodes, colors]);

  useEffect(() => {
    const context = canvas.current?.getContext("2d");
    if (!context) return;
    const ratio = window.devicePixelRatio || 1;
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.clearRect(0, 0, MINIMAP.width * ratio, MINIMAP.height * ratio);
    if (dots) context.drawImage(dots, 0, 0);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    // The view: the viewport's corners in graph coordinates.
    const { width, height } = sigma.getDimensions();
    const a = sigma.viewportToGraph({ x: 0, y: 0 });
    const b = sigma.viewportToGraph({ x: width, y: height });
    const [x1, y1] = toMinimap(a.x, a.y);
    const [x2, y2] = toMinimap(b.x, b.y);
    context.strokeStyle = colors.lit;
    context.lineWidth = 1;
    context.strokeRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
  }, [sigma, dots, colors, tick]);

  const moveTo = useCallback(
    (clientX: number, clientY: number, box: DOMRect) => {
      const x = ((clientX - box.left) / MINIMAP.width) * 2 * BOX - BOX;
      const y = (1 - (clientY - box.top) / MINIMAP.height) * 2 * BOX - BOX;
      const framed = sigma.graphToFramedGraph({ x, y });
      void sigma.getCamera().animate(framed, { duration: still ? 0 : 300 });
    },
    [sigma, still],
  );

  const ratio = typeof window === "undefined" ? 1 : window.devicePixelRatio || 1;
  return (
    <canvas
      ref={canvas}
      data-testid="graph-minimap"
      aria-hidden
      width={MINIMAP.width * ratio}
      height={MINIMAP.height * ratio}
      style={{ width: MINIMAP.width, height: MINIMAP.height }}
      className="bg-surface/90 absolute right-2 bottom-2 cursor-pointer rounded-sm border border-line"
      onClick={(event) => {
        moveTo(event.clientX, event.clientY, event.currentTarget.getBoundingClientRect());
      }}
    />
  );
}
