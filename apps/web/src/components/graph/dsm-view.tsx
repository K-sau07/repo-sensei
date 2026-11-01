"use client";

/**
 * The DSM tab (6.7): the level as a dependency structure matrix on a canvas, its groups in layer
 * order. Row r, column c: how much r depends on c; darker is more, red is a pair in one cycle.
 * Hovering a cell names it; clicking one lists the lines of code behind it. Below: each group's
 * coupling (what depends on it, what it depends on, how unstable it is), and every dependency as
 * a list, for a keyboard or a screen reader.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { LEVEL_LABELS, type DepLevel, type Dsm, type DsmCell } from "@/lib/api/deps";
import { ApiError } from "@/lib/api/errors";

import { useGraphColors, type GraphColors } from "./graph-colors";
import { EdgePanel } from "./graph-panels";
import { useGraphState } from "./graph-state";
import { LevelSelect } from "./level-select";
import { useDsm } from "./use-deps";

const SIDE = 560; // the matrix's width at most (px)
const MIN_CELL = 3;
const MAX_CELL = 28;
const LABELLED = 60; // groups up to which the rows are named beside the matrix
const LISTED = 500;
const MAX_CANVAS = 4_096;

/** The canvas's pixels per CSS pixel: the screen's, within what browsers draw (4096² is safe). */
function pixelRatio(side: number): number {
  const screen = typeof window === "undefined" ? 1 : window.devicePixelRatio || 1;
  return Math.max(0.25, Math.min(screen, MAX_CANVAS / Math.max(1, side)));
}

export function cellSize(groups: number): number {
  return Math.max(MIN_CELL, Math.min(MAX_CELL, Math.floor(SIDE / Math.max(1, groups))));
}

export function DsmView() {
  const [state, setState] = useGraphState();
  const level = state.level;
  const dsm = useDsm(level);
  const colors = useGraphColors();
  const [picked, setPicked] = useState<DsmCell | null>(null);

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex items-center gap-3">
        <LevelSelect
          value={level}
          onChange={(value) => {
            setPicked(null);
            void setState({ level: value });
          }}
        />
        {dsm.data ? (
          <p className="text-xs text-fg-muted">
            {dsm.data.order.length} {LEVEL_LABELS[level].toLowerCase()} ·{" "}
            {dsm.data.cycles.length === 0
              ? "no cycles"
              : `${String(dsm.data.cycles.length)} ${dsm.data.cycles.length === 1 ? "cycle" : "cycles"}`}
          </p>
        ) : null}
      </div>
      {dsm.isPending || colors === null ? (
        <Skeleton className="h-96 w-full max-w-xl" />
      ) : dsm.isError ? (
        dsm.error instanceof ApiError && dsm.error.code === "dsm_too_large" ? (
          <p className="text-sm text-fg-muted" data-testid="dsm-too-large">
            Too many {LEVEL_LABELS[level].toLowerCase()} to draw as a matrix: pick a coarser level.
          </p>
        ) : (
          <ErrorState title="Couldn't load the matrix" onRetry={() => void dsm.refetch()} />
        )
      ) : dsm.data.order.length === 0 ? (
        <p className="text-sm text-fg-muted">Nothing at this level depends on anything.</p>
      ) : (
        <div className="flex flex-wrap items-start gap-6">
          <Matrix dsm={dsm.data} colors={colors} picked={picked} onPick={setPicked} />
          {picked ? (
            <div className="w-80 rounded-md border border-line">
              <EdgePanel
                edge={{
                  source: dsm.data.order[picked.row]?.id ?? "",
                  target: dsm.data.order[picked.col]?.id ?? "",
                }}
                edges={[]}
                levelOf={
                  new Map<string, DepLevel>([
                    [dsm.data.order[picked.row]?.id ?? "", level],
                    [dsm.data.order[picked.col]?.id ?? "", level],
                  ])
                }
                onClose={() => {
                  setPicked(null);
                }}
              />
            </div>
          ) : null}
        </div>
      )}
      {dsm.data && dsm.data.order.length > 0 ? (
        <>
          <Coupling dsm={dsm.data} />
          <details className="text-sm">
            <summary className="cursor-pointer text-fg-muted">Every dependency, as a list</summary>
            <ul className="mt-2 flex flex-col">
              {dsm.data.cells.slice(0, LISTED).map((cell) => (
                <li key={`${String(cell.row)}:${String(cell.col)}`}>
                  <button
                    type="button"
                    className="text-left text-xs hover:underline"
                    onClick={() => {
                      setPicked(cell);
                    }}
                  >
                    {dsm.data.order[cell.row]?.label} depends on {dsm.data.order[cell.col]?.label}:{" "}
                    {cell.count}
                    {cell.cycle ? " (in a cycle)" : ""}
                  </button>
                </li>
              ))}
            </ul>
          </details>
        </>
      ) : null}
    </div>
  );
}

function Matrix({
  dsm,
  colors,
  picked,
  onPick,
}: {
  dsm: Dsm;
  colors: GraphColors;
  picked: DsmCell | null;
  onPick: (cell: DsmCell) => void;
}) {
  const n = dsm.order.length;
  const cell = cellSize(n);
  const side = n * cell;
  const canvas = useRef<HTMLCanvasElement>(null);
  const [hover, setHover] = useState<DsmCell | null>(null);
  const at = useMemo(() => new Map(dsm.cells.map((c) => [c.row * n + c.col, c])), [dsm.cells, n]);
  const most = useMemo(() => Math.max(1, ...dsm.cells.map((c) => c.count)), [dsm.cells]);

  useEffect(() => {
    const context = canvas.current?.getContext("2d");
    if (!context) return;
    const ratio = pixelRatio(side);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, side, side);
    context.fillStyle = colors.edge;
    context.globalAlpha = 0.12;
    for (let i = 0; i < n; i++) context.fillRect(i * cell, i * cell, cell, cell); // the diagonal
    for (const c of dsm.cells) {
      context.globalAlpha = 0.25 + (0.75 * Math.log(c.count + 1)) / Math.log(most + 1);
      context.fillStyle = c.cycle ? colors.cycle : colors.lit;
      context.fillRect(c.col * cell, c.row * cell, cell, cell);
    }
    context.globalAlpha = 1;
    if (cell >= 6) {
      context.strokeStyle = colors.edge;
      context.globalAlpha = 0.15;
      context.lineWidth = 1;
      for (let i = 0; i <= n; i++) {
        context.beginPath();
        context.moveTo(i * cell + 0.5, 0);
        context.lineTo(i * cell + 0.5, side);
        context.moveTo(0, i * cell + 0.5);
        context.lineTo(side, i * cell + 0.5);
        context.stroke();
      }
      context.globalAlpha = 1;
    }
    if (picked) {
      context.strokeStyle = colors.label;
      context.lineWidth = 2;
      context.strokeRect(picked.col * cell + 1, picked.row * cell + 1, cell - 2, cell - 2);
    }
  }, [dsm.cells, n, cell, side, most, colors, picked]);

  const cellAt = (event: React.MouseEvent<HTMLCanvasElement>): DsmCell | null => {
    const box = event.currentTarget.getBoundingClientRect();
    const col = Math.floor((event.clientX - box.left) / cell);
    const row = Math.floor((event.clientY - box.top) / cell);
    return at.get(row * n + col) ?? null;
  };
  const ratio = pixelRatio(side);
  const named = (c: DsmCell) =>
    `${dsm.order[c.row]?.label ?? ""} depends on ${dsm.order[c.col]?.label ?? ""}: ${String(c.count)}${c.cycle ? ", in a cycle" : ""}`;
  return (
    <figure className="flex flex-col gap-2">
      <div className="flex">
        {n <= LABELLED ? (
          <ol
            aria-hidden
            className="mr-2 flex flex-col text-right text-[10px] leading-none text-fg-muted"
          >
            {dsm.order.map((g, i) => (
              <li
                key={g.id}
                style={{ height: cell }}
                className="flex items-center justify-end truncate"
                title={g.id}
              >
                {g.label} <span className="ml-1 text-fg-faint tabular-nums">{i + 1}</span>
              </li>
            ))}
          </ol>
        ) : null}
        <canvas
          ref={canvas}
          data-testid="dsm-canvas"
          data-cells={dsm.cells.length}
          data-cycle-cells={dsm.cells.filter((c) => c.cycle).length}
          aria-label={`A ${String(n)} by ${String(n)} dependency matrix; every dependency is listed below`}
          role="img"
          width={side * ratio}
          height={side * ratio}
          style={{ width: side, height: side }}
          className="cursor-crosshair rounded-sm border border-line"
          onMouseMove={(event) => {
            setHover(cellAt(event));
          }}
          onMouseLeave={() => {
            setHover(null);
          }}
          onClick={(event) => {
            const found = cellAt(event);
            if (found) onPick(found);
          }}
        />
      </div>
      <figcaption className="h-4 text-xs text-fg-muted" data-testid="dsm-hover">
        {hover ? named(hover) : "Row depends on column. Red: both in one cycle."}
      </figcaption>
    </figure>
  );
}

function Coupling({ dsm }: { dsm: Dsm }) {
  return (
    <Table className="max-w-2xl">
      <TableCaption>
        Coupling: what depends on each, what it depends on, and its instability
      </TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead className="text-right">Depended on by (Ca)</TableHead>
          <TableHead className="text-right">Depends on (Ce)</TableHead>
          <TableHead className="text-right">Instability</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {dsm.order.slice(0, LISTED).map((g) => (
          <TableRow key={g.id}>
            <TableCell title={g.id}>{g.label}</TableCell>
            <TableCell className="text-right tabular-nums">{g.afferent}</TableCell>
            <TableCell className="text-right tabular-nums">{g.efferent}</TableCell>
            <TableCell className="text-right tabular-nums">
              {g.instability === null ? "–" : g.instability.toFixed(2)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
