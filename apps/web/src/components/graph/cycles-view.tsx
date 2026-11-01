"use client";

/**
 * The Cycles tab (6.7): the level's dependency cycles, the largest first. Each lists its groups
 * and the edges that close it, the lightest marked (the fewest lines to change to break it), and
 * shows it alone on the graph.
 */
import { RefreshCcw } from "lucide-react";

import { ErrorState } from "@/components/feedback/error-state";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { LEVEL_LABELS, type Cycle, type DsmNode } from "@/lib/api/deps";

import { useGraphState } from "./graph-state";
import { LevelSelect } from "./level-select";
import { useCycles, useDsm } from "./use-deps";

export function lightest(cycle: Cycle): Cycle["edges"][number] | null {
  let found: Cycle["edges"][number] | null = null;
  for (const edge of cycle.edges) {
    if (found === null || edge.weight < found.weight) found = edge;
  }
  return found;
}

export function CyclesView() {
  const [state, setState] = useGraphState();
  const level = state.level;
  const dsm = useCycles(level);
  // Names, when the level is small enough for a matrix (else its ids).
  const matrix = useDsm(level, level !== "symbol");
  const labels = new Map((matrix.data?.order ?? []).map((n: DsmNode) => [n.id, n.label]));
  const name = (id: string) => labels.get(id) ?? id;
  return (
    <div className="flex max-w-3xl flex-col gap-4 p-4">
      <LevelSelect
        value={level}
        onChange={(value) => void setState({ level: value, cycle: null })}
      />
      {dsm.isPending ? (
        <Skeleton className="h-40" />
      ) : dsm.isError ? (
        <ErrorState title="Couldn't load the cycles" onRetry={() => void dsm.refetch()} />
      ) : dsm.data.cycles.length === 0 ? (
        <p className="text-sm text-fg-muted" data-testid="no-cycles">
          No cycles among the {LEVEL_LABELS[level].toLowerCase()}: every dependency goes one way.
        </p>
      ) : (
        <ol className="flex flex-col gap-3" aria-label="Cycles">
          {dsm.data.cycles.map((cycle, i) => {
            const weakest = lightest(cycle);
            return (
              <li
                key={cycle.nodes.join("\u0000")}
                className="flex flex-col gap-2 rounded-md border border-line p-3"
                data-testid="cycle"
              >
                <div className="flex items-center gap-2">
                  <RefreshCcw aria-hidden className="size-4 text-danger" />
                  <h2 className="flex-1 text-sm font-medium">
                    {cycle.nodes.length} {LEVEL_LABELS[level].toLowerCase()} depend on each other
                  </h2>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => void setState({ tab: "graph", cycle: i, node: null })}
                  >
                    Show in the graph
                  </Button>
                </div>
                <p className="text-xs break-all text-fg-muted">
                  {cycle.nodes.map(name).join(", ")}
                </p>
                <ul className="flex flex-col gap-0.5 text-xs">
                  {cycle.edges.map((edge) => (
                    <li key={`${edge.source}\u0000${edge.target}`} className="flex gap-2">
                      <span className="min-w-0 flex-1 truncate">
                        {name(edge.source)} <span aria-hidden>→</span>
                        <span className="sr-only">depends on</span> {name(edge.target)}
                      </span>
                      <span className="text-fg-muted tabular-nums">{edge.weight}</span>
                      {edge === weakest ? (
                        <span className="text-warn">lightest: the least to change</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
