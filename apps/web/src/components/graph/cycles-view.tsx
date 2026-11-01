"use client";

/**
 * The Cycles tab (6.7): the level's dependency cycles, the largest first. Each leads with the
 * few edges that close it (cut them and it is gone: the API's lightest such set it finds), then
 * its groups and all its edges (folded away when there are many), and
 * shows it alone on the graph.
 */
import { RefreshCcw } from "lucide-react";

import { ErrorState } from "@/components/feedback/error-state";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { LEVEL_LABELS, type Cycle, type DsmNode } from "@/lib/api/deps";

import { useGraphState } from "./graph-state";
import { GuessesSwitch } from "./guesses-switch";
import { LevelSelect } from "./level-select";
import { useCycles, useDsm } from "./use-deps";

/** Shown before "and N more". */
const FIRST = 12;

function Edge({ edge, name }: { edge: Cycle["edges"][number]; name: (id: string) => string }) {
  return (
    <li className="flex gap-2">
      <span className="min-w-0 flex-1 truncate">
        {name(edge.source)} <span aria-hidden>→</span>
        <span className="sr-only">depends on</span> {name(edge.target)}
      </span>
      <span className="text-fg-muted tabular-nums">{edge.weight}</span>
    </li>
  );
}

/** What closes a cycle, as words: "Cut 3 of 9 edges (20 of 363 uses) to break it". */
export function breaksLabel(cycle: Cycle): string {
  const uses = (edges: Cycle["edges"]) => edges.reduce((sum, e) => sum + e.weight, 0);
  const n = cycle.breaks.length;
  return `Cut ${String(n)} of ${String(cycle.edges.length)} ${cycle.edges.length === 1 ? "edge" : "edges"} (${String(uses(cycle.breaks))} of ${String(uses(cycle.edges))} uses) to break it`;
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
      <div className="flex items-center gap-3">
        <LevelSelect
          value={level}
          onChange={(value) => void setState({ level: value, cycle: null })}
        />
        <GuessesSwitch />
      </div>
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
                <p className="text-xs font-medium" data-testid="cycle-breaks">
                  {breaksLabel(cycle)}
                </p>
                <ul className="flex flex-col gap-0.5 text-xs" aria-label="Edges that close it">
                  {cycle.breaks.slice(0, FIRST).map((edge) => (
                    <Edge key={`${edge.source}\u0000${edge.target}`} edge={edge} name={name} />
                  ))}
                </ul>
                {cycle.breaks.length > FIRST ? (
                  <details className="text-xs">
                    <summary className="cursor-pointer text-fg-muted">
                      and {cycle.breaks.length - FIRST} more
                    </summary>
                    <ul className="mt-1 flex flex-col gap-0.5">
                      {cycle.breaks.slice(FIRST).map((edge) => (
                        <Edge key={`${edge.source}\u0000${edge.target}`} edge={edge} name={name} />
                      ))}
                    </ul>
                  </details>
                ) : null}
                <details className="text-xs">
                  <summary className="cursor-pointer text-fg-muted">
                    Its {cycle.nodes.length} {LEVEL_LABELS[level].toLowerCase()} and{" "}
                    {cycle.edges.length} {cycle.edges.length === 1 ? "edge" : "edges"}
                  </summary>
                  <p className="mt-1 break-all text-fg-muted">{cycle.nodes.map(name).join(", ")}</p>
                  <ul className="mt-1 flex flex-col gap-0.5">
                    {cycle.edges.map((edge) => (
                      <Edge key={`${edge.source}\u0000${edge.target}`} edge={edge} name={name} />
                    ))}
                  </ul>
                </details>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
