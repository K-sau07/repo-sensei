"use client";

/**
 * A trace path as an ordered list (the swimlanes' accessible alternative, and a denser read):
 * each hop in order with its lane, what it is, how it was reached and where, and how sure; a gap
 * where the path stops; then the return leg, what the client does with the response.
 */
import { cn } from "cn";

import {
  downHops,
  hopName,
  hopPlace,
  reachedAt,
  isMidEntry,
  percent,
  type TracePath,
} from "@/lib/api/trace";

import { HopPip } from "./hop-pip";
import { kindLabel, TRACE_LANES } from "./meta";
import { TraceGap } from "./trace-gap";
import { TraceReturns } from "./trace-returns";
import { Alternatives, hopSummary } from "./trace-swimlane";

export function TraceList({
  path,
  selected,
  onSelect,
}: {
  path: TracePath;
  selected: number | null;
  onSelect: (hop: number | null) => void;
}) {
  const gap = path.breaks.at(-1) ?? null;
  return (
    <div className="flex flex-col gap-2">
      <ol aria-label="Hops, in order" data-testid="trace-list" className="flex flex-col gap-0.5">
        {downHops(path).map((hop, i) => {
          const lane = TRACE_LANES[hop.layer];
          const place = hopPlace(hop);
          return (
            <li
              key={hop.node.symbol_key}
              className={cn(
                "flex items-center gap-3 rounded-md px-2 py-1 text-sm",
                selected === i ? "bg-surface-3" : "hover:bg-surface-2",
              )}
            >
              <span
                aria-hidden
                className="w-5 shrink-0 text-right text-xs text-fg-faint tabular-nums"
              >
                {i + 1}
              </span>
              <span
                aria-hidden
                className={cn("flex w-32 shrink-0 items-center gap-1.5 text-xs", lane.text)}
              >
                <span>{lane.glyph}</span>
                {lane.label}
              </span>
              <button
                type="button"
                aria-pressed={selected === i}
                onClick={() => {
                  onSelect(selected === i ? null : i);
                }}
                className="flex min-w-0 flex-1 items-baseline gap-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                <span className="sr-only">{hopSummary(path, i)}</span>
                <span aria-hidden className="shrink-0 font-mono text-fg">
                  {hopName(hop.node)}
                </span>
                <span aria-hidden className="shrink-0 text-xs text-fg-muted">
                  {kindLabel(hop.node.kind)} · {reachedAt(path, i)}
                </span>
                {isMidEntry(path, i) && (
                  <span
                    aria-hidden
                    className="text-surface-0 shrink-0 rounded-sm bg-signal px-1 text-[10px] font-medium"
                  >
                    entry
                  </span>
                )}
                {place && (
                  <span aria-hidden className="min-w-0 truncate font-mono text-xs text-fg-faint">
                    {place.path}:{place.start}
                  </span>
                )}
              </button>
              <span className="text-xs">
                <Alternatives hop={hop} />
              </span>
              {hop.edge && (
                <span className="flex shrink-0 items-center gap-1 text-xs text-fg-muted tabular-nums">
                  <HopPip edge={hop.edge} />
                  <span aria-hidden className="w-8 text-right">
                    {percent(hop.edge.confidence)}
                  </span>
                </span>
              )}
            </li>
          );
        })}
        {gap && (
          <li className="py-1 pl-10">
            <TraceGap path={path} gap={gap} className="max-w-lg" />
          </li>
        )}
      </ol>
      <TraceReturns path={path} selected={selected} onSelect={onSelect} />
    </div>
  );
}
