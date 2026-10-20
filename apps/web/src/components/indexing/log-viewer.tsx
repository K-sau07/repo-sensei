"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { cn } from "cn";
import { useRef, useState } from "react";

import { CopyButton } from "@/components/primitives/copy-button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { STAGE_LABELS } from "@/lib/indexing";

import type { LogLine } from "./use-job-events";

const LEVELS = ["info", "warn", "error"] as const;
type Level = (typeof LEVELS)[number];
const LEVEL_TONE: Record<Level, string> = {
  info: "text-fg-muted",
  warn: "text-warn",
  error: "text-danger",
};
const ROW_HEIGHT = 28;

export function filterLines(lines: LogLine[], levels: Level[], query: string): LogLine[] {
  const needle = query.trim().toLowerCase();
  return lines.filter(
    (line) =>
      levels.includes(line.level) && (!needle || line.message.toLowerCase().includes(needle)),
  );
}

export const formatLine = (line: LogLine) =>
  `${line.ts} ${line.level.toUpperCase()} [${line.stage ?? "job"}] ${line.message}`;

/** The run's log in a drawer: filter by level, search, copy what's shown. Virtualized, so a
 * long log scrolls as smoothly as a short one. */
export function LogViewer({
  open,
  onOpenChange,
  lines,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  lines: LogLine[];
}) {
  const [levels, setLevels] = useState<Level[]>([...LEVELS]);
  const [query, setQuery] = useState("");
  const shown = filterLines(lines, levels, query);
  const scroller = useRef<HTMLDivElement>(null);
  // TanStack Virtual returns fresh functions each render, which the React Compiler can't
  // memoize; this component simply isn't compiled.
  // eslint-disable-next-line react-hooks/incompatible-library
  const rows = useVirtualizer({
    count: shown.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
    // The drawer's list mounts after the first render; start with a screenful of rows rather
    // than none until the list is measured.
    initialRect: { width: 640, height: 720 },
  });

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full gap-3 data-[side=right]:sm:max-w-3xl">
        <SheetHeader>
          <SheetTitle>Index log</SheetTitle>
          <SheetDescription>What each stage reported, as it happened.</SheetDescription>
        </SheetHeader>
        <div className="flex flex-wrap items-center gap-2 px-4">
          <ToggleGroup
            multiple
            value={levels}
            onValueChange={(value) => {
              setLevels(value as Level[]);
            }}
            aria-label="Levels"
          >
            {LEVELS.map((level) => (
              <ToggleGroupItem key={level} value={level} size="sm">
                {level}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <Input
            aria-label="Search the log"
            placeholder="Search"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
            }}
            className="h-control-sm w-48"
          />
          <span className="ml-auto inline-flex items-center gap-1 text-xs text-fg-muted">
            {shown.length} {shown.length === 1 ? "line" : "lines"}
            <CopyButton value={shown.map(formatLine).join("\n")} label="Copy the lines shown" />
          </span>
        </div>
        {/* Focusable so the scrolling list can be read and scrolled from the keyboard. */}
        {}
        <div
          ref={scroller}
          role="log"
          aria-label="Log lines"
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- see above
          tabIndex={0}
          className="mx-4 mb-4 min-h-0 flex-1 overflow-y-auto rounded-md border border-line bg-surface-1 font-mono text-xs focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
        >
          {shown.length === 0 ? (
            <p className="p-4 text-fg-muted">
              {lines.length === 0 ? "Nothing logged yet." : "No lines match."}
            </p>
          ) : (
            <ol className="relative" style={{ height: rows.getTotalSize() }}>
              {rows.getVirtualItems().map((row) => {
                const line = shown[row.index];
                if (!line) return null;
                return (
                  <li
                    key={line.id}
                    className="absolute inset-x-0 flex items-center gap-3 px-3"
                    style={{ height: row.size, transform: `translateY(${String(row.start)}px)` }}
                  >
                    <time dateTime={line.ts} className="shrink-0 text-fg-faint tabular-nums">
                      {new Date(line.ts).toLocaleTimeString("en", { hour12: false })}
                    </time>
                    <span className={cn("w-10 shrink-0 uppercase", LEVEL_TONE[line.level])}>
                      {line.level}
                    </span>
                    <span className="w-24 shrink-0 truncate text-fg-muted">
                      {line.stage ? STAGE_LABELS[line.stage] : "Job"}
                    </span>
                    <span className="truncate text-fg" title={line.message}>
                      {line.message}
                    </span>
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
