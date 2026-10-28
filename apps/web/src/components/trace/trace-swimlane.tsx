"use client";

/**
 * A trace path as swimlanes (3.15): a lane per layer, top to bottom as a request travels (the
 * component and its hooks, the HTTP call, the endpoint, the handler, the service, the data), and
 * the path's hops left to right, each card in its lane: what it is, its name, how it was reached
 * and where (the call site's `file:line`), and its pip. A lane the path doesn't use stays, thin,
 * so a skipped layer shows. The Trace Beam runs through the cards; a gap is a dashed card where
 * the next hop would be.
 *
 * The cards are an ordered list in the path's order, so screen readers and the keyboard get the
 * hops in order (the lanes and the beam are decoration); the list view says the same at length.
 */
import { cn } from "cn";
import { Split } from "lucide-react";
import { useLayoutEffect, useRef, useState, type CSSProperties } from "react";

import {
  breaksByHop,
  edgeLabel,
  hopPlace,
  isAmbiguous,
  TRACE_LAYERS,
  type TraceHop,
  type TraceLayer,
  type TracePath,
} from "@/lib/api/trace";

import { HopPip } from "./hop-pip";
import { kindLabel, TRACE_LANES } from "./meta";
import { TraceBeam, type Anchor } from "./trace-beam";
import { TraceGap } from "./trace-gap";

const LABEL_COLUMN = "8.5rem";
const HOP_COLUMN = "11.5rem";
const GAP_COLUMN = "15rem";
const USED_ROW = "minmax(4.75rem, auto)";
const EMPTY_ROW = "1.75rem";

interface Measured {
  width: number;
  height: number;
  rows: string | null;
  anchors: Anchor[];
  gap: Anchor | null;
}

const UNMEASURED: Measured = { width: 0, height: 0, rows: null, anchors: [], gap: null };

const fileName = (path: string) => path.slice(path.lastIndexOf("/") + 1);

/** The lanes shown: every layer a request crosses, and "other" when the path has one. */
export function lanesOf(path: TracePath): TraceLayer[] {
  const used = new Set(path.hops.map((hop) => hop.layer));
  return TRACE_LAYERS.filter((layer) => layer !== "other" || used.has("other"));
}

/** Where a gap is drawn: in the lane after the one it stops in (where the next hop would be). */
export function gapLane(layer: TraceLayer): TraceLayer {
  const next = TRACE_LAYERS[TRACE_LAYERS.indexOf(layer) + 1];
  return next && next !== "other" ? next : layer;
}

/** The words a hop's card (and its row in the list) says, for screen readers too. */
export function hopSummary(hop: TraceHop, index: number, count: number): string {
  const place = hopPlace(hop);
  const how = hop.edge ? edgeLabel(hop.edge) : "entry";
  return [
    `Step ${String(index + 1)} of ${String(count)}`,
    TRACE_LANES[hop.layer].label,
    `${kindLabel(hop.node.kind)} ${hop.node.name}`,
    how,
    place ? `${place.path}:${String(place.start)}` : null,
  ]
    .filter(Boolean)
    .join(", ");
}

/** "1 of 2": the call site has other possible targets, and this path follows one. */
export function Alternatives({ hop }: { hop: TraceHop }) {
  if (!isAmbiguous(hop.edge)) return null;
  const { alternatives, type } = hop.edge;
  return (
    <span
      className="inline-flex shrink-0 items-center gap-0.5 text-warn tabular-nums"
      title={`Ambiguous: this ${type.replaceAll("_", " ")} site has ${String(alternatives)} possible targets; this path follows one`}
    >
      <Split aria-hidden className="size-3" />
      <span className="sr-only">ambiguous, </span>1 of {alternatives}
    </span>
  );
}

function HopCard({
  hop,
  index,
  count,
  selected,
  onSelect,
}: {
  hop: TraceHop;
  index: number;
  count: number;
  selected: boolean;
  onSelect: () => void;
}) {
  const lane = TRACE_LANES[hop.layer];
  const place = hopPlace(hop);
  return (
    <>
      <button
        type="button"
        aria-pressed={selected}
        onClick={onSelect}
        title={hop.node.qualified_name}
        className={cn(
          "flex h-[4.25rem] w-full flex-col justify-between rounded-[10px] border bg-surface-2 py-1.5 pr-7 pl-2.5 text-left text-xs shadow-(--elevation-1) outline-none hover:border-line-strong focus-visible:ring-2 focus-visible:ring-accent",
          selected ? "border-accent bg-surface-3" : "border-line",
        )}
      >
        <span className="sr-only">{hopSummary(hop, index, count)}</span>
        <span aria-hidden className={cn("flex min-w-0 items-center gap-1", lane.text)}>
          <span>{lane.glyph}</span>
          <span className="truncate">{kindLabel(hop.node.kind)}</span>
        </span>
        <span aria-hidden className="truncate font-mono text-sm text-fg">
          {hop.node.name}
        </span>
        <span className="flex min-w-0 items-center gap-1.5 text-fg-muted">
          <span aria-hidden className="shrink-0">
            {hop.edge ? edgeLabel(hop.edge) : "entry"}
          </span>
          {place && (
            <span aria-hidden className="truncate font-mono text-fg-faint">
              {fileName(place.path)}:{place.start}
            </span>
          )}
          <Alternatives hop={hop} />
        </span>
      </button>
      {hop.edge && <HopPip edge={hop.edge} className="absolute top-1 right-1" />}
    </>
  );
}

export function TraceSwimlane({
  path,
  selected,
  onSelect,
  pulse,
  onPulsed,
}: {
  path: TracePath;
  /** The hop whose code is open. */
  selected: number | null;
  onSelect: (hop: number | null) => void;
  /** Run the beam's pulse (the trace just resolved). */
  pulse: boolean;
  onPulsed?: () => void;
}) {
  const lanes = lanesOf(path);
  const used = new Set(path.hops.map((hop) => hop.layer));
  const breaks = breaksByHop(path);
  const gap = path.breaks.at(-1) ?? null;
  const gapAt = gap ? path.hops[gap.after_hop] : undefined;
  const gapRow = gapAt ? gapLane(gapAt.layer) : null;
  if (gapRow) used.add(gapRow);
  const row = (layer: TraceLayer) => lanes.indexOf(layer) + 1;
  const gapColumn = gap ? gap.after_hop + 3 : null;
  const columns = Math.max(path.hops.length, gapColumn ? gapColumn - 1 : 0);
  const template: CSSProperties = {
    gridTemplateColumns: [
      LABEL_COLUMN,
      ...Array.from({ length: columns }, (_, i) => (i + 2 === gapColumn ? GAP_COLUMN : HOP_COLUMN)),
    ].join(" "),
    gridTemplateRows: lanes.map((layer) => (used.has(layer) ? USED_ROW : EMPTY_ROW)).join(" "),
    columnGap: "2.25rem",
    rowGap: "0.375rem",
  };

  // The cards' places, measured once they're laid out (and again when the lanes resize).
  const list = useRef<HTMLOListElement>(null);
  const cards = useRef<(HTMLLIElement | null)[]>([]);
  const gapCard = useRef<HTMLLIElement>(null);
  const [box, setBox] = useState<Measured>(UNMEASURED);
  useLayoutEffect(() => {
    const element = list.current;
    if (!element) return;
    const measure = () => {
      const anchorOf = (card: HTMLElement | null, layer: TraceLayer): Anchor | null =>
        card
          ? {
              left: card.offsetLeft,
              right: card.offsetLeft + card.offsetWidth,
              y: card.offsetTop + card.offsetHeight / 2,
              layer,
            }
          : null;
      const anchors = path.hops.flatMap(
        (hop, i) => anchorOf(cards.current[i] ?? null, hop.layer) ?? [],
      );
      setBox({
        width: element.scrollWidth,
        height: element.scrollHeight,
        // The rows as laid out (a gap's card can make its lane taller), for the bands.
        rows: getComputedStyle(element).gridTemplateRows,
        anchors,
        gap: gapRow ? anchorOf(gapCard.current, gapRow) : null,
      });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, [path, gapRow]);

  const breakAnchor = gap ? box.anchors[gap.after_hop] : undefined;
  return (
    <div className="overflow-x-auto pb-2" data-testid="trace-swimlane">
      <div className="relative w-max min-w-full">
        {/* The lanes: a band and a label each, on the same grid as the cards. */}
        <div
          aria-hidden
          className="absolute inset-0 grid py-1"
          style={{ ...template, ...(box.rows ? { gridTemplateRows: box.rows } : {}) }}
        >
          {lanes.map((layer) => {
            const lane = TRACE_LANES[layer];
            return (
              <div
                key={layer}
                className={cn("flex items-center rounded-md", used.has(layer) ? lane.band : "")}
                style={{ gridRow: row(layer), gridColumn: "1 / -1" }}
              >
                <span
                  className={cn(
                    "sticky left-0 flex items-center gap-1.5 bg-transparent px-2 text-xs font-medium",
                    used.has(layer) ? lane.text : "text-fg-faint",
                  )}
                >
                  <span>{lane.glyph}</span>
                  {lane.label}
                </span>
              </div>
            );
          })}
        </div>
        <TraceBeam
          anchors={box.anchors}
          gap={breakAnchor && box.gap ? { from: breakAnchor, to: box.gap } : null}
          width={box.width}
          height={box.height}
          pulse={pulse}
          {...(onPulsed ? { onPulsed } : {})}
        />
        <ol ref={list} aria-label="Hops, in order" className="relative grid py-1" style={template}>
          {path.hops.map((hop, i) => (
            <li
              key={hop.node.symbol_key}
              ref={(element) => {
                cards.current[i] = element;
              }}
              className="relative self-center"
              style={{ gridRow: row(hop.layer), gridColumn: i + 2 }}
              data-hop={i}
              data-break={breaks.has(i) ? "" : undefined}
            >
              <HopCard
                hop={hop}
                index={i}
                count={path.hops.length}
                selected={selected === i}
                onSelect={() => {
                  onSelect(selected === i ? null : i);
                }}
              />
            </li>
          ))}
          {gap && gapRow && gapColumn && (
            <li
              ref={gapCard}
              className="self-center"
              style={{ gridRow: row(gapRow), gridColumn: gapColumn }}
            >
              <TraceGap path={path} gap={gap} />
            </li>
          )}
        </ol>
      </div>
    </div>
  );
}
