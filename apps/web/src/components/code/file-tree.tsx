"use client";

import { defaultRangeExtractor, useVirtualizer, type Range } from "@tanstack/react-virtual";
import { cn } from "cn";
import {
  ChevronRight,
  ChevronsDownUp,
  File as FileIcon,
  Folder,
  FolderOpen,
  Loader2,
  LocateFixed,
  TriangleAlert,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { LAYERS, type Layer } from "@/components/primitives/layer-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api/errors";
import {
  ancestorsOf,
  flattenTree,
  layerOfRole,
  treeKeyAction,
  typeAhead,
  type TreeRow,
} from "@/lib/code/tree";

import { useListings } from "./use-code";

const ROW_HEIGHT = 28;
const INDENT = 12;
const TYPE_AHEAD_MS = 700;
const LAYER_ORDER = ["ui", "api", "svc", "db"] as const satisfies readonly Layer[];
const DOT: Record<Layer, string> = {
  ui: "bg-layer-ui",
  api: "bg-layer-api",
  svc: "bg-layer-svc",
  db: "bg-layer-db",
};

interface FileTreeProps {
  /** The file or directory open in the browser: highlighted, and revealed when it changes. */
  current: string;
  /** A file was picked (click or Enter). */
  onOpenFile: (path: string) => void;
  /** Directories to show open at first, besides the ones above `current`. */
  defaultExpanded?: readonly string[];
  className?: string;
}

function TreeItem({
  row,
  id,
  active,
  current,
  onPick,
}: {
  row: TreeRow;
  id: string;
  active: boolean;
  current: boolean;
  onPick: () => void;
}) {
  const { entry, level, expanded, status } = row;
  const isDir = entry.type === "dir";
  const layer = isDir ? null : layerOfRole(entry.role);
  const generated = entry.flags?.includes("generated") === true;
  const FolderGlyph = expanded ? FolderOpen : Folder;
  return (
    // Keys are handled by the tree, which keeps focus and points at this row
    // (aria-activedescendant), so a virtualized row can come and go without losing it.
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events
    <div
      role="treeitem"
      id={id}
      aria-level={level}
      aria-setsize={row.siblings}
      aria-posinset={row.position}
      aria-expanded={isDir ? expanded : undefined}
      aria-selected={current}
      aria-busy={status === "loading" || undefined}
      tabIndex={-1}
      onClick={onPick}
      data-active={active || undefined}
      className={cn(
        "flex h-full cursor-pointer items-center gap-1.5 pr-2 text-sm whitespace-nowrap select-none hover:bg-surface-2",
        current ? "bg-surface-3 text-fg" : "text-fg-muted",
        "group-focus-visible/tree:data-active:outline-2 group-focus-visible/tree:data-active:-outline-offset-2 group-focus-visible/tree:data-active:outline-ring/50",
      )}
      style={{ paddingLeft: 8 + (level - 1) * INDENT }}
    >
      {isDir ? (
        status === "loading" ? (
          <Loader2 aria-hidden className="size-3.5 shrink-0 animate-spin text-fg-faint" />
        ) : status === "error" ? (
          <TriangleAlert aria-hidden className="size-3.5 shrink-0 text-danger" />
        ) : (
          <ChevronRight
            aria-hidden
            className={cn(
              "size-3.5 shrink-0 text-fg-faint transition-transform duration-(--dur-fast)",
              expanded && "rotate-90",
            )}
          />
        )
      ) : (
        <span aria-hidden className="size-3.5 shrink-0" />
      )}
      {isDir ? (
        <FolderGlyph aria-hidden className="size-4 shrink-0 text-fg-faint" />
      ) : (
        <FileIcon aria-hidden className="size-4 shrink-0 text-fg-faint" />
      )}
      <span className={cn("min-w-0 truncate", generated && "text-fg-faint")}>{entry.name}</span>
      {generated && <span className="sr-only">(generated)</span>}
      {layer && (
        <>
          <span aria-hidden className={cn("ml-auto size-1.5 shrink-0 rounded-full", DOT[layer])} />
          <span className="sr-only">({LAYERS[layer].label} layer)</span>
        </>
      )}
      {status === "error" && (
        <span className="sr-only">(couldn&apos;t load; press Enter to retry)</span>
      )}
    </div>
  );
}

/**
 * The repository's files as a tree: directories load as they are opened, only the rows on screen
 * are rendered (a flat list of the visible rows, virtualized), and it follows the ARIA tree
 * pattern: ↑↓ move, → opens, ← closes or goes up, Home/End, Enter opens a file, and typing a
 * name jumps to it. The current file is revealed (its directories opened, scrolled to) whenever
 * it changes, or on request.
 */
export function FileTree({ current, onOpenFile, defaultExpanded = [], className }: FileTreeProps) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(
    () => new Set([...defaultExpanded, ...ancestorsOf(current)]),
  );
  const [activePath, setActivePath] = useState(current);
  // The row to scroll to once it is listed (after a reveal opens its directories).
  const [pendingReveal, setPendingReveal] = useState<string | null>(current || null);
  const [revealed, setRevealed] = useState(current);
  if (revealed !== current) {
    // The open file changed (a link, Back, the breadcrumb): reveal it.
    setRevealed(current);
    setActivePath(current);
    setPendingReveal(current || null);
    if (ancestorsOf(current).some((dir) => !expanded.has(dir))) {
      setExpanded(new Set([...expanded, ...ancestorsOf(current)]));
    }
  }

  const dirs = useMemo(() => ["", ...[...expanded].sort()], [expanded]);
  const { states, errors, refetch } = useListings(dirs);
  const rows = useMemo(() => flattenTree((path) => states.get(path), expanded), [states, expanded]);
  const indexOf = useMemo(() => new Map(rows.map((row, index) => [row.entry.path, index])), [rows]);
  const activeIndex = indexOf.get(activePath) ?? (rows.length > 0 ? 0 : -1);

  const scroller = useRef<HTMLDivElement>(null);
  const baseId = useId();
  const rowId = (index: number) => `${baseId}-row-${String(index)}`;
  // The focused row is always rendered, so aria-activedescendant always points at an element.
  const rangeExtractor = useCallback(
    (range: Range) => {
      const shown = defaultRangeExtractor(range);
      return activeIndex >= 0 && !shown.includes(activeIndex)
        ? [...shown, activeIndex].sort((a, b) => a - b)
        : shown;
    },
    [activeIndex],
  );
  // TanStack Virtual returns fresh functions each render, which the React Compiler can't
  // memoize; this component simply isn't compiled.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    getItemKey: (index) => rows[index]?.entry.path ?? index,
    overscan: 16,
    rangeExtractor,
    // Start with a screenful of rows rather than none until the panel is measured.
    initialRect: { width: 280, height: 720 },
  });

  // Once the revealed path is listed: open it if it is a directory, focus it and scroll to it.
  useEffect(() => {
    if (pendingReveal === null) return;
    const index = indexOf.get(pendingReveal);
    if (index === undefined) {
      const listed = ["", ...ancestorsOf(pendingReveal)].every(
        (dir) => states.get(dir)?.status === "loaded",
      );
      if (listed) setPendingReveal(null); // every directory above is listed: it isn't there
      return;
    }
    const row = rows[index];
    if (row?.entry.type === "dir" && !row.expanded) {
      setExpanded((open) => new Set([...open, pendingReveal]));
    }
    setActivePath(pendingReveal);
    virtualizer.scrollToIndex(index, { align: "auto" });
    setPendingReveal(null);
  }, [pendingReveal, indexOf, rows, states, virtualizer]);

  const toggle = (path: string) => {
    setExpanded((open) => {
      const next = new Set(open);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const activate = (index: number) => {
    const row = rows[index];
    if (!row) return;
    setActivePath(row.entry.path);
    if (row.entry.type === "file") onOpenFile(row.entry.path);
    else if (row.status === "error") refetch(row.entry.path);
    else toggle(row.entry.path);
  };

  const focusRow = (index: number) => {
    const row = rows[index];
    if (!row) return;
    setActivePath(row.entry.path);
    virtualizer.scrollToIndex(index, { align: "auto" });
  };

  const typed = useRef({ text: "", at: 0 });
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key.length === 1 && event.key !== " ") {
      // Type-ahead: letters typed in quick succession make one prefix.
      const now = event.timeStamp;
      const text =
        now - typed.current.at < TYPE_AHEAD_MS ? typed.current.text + event.key : event.key;
      typed.current = { text, at: now };
      const found = typeAhead(rows, activeIndex, text);
      if (found !== -1) focusRow(found);
      // Typing belongs to the tree, not the page's single-key shortcuts ("g c", "j").
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    typed.current = { text: "", at: 0 }; // any other key ends the typed prefix
    const action = treeKeyAction(event.key, rows, activeIndex);
    if (!action) return;
    event.preventDefault();
    event.stopPropagation();
    if (action.type === "focus") focusRow(action.index);
    else if (action.type === "activate") activate(action.index);
    else if (action.type === "expand") setExpanded((open) => new Set([...open, action.path]));
    else setExpanded((open) => new Set([...open].filter((path) => path !== action.path)));
  };

  const root = states.get("");
  const rootError = errors.get("");
  return (
    <section aria-label="Files" className={cn("flex min-h-0 flex-col", className)}>
      <header className="flex h-10 shrink-0 items-center gap-1 border-b border-line px-3">
        <h2 className="flex-1 text-sm font-medium">Files</h2>
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Reveal the current file"
                disabled={!current}
                onClick={() => {
                  setExpanded(new Set([...expanded, ...ancestorsOf(current)]));
                  setPendingReveal(current);
                  scroller.current?.focus();
                }}
              />
            }
          >
            <LocateFixed aria-hidden />
          </TooltipTrigger>
          <TooltipContent>Reveal the current file</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Collapse all folders"
                onClick={() => {
                  setExpanded(new Set());
                  virtualizer.scrollToOffset(0);
                }}
              />
            }
          >
            <ChevronsDownUp aria-hidden />
          </TooltipTrigger>
          <TooltipContent>Collapse all folders</TooltipContent>
        </Tooltip>
      </header>
      {root?.status === "loaded" ? (
        <div
          ref={scroller}
          role="tree"
          aria-label="Repository files"
          aria-activedescendant={activeIndex >= 0 ? rowId(activeIndex) : undefined}
          tabIndex={0}
          onKeyDown={onKeyDown}
          className="group/tree min-h-0 flex-1 overflow-auto py-1 outline-none"
        >
          {rows.length === 0 ? (
            <p className="px-3 py-2 text-sm text-fg-muted">This repository has no files.</p>
          ) : (
            <div role="none" className="relative" style={{ height: virtualizer.getTotalSize() }}>
              {virtualizer.getVirtualItems().map((item) => {
                const row = rows[item.index];
                if (!row) return null;
                return (
                  <div
                    key={item.key}
                    role="none"
                    className="absolute inset-x-0 top-0"
                    style={{ height: item.size, transform: `translateY(${String(item.start)}px)` }}
                  >
                    <TreeItem
                      row={row}
                      id={rowId(item.index)}
                      active={item.index === activeIndex}
                      current={row.entry.path === current}
                      onPick={() => {
                        scroller.current?.focus({ preventScroll: true });
                        activate(item.index);
                      }}
                    />
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ) : root?.status === "error" ? (
        <div className="p-3">
          <ErrorState
            title="Couldn't load the files"
            description={rootError instanceof ApiError ? rootError.message : undefined}
            onRetry={() => {
              refetch("");
            }}
            {...(rootError instanceof ApiError && rootError.requestId
              ? { requestId: rootError.requestId }
              : {})}
          />
        </div>
      ) : (
        <div role="status" aria-label="Loading files" className="flex flex-col gap-2 p-3">
          {Array.from({ length: 8 }, (_, index) => (
            <Skeleton
              key={index}
              className="h-4"
              style={{ width: `${String(50 + ((index * 37) % 40))}%` }}
            />
          ))}
        </div>
      )}
      <ul
        aria-label="Layers"
        className="flex shrink-0 flex-wrap gap-x-3 gap-y-1 border-t border-line px-3 py-2 text-xs text-fg-muted"
      >
        {LAYER_ORDER.map((layer) => (
          <li key={layer} className="flex items-center gap-1.5">
            <span aria-hidden className={cn("size-1.5 rounded-full", DOT[layer])} />
            <span aria-hidden>{LAYERS[layer].glyph}</span>
            {LAYERS[layer].label}
          </li>
        ))}
      </ul>
    </section>
  );
}
