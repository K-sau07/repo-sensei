"use client";

/**
 * The Graph tab (6.7): a level of the dependency graph on a WebGL canvas, or as tables.
 *
 * - The level: services, packages, modules, files or symbols. Zooming in past a threshold opens
 *   the nodes in view into the level below, in place (and zooming out closes them); a node's
 *   panel opens it too, without zooming.
 * - Hover lights a node's neighbours; a click focuses it, with its neighbours ± hops out.
 * - Search lights the nodes by name, or (a phrase) the nodes the search's hits are in.
 * - A path from one node to another, along what depends on what.
 * - An edge's panel lists the lines of code behind it.
 */
import { Search, X } from "lucide-react";
import { useReducedMotionConfig } from "motion/react";
import dynamic from "next/dynamic";
import { useCallback, useMemo, useState } from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { webGLSupport } from "@/components/indexing/constellation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  LEVEL_LABELS,
  cycleEdges,
  expand,
  fanIn,
  finer,
  matching,
  neighborhood,
  nodeOfPath,
  shortestPath,
  sizeOf,
  type DepLevel,
} from "@/lib/api/deps";
import { refineLayout } from "@/lib/graph/refine-layout";

import { useGraphColors } from "./graph-colors";
import { GraphLegend } from "./graph-legend";
import { EdgePanel, NodePanel } from "./graph-panels";
import { MAX_DEGREE, useGraphState } from "./graph-state";
import { GraphTable } from "./graph-table";
import { GuessesSwitch } from "./guesses-switch";
import { LevelSelect } from "./level-select";
import { MAX_DSM, useCycles, useDeps, useDsm, useGraphSearch } from "./use-deps";

const GraphCanvas = dynamic(() => import("./graph-canvas"), {
  ssr: false,
  loading: () => <div className="size-full" />,
});

/** Zoomed in this far (the camera's ratio), the nodes in view open into the level below; out
 * this far, they close. */
export const OPEN_AT = 0.35;
export const CLOSE_AT = 0.7;
/** The most nodes opened at once by zooming (more in view: zoom in further). */
export const MAX_OPEN = 12;

export interface SelectedEdge {
  source: string;
  target: string;
}

function same(a: ReadonlySet<string>, b: ReadonlySet<string>): boolean {
  return a.size === b.size && [...a].every((x) => b.has(x));
}

export function GraphExplorer() {
  const [state, setState] = useGraphState();
  const level = state.level;
  const below = finer(level);
  const coarse = useDeps(level);
  // Opened by zooming in (closed by zooming out), and from a node's panel (until closed there).
  const [zoomOpen, setZoomOpen] = useState<ReadonlySet<string>>(new Set());
  const [pinned, setPinned] = useState<ReadonlySet<string>>(new Set());
  const open = useMemo(() => new Set([...zoomOpen, ...pinned]), [zoomOpen, pinned]);
  const fine = useDeps(open.size > 0 ? below : null);
  const cyclesOf = useCycles(level);
  const colors = useGraphColors();
  const still = useReducedMotionConfig() ?? false;
  const [edge, setEdge] = useState<SelectedEdge | null>(null);
  const [webgl] = useState(() => typeof document === "undefined" || webGLSupport().webgl2);
  const [query, setQuery] = useState(state.q);
  const [zoom, setZoom] = useState(1);

  const placedCoarse = useMemo(
    () => (coarse.data ? refineLayout(coarse.data) : null),
    [coarse.data],
  );
  const placedFine = useMemo(() => (fine.data ? refineLayout(fine.data) : null), [fine.data]);
  const shown = useMemo(
    () => (placedCoarse ? expand(placedCoarse, placedFine, open) : { nodes: [], edges: [] }),
    [placedCoarse, placedFine, open],
  );
  const ids = useMemo(() => new Set(shown.nodes.map((n) => n.id)), [shown.nodes]);
  const levelOf = useMemo(
    () => new Map<string, DepLevel>(shown.nodes.map((n) => [n.id, n.level])),
    [shown.nodes],
  );
  const sizes = useMemo(() => {
    const fan = fanIn(shown.edges);
    const most = Math.max(0, ...fan.values());
    // A big level's nodes drawn smaller, so they don't cover each other.
    const scale = shown.nodes.length > 5_000 ? 0.3 : shown.nodes.length > 500 ? 0.6 : 1;
    return new Map(shown.nodes.map((n) => [n.id, sizeOf(fan.get(n.id) ?? 0, most) * scale]));
  }, [shown]);
  const cycles = useMemo(() => cycleEdges(cyclesOf.data?.cycles ?? []), [cyclesOf.data]);
  // Coupling (for a node's panel) comes with the matrix: only for a level small enough for one.
  const dsm = useDsm(level, coarse.data !== undefined && coarse.data.total <= MAX_DSM);

  // Search: by name; a phrase, or a name nothing is called, by the search's hits.
  const byName = useMemo(() => matching(shown.nodes, state.q), [shown.nodes, state.q]);
  const phrase = state.q.trim().includes(" ") || (state.q.trim() !== "" && byName.length === 0);
  const search = useGraphSearch(state.q, phrase);
  const found = useMemo(() => {
    const lit = new Set(byName);
    if (phrase && search.data) {
      for (const hits of Object.values(search.data.lanes)) {
        for (const hit of hits) {
          const id =
            nodeOfPath(ids, level, hit.path, hit.symbol_key) ??
            (below ? nodeOfPath(ids, below, hit.path, hit.symbol_key) : null);
          if (id !== null) lit.add(id);
        }
      }
    }
    return lit;
  }, [byName, phrase, search.data, ids, level, below]);

  const cycle = state.cycle !== null ? cyclesOf.data?.cycles[state.cycle] : undefined;
  const focus = useMemo(() => {
    if (cycle) return new Set(cycle.nodes);
    if (state.node === null || !ids.has(state.node)) return null;
    return neighborhood(shown.edges, state.node, Math.max(1, state.degree));
  }, [cycle, state.node, state.degree, ids, shown.edges]);
  const path = useMemo(() => {
    if (state.from === null || state.to === null) return null;
    if (!ids.has(state.from) || !ids.has(state.to)) return null;
    return shortestPath(shown.edges, state.from, state.to);
  }, [state.from, state.to, ids, shown.edges]);

  const onZoom = useCallback(
    (ratio: number, visible: string[], nearest: string | null) => {
      setZoom(ratio);
      if (below === null || !coarse.data) return;
      if (ratio > CLOSE_AT) {
        setZoomOpen((held) => (held.size === 0 ? held : new Set()));
        return;
      }
      if (ratio > OPEN_AT) return;
      const parents = new Set(coarse.data.nodes.map((n) => n.id));
      // The nodes in view; none (zoomed in between them): the nearest.
      let inView = visible.filter((id) => parents.has(id));
      if (inView.length === 0 && nearest !== null && parents.has(nearest)) inView = [nearest];
      if (inView.length === 0 || inView.length > MAX_OPEN) return;
      // The nodes in view open (those panned away close): never more than MAX_OPEN at once.
      setZoomOpen((held) => {
        const next = new Set(inView);
        return same(next, held) ? held : next;
      });
    },
    [below, coarse.data],
  );
  const selectNode = useCallback(
    (id: string | null) => {
      setEdge(null);
      void setState({ node: id, cycle: null });
    },
    [setState],
  );
  const selectEdge = useCallback((source: string, target: string) => {
    setEdge({ source, target });
  }, []);

  if (coarse.isError) {
    return (
      <div className="p-6">
        <ErrorState title="Couldn't load the graph" onRetry={() => void coarse.refetch()} />
      </div>
    );
  }
  const selected = state.node !== null && ids.has(state.node) ? state.node : null;
  const table = state.table || !webgl;
  const label = `${LEVEL_LABELS[level]}: ${String(shown.nodes.length)} nodes, ${String(shown.edges.length)} edges`;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2">
        <LevelSelect
          value={level}
          onChange={(value) => {
            setZoomOpen(new Set());
            setPinned(new Set());
            setEdge(null);
            void setState({ level: value, node: null, from: null, to: null, cycle: null });
          }}
        />
        <form
          role="search"
          aria-label="Search the graph"
          className="flex items-center gap-1"
          onSubmit={(event) => {
            event.preventDefault();
            void setState({ q: query.trim() });
          }}
        >
          <Input
            aria-label="Find in the graph"
            placeholder="A name, or what it does"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
            }}
            className="h-8 w-64"
          />
          <Button type="submit" size="icon" variant="ghost" aria-label="Find">
            <Search aria-hidden />
          </Button>
          {state.q ? (
            <Button
              type="button"
              size="icon"
              variant="ghost"
              aria-label="Clear the search"
              onClick={() => {
                setQuery("");
                void setState({ q: "" });
              }}
            >
              <X aria-hidden />
            </Button>
          ) : null}
        </form>
        {state.q ? (
          <p className="text-xs text-fg-muted" data-testid="graph-found" aria-live="polite">
            {phrase && search.isFetching
              ? "Searching…"
              : `${String(found.size)} ${found.size === 1 ? "node" : "nodes"} found`}
          </p>
        ) : null}
        {selected !== null && !cycle ? (
          <div
            className="flex items-center gap-1 text-xs text-fg-muted"
            role="group"
            aria-label="Focus"
          >
            <span>Focus ±</span>
            <Button
              size="icon"
              variant="ghost"
              aria-label="Fewer hops"
              disabled={state.degree <= 1}
              onClick={() => void setState({ degree: Math.max(1, state.degree - 1) })}
            >
              −
            </Button>
            <span className="tabular-nums" data-testid="graph-degree">
              {state.degree}
            </span>
            <Button
              size="icon"
              variant="ghost"
              aria-label="More hops"
              disabled={state.degree >= MAX_DEGREE}
              onClick={() => void setState({ degree: Math.min(MAX_DEGREE, state.degree + 1) })}
            >
              +
            </Button>
            <Button size="sm" variant="ghost" onClick={() => void setState({ node: null })}>
              Show all
            </Button>
          </div>
        ) : null}
        {cycle ? (
          <Button size="sm" variant="ghost" onClick={() => void setState({ cycle: null })}>
            Show all
          </Button>
        ) : null}
        <span className="ml-auto">
          <GuessesSwitch
            onChange={() => {
              setEdge(null);
            }}
          />
        </span>
        <span className="flex items-center gap-1.5 text-xs text-fg-muted">
          <Switch
            checked={table}
            disabled={!webgl}
            onCheckedChange={(checked) => void setState({ table: checked })}
            aria-label="Show as tables"
          />
          <span aria-hidden>Table</span>
        </span>
      </div>
      {state.from !== null || state.to !== null ? (
        <PathBar
          from={state.from}
          to={state.to}
          path={path}
          labelOf={(id) => shown.nodes.find((n) => n.id === id)?.label ?? id}
          onClear={() => void setState({ from: null, to: null })}
        />
      ) : null}
      {coarse.data?.truncated ? (
        <p className="border-b border-line px-4 py-1.5 text-xs text-fg-muted">
          The {String(coarse.data.nodes.length)} largest of {String(coarse.data.total)}{" "}
          {LEVEL_LABELS[level].toLowerCase()} are shown.
        </p>
      ) : null}
      {!webgl ? (
        <p className="border-b border-line px-4 py-1.5 text-xs text-fg-muted">
          This browser can&apos;t draw WebGL2: the graph is shown as tables.
        </p>
      ) : null}
      <div className="flex min-h-0 flex-1">
        <div
          className="relative h-[calc(100dvh-14rem)] min-h-[28rem] flex-1 overflow-hidden"
          data-testid="graph-canvas"
          data-nodes={shown.nodes.length}
          data-open={[...open].join(",")}
          data-zoom={zoom.toFixed(2)}
        >
          {coarse.isPending || colors === null ? (
            <div role="status" aria-label="Loading the graph" className="p-6">
              <Skeleton className="h-64" />
            </div>
          ) : table ? (
            <GraphTable nodes={shown.nodes} edges={shown.edges} onSelectNode={selectNode} />
          ) : (
            <>
              <GraphCanvas
                view={{
                  nodes: shown.nodes,
                  edges: shown.edges,
                  sizes,
                  colors,
                  focus,
                  found,
                  path,
                  cycles,
                  selected,
                }}
                label={label}
                still={still}
                onSelectNode={selectNode}
                onSelectEdge={selectEdge}
                onZoom={onZoom}
              />
              <GraphLegend className="absolute bottom-2 left-2" />
            </>
          )}
        </div>
        <aside
          aria-label="Details"
          className="flex w-80 shrink-0 flex-col overflow-y-auto border-l border-line"
        >
          {edge ? (
            <EdgePanel
              edge={edge}
              edges={shown.edges}
              levelOf={levelOf}
              onClose={() => {
                setEdge(null);
              }}
            />
          ) : selected !== null ? (
            <NodePanel
              id={selected}
              nodes={shown.nodes}
              edges={shown.edges}
              dsm={dsm.data ?? null}
              canOpen={below !== null && (levelOf.get(selected) ?? level) === level}
              opened={open.has(selected)}
              onOpen={(id) => {
                if (open.has(id)) {
                  setPinned((held) => new Set([...held].filter((x) => x !== id)));
                  setZoomOpen((held) => new Set([...held].filter((x) => x !== id)));
                } else {
                  setPinned((held) => new Set([...held, id]));
                }
              }}
              onSelectEdge={selectEdge}
              onPath={(end, id) => void setState(end === "from" ? { from: id } : { to: id })}
            />
          ) : (
            <p className="p-4 text-sm text-fg-muted">
              Hover a node to see what it touches; click one to focus on it, or an edge for the code
              behind it. Zoom in to open {LEVEL_LABELS[level].toLowerCase()} into{" "}
              {below ? LEVEL_LABELS[below].toLowerCase() : "their parts"}.
            </p>
          )}
        </aside>
      </div>
      <p className="sr-only" aria-live="polite">
        {label}
      </p>
    </div>
  );
}

function PathBar({
  from,
  to,
  path,
  labelOf,
  onClear,
}: {
  from: string | null;
  to: string | null;
  path: readonly string[] | null;
  labelOf: (id: string) => string;
  onClear: () => void;
}) {
  let text: string;
  if (from === null || to === null) {
    text =
      from !== null
        ? `From ${labelOf(from)}: pick where to`
        : `To ${labelOf(to ?? "")}: pick where from`;
  } else if (path === null) {
    text = `Nothing leads from ${labelOf(from)} to ${labelOf(to)}.`;
  } else {
    const hops = path.length - 1;
    text = `${path.map(labelOf).join(" → ")} (${String(hops)} ${hops === 1 ? "hop" : "hops"})`;
  }
  return (
    <div
      className="flex items-center gap-2 border-b border-line px-4 py-1.5 text-xs"
      data-testid="graph-path"
    >
      <span className="font-medium">Path</span>
      <span className="min-w-0 flex-1 truncate text-fg-muted">{text}</span>
      <Button size="sm" variant="ghost" onClick={onClear}>
        Clear
      </Button>
    </div>
  );
}
