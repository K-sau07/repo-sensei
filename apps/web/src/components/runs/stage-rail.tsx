"use client";

/**
 * A run's steps in words (4.12), the left of its timeline: each step, or (aggregated) each
 * run of repeated calls folded into one ("Searching code ×3"). Virtualized: a long run's
 * hundreds of steps scroll as a few rows. Steps past the one replayed to are ghosted.
 */
import { useVirtualizer } from "@tanstack/react-virtual";
import { cn } from "cn";
import { useRef } from "react";

import { aggregate, formatMs, stepTitle, type Step, type StepGroup } from "@/lib/api/runs";

const ROW_PX = 44;

export function StageRail({
  steps,
  aggregated,
  selected,
  until,
  onSelect,
}: {
  steps: readonly Step[];
  aggregated: boolean;
  selected: number | null;
  until: number;
  onSelect: (idx: number) => void;
}) {
  const groups: StepGroup[] = aggregated
    ? aggregate(steps)
    : steps.map((step) => ({ steps: [step], label: stepTitle(step) }));
  const scroller = useRef<HTMLDivElement>(null);
  // TanStack Virtual returns fresh functions each render, which the React Compiler can't
  // memoize; this component simply isn't compiled.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: groups.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_PX,
    overscan: 8,
  });
  return (
    <div ref={scroller} className="h-full overflow-auto" data-testid="stage-rail">
      <ol aria-label="Steps" className="relative" style={{ height: virtualizer.getTotalSize() }}>
        {virtualizer.getVirtualItems().map((row) => {
          const group = groups[row.index];
          const first = group?.steps[0];
          if (!group || !first) return null;
          const ms = group.steps.reduce((sum, s) => sum + s.ms, 0);
          const tokens = group.steps.reduce((sum, s) => sum + s.tokens, 0);
          const chosen = group.steps.some((s) => s.idx === selected);
          return (
            <li
              key={first.idx}
              className="absolute inset-x-0"
              style={{ top: row.start, height: ROW_PX }}
            >
              <button
                type="button"
                aria-current={chosen ? "step" : undefined}
                onClick={() => {
                  onSelect(first.idx);
                }}
                className={cn(
                  "flex h-full w-full flex-col justify-center gap-0.5 border-l-2 border-transparent px-3 text-left hover:bg-muted",
                  chosen && "border-brand bg-muted",
                  // Ghosted (past the replay): muted and dashed, still legible.
                  first.idx > until && "border-dashed border-line text-fg-muted",
                )}
              >
                <span className="truncate text-xs font-medium">
                  {group.label}
                  {first.idx > until ? <span className="sr-only"> (after the replay)</span> : null}
                </span>
                <span className="font-mono text-[10px] text-fg-muted">
                  {formatMs(ms)}
                  {tokens ? ` · ${tokens.toLocaleString()} tokens` : ""}
                  {first.summary ? ` · ${first.summary}` : ""}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
