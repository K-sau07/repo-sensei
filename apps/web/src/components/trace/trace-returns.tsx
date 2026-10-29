"use client";

/**
 * A path's return leg (3.22), after its deepest hop: the response coming back up, to each
 * function that does something with it (deepest first), and what it does there, in source
 * order: a key stored, a page navigated to, state set, an error shown. An error's branch (a
 * catch, an onError) is marked as one. Each step can be picked, like a hop, to open its code.
 */
import { cn } from "cn";
import { Undo2 } from "lucide-react";

import {
  hopName,
  hopPlace,
  howReached,
  outcomeLabel,
  returnGroups,
  type TraceHop,
  type TracePath,
} from "@/lib/api/trace";

import { HopPip } from "./hop-pip";
import { OUTCOMES } from "./meta";

const fileName = (path: string) => path.slice(path.lastIndexOf("/") + 1);

function placeOf(hop: TraceHop): string | null {
  const place = hopPlace(hop);
  return place ? `${place.path}:${String(place.start)}` : null;
}

/** The words a return step says, for screen readers too. */
export function returnSummary(path: TracePath, index: number): string {
  const hop = path.hops[index];
  if (!hop) return "";
  const outcome = hop.node.outcome;
  const what = outcome
    ? `${OUTCOMES[outcome.kind].label} ${outcomeLabel(outcome)}`
    : hopName(hop.node);
  return ["Returns", howReached(path, index), what, placeOf(hop)].filter(Boolean).join(", ");
}

export function TraceReturns({
  path,
  selected,
  onSelect,
}: {
  path: TracePath;
  selected: number | null;
  onSelect: (hop: number | null) => void;
}) {
  const groups = returnGroups(path);
  if (groups.length === 0) return null;
  const pick = (index: number) => {
    onSelect(selected === index ? null : index);
  };
  return (
    <section
      aria-label="Returns: what the client does with the response"
      data-testid="trace-returns"
      className="flex flex-col gap-1.5 rounded-[10px] border border-line bg-surface-1 px-3 py-2"
    >
      <h3 className="flex items-center gap-1.5 text-xs font-medium text-fg-muted">
        <Undo2 aria-hidden className="size-3.5" />
        Returns
        <span className="font-normal text-fg-faint">what the client does with the response</span>
      </h3>
      <ol aria-label="Returns, in order" className="flex flex-col gap-1.5">
        {groups.map((group) => {
          const place = hopPlace(group.hop);
          return (
            <li key={group.index} className="flex flex-wrap items-center gap-1.5 text-sm">
              <button
                type="button"
                aria-pressed={selected === group.index}
                onClick={() => {
                  pick(group.index);
                }}
                className={cn(
                  "flex items-baseline gap-1.5 rounded-md px-1.5 py-0.5 text-left outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-accent",
                  selected === group.index && "bg-surface-3",
                )}
              >
                <span className="sr-only">{returnSummary(path, group.index)}</span>
                <span aria-hidden className="text-fg-muted">
                  ↩
                </span>
                <span aria-hidden className="font-mono text-fg">
                  {hopName(group.hop.node)}
                </span>
                {place && (
                  <span aria-hidden className="font-mono text-xs text-fg-faint">
                    {fileName(place.path)}:{place.start}
                  </span>
                )}
              </button>
              <ul
                aria-label={`What ${hopName(group.hop.node)} does with it`}
                className="flex flex-wrap items-center gap-1"
              >
                {group.outcomes.map(({ index, hop }) => {
                  const outcome = hop.node.outcome;
                  const meta = outcome ? OUTCOMES[outcome.kind] : null;
                  const error = outcome?.on === "error";
                  return (
                    <li key={index} className="flex items-center" data-outcome={outcome?.kind}>
                      <button
                        type="button"
                        aria-pressed={selected === index}
                        onClick={() => {
                          pick(index);
                        }}
                        title={
                          outcome?.value ? `${hop.node.name}: ${outcome.value}` : hop.node.name
                        }
                        className={cn(
                          "inline-flex max-w-72 items-center gap-1 rounded-full border bg-surface-2 py-0.5 pr-2 pl-1.5 text-xs outline-none hover:border-line-strong focus-visible:ring-2 focus-visible:ring-accent",
                          selected === index
                            ? "border-accent bg-surface-3"
                            : error
                              ? "border-danger/40"
                              : "border-line",
                        )}
                      >
                        <span className="sr-only">{returnSummary(path, index)}</span>
                        <span aria-hidden className={error ? "text-danger" : "text-fg-muted"}>
                          {meta?.glyph ?? "•"}
                        </span>
                        {error && (
                          <span aria-hidden className="text-danger">
                            on error
                          </span>
                        )}
                        <span aria-hidden className="truncate font-mono text-fg">
                          {outcome ? outcomeLabel(outcome) : hop.node.name}
                        </span>
                      </button>
                      {hop.edge && <HopPip edge={hop.edge} />}
                    </li>
                  );
                })}
              </ul>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
