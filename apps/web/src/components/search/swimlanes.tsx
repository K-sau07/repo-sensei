"use client";

import { PreviewCard } from "@base-ui/react/preview-card";
import { cn } from "cn";
import { Loader2 } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import { CodeHoverCard, type CodePreview } from "@/components/code/code-hover-card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/errors";
import { LAYERS, type Layer, type SearchHit } from "@/lib/api/search";

import { LANES, plural } from "./meta";
import { ResultCard, type CardTarget } from "./result-card";
import { LANE_ATTR } from "./use-result-keys";
import { useLanePages, type SearchResult } from "./use-search";

/** A lane with no hits narrows to its header, so the others keep their places. */
const COLLAPSED = "2.75rem";
const OPEN = "minmax(15rem, 1fr)";

export const laneId = (base: string, layer: string) => `${base}-lane-${layer}`;

function LaneHeader({
  layer,
  count,
  id,
  collapsed,
  filtered,
}: {
  layer: Layer;
  count: number;
  id: string;
  collapsed: boolean;
  filtered: boolean;
}) {
  const { label, glyph, text, rule } = LANES[layer];
  const total = filtered ? "filtered out" : plural(count, "result");
  return (
    <h3
      id={id}
      className={cn(
        "flex items-center gap-1.5 text-xs font-medium",
        collapsed && "flex-col gap-2 [writing-mode:vertical-rl]",
      )}
    >
      <span aria-hidden className={cn("h-3 w-0.5 rounded-full", collapsed && "h-0.5 w-3", rule)} />
      <span aria-hidden className={text}>
        {glyph}
      </span>
      <span aria-hidden className="text-fg">
        {label}
      </span>
      <span aria-hidden className="text-fg-muted tabular-nums">
        {collapsed && filtered ? "–" : count.toLocaleString("en-US")}
      </span>
      <span className="sr-only">{`${label}, ${total}`}</span>
    </h3>
  );
}

interface LaneProps {
  layer: Layer;
  base: string;
  result: SearchResult;
  target: CardTarget;
  preview: PreviewCard.Handle<CodePreview>;
  filtered: boolean;
  /** The one lane a narrow screen shows. */
  visible: boolean;
  active: string | null;
  onActive: (id: string) => void;
}

function Lane({
  layer,
  base,
  result,
  target,
  preview,
  filtered,
  visible,
  active,
  onActive,
}: LaneProps) {
  const [requested, setRequested] = useState(false);
  const pages = useLanePages(result.request, layer, result.next[layer], requested);
  const count = result.counts[layer];
  const hits: SearchHit[] = [
    ...result.lanes[layer],
    ...(pages.data?.pages.flatMap((page) => page.lanes[layer]) ?? []),
  ];
  const id = laneId(base, layer);
  const headerId = `${id}-heading`;
  const collapsed = count === 0;
  const more = requested ? pages.hasNextPage : result.next[layer] !== null;
  const left = Math.max(0, count - hits.length);
  const roving = hits.some((hit) => hit.id === active) ? active : (hits[0]?.id ?? null);
  const pageError = pages.error instanceof ApiError ? pages.error : null;

  return (
    <section
      id={id}
      aria-labelledby={headerId}
      {...{ [LANE_ATTR]: layer }}
      className={cn(
        "min-w-0 flex-col gap-2",
        visible ? "flex" : "hidden @3xl:flex",
        collapsed &&
          "@3xl:items-center @3xl:rounded-[10px] @3xl:border @3xl:border-dashed @3xl:border-line @3xl:py-3",
      )}
    >
      <LaneHeader
        layer={layer}
        count={count}
        id={headerId}
        collapsed={collapsed}
        filtered={filtered}
      />
      {collapsed ? (
        <p className="text-xs text-fg-muted @3xl:sr-only">
          {filtered ? "This layer is filtered out." : "No results in this layer."}
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {hits.map((hit) => (
            <ResultCard
              key={hit.id}
              hit={hit}
              target={target}
              preview={preview}
              tabbable={hit.id === roving}
              onFocus={() => {
                onActive(hit.id);
              }}
            />
          ))}
        </ul>
      )}
      {pageError && (
        <p role="alert" className="text-xs text-danger">
          {pageError.code === "invalid_cursor"
            ? "These results changed. Search again to see the rest."
            : "Couldn't load more."}
        </p>
      )}
      {more && (
        <Button
          variant="ghost"
          size="sm"
          className="self-start"
          disabled={pages.isFetching}
          onClick={() => {
            if (!requested) setRequested(true);
            else void pages.fetchNextPage();
          }}
        >
          {pages.isFetching && <Loader2 aria-hidden className="animate-spin" />}
          More<span className="sr-only"> {LANES[layer].label} results</span>{" "}
          <span className="text-fg-muted tabular-nums">({left.toLocaleString("en-US")})</span>
        </Button>
      )}
    </section>
  );
}

/** Lanes to switch between on a narrow screen, one shown at a time. */
function LanePicker({
  result,
  base,
  visible,
  onPick,
}: {
  result: SearchResult;
  base: string;
  visible: Layer;
  onPick: (layer: Layer) => void;
}) {
  return (
    <div role="group" aria-label="Lane" className="flex gap-1 overflow-x-auto @3xl:hidden">
      {LAYERS.map((layer) => {
        const count = result.counts[layer];
        return (
          <Button
            key={layer}
            variant={layer === visible ? "secondary" : "ghost"}
            size="sm"
            aria-pressed={layer === visible}
            aria-controls={laneId(base, layer)}
            disabled={count === 0}
            onClick={() => {
              onPick(layer);
            }}
          >
            <span aria-hidden className={LANES[layer].text}>
              {LANES[layer].glyph}
            </span>
            {LANES[layer].label}
            <span className="text-fg-muted tabular-nums">{count}</span>
          </Button>
        );
      })}
    </div>
  );
}

export interface SwimlanesProps {
  result: SearchResult;
  target: CardTarget;
  /** Lanes the filter leaves out. */
  layers: readonly Layer[];
  visible: Layer | null;
  onVisible: (layer: Layer) => void;
  /** Dimmed while the next query's results come. */
  stale?: boolean;
  children?: ReactNode;
}

/** The results in layer lanes: UI | API | Service | DB | Tests/Config. */
export function Swimlanes({
  result,
  target,
  layers,
  visible,
  onVisible,
  stale = false,
}: SwimlanesProps) {
  const base = useId();
  const [preview] = useState(() => PreviewCard.createHandle<CodePreview>());
  const [active, setActive] = useState<string | null>(null);
  const shown = visible ?? LAYERS.find((layer) => result.counts[layer] > 0) ?? "ui";
  const columns = LAYERS.map((layer) => (result.counts[layer] > 0 ? OPEN : COLLAPSED)).join(" ");
  // A lane's further pages are its query's: the next query starts every lane afresh.
  const query = JSON.stringify(result.request);
  return (
    <div className={cn("flex min-h-0 flex-col gap-3", stale && "opacity-60")} aria-busy={stale}>
      <LanePicker result={result} base={base} visible={shown} onPick={onVisible} />
      <div
        className="grid grid-cols-1 gap-3 @3xl:[grid-template-columns:var(--lanes)] @3xl:overflow-x-auto"
        style={{ "--lanes": columns } as React.CSSProperties}
      >
        {LAYERS.map((layer) => (
          <Lane
            key={`${layer} ${query}`}
            layer={layer}
            base={base}
            result={result}
            target={target}
            preview={preview}
            filtered={layers.length > 0 && !layers.includes(layer)}
            visible={layer === shown}
            active={active}
            onActive={setActive}
          />
        ))}
      </div>
      <CodeHoverCard handle={preview} />
    </div>
  );
}

/** Loading: lanes shaped like the results, so nothing moves when they come. */
export function SwimlanesSkeleton() {
  return (
    <div role="status" aria-label="Searching" className="flex flex-col gap-3">
      <div className="flex gap-1 @3xl:hidden">
        {LAYERS.map((layer) => (
          <Skeleton key={layer} className="h-7 w-16" />
        ))}
      </div>
      <div className="grid grid-cols-1 gap-3 @3xl:grid-cols-5">
        {LAYERS.map((layer, lane) => (
          <div
            key={layer}
            className={cn("flex-col gap-2", lane === 0 ? "flex" : "hidden @3xl:flex")}
          >
            <div className="flex items-center gap-1.5 text-xs font-medium">
              <span aria-hidden className={cn("h-3 w-0.5 rounded-full", LANES[layer].rule)} />
              <span className="text-fg-muted">{LANES[layer].label}</span>
            </div>
            {Array.from({ length: 3 }, (_, index) => (
              <Skeleton key={index} className="h-36 rounded-[10px]" />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
