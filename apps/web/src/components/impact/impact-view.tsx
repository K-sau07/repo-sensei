"use client";

/**
 * The impact of one change (6.8): the heat map and, beside it, the ranked lists.
 *
 * - The map: the change at the center, each affected node on the ring of its distance, beside
 *   what it is reached through, an arrow to it; colored by score (signal → warn → danger) and
 *   sized by category (routes, screens, tests and tables larger). A ring pulses out once when the
 *   answer arrives (not with reduced motion).
 * - The lists: routes, screens, tests, tables, then the other code, each ranked, with how sure,
 *   how far and through what; a name lights its node, its place opens the code.
 * - How sure an edge must be to be followed; "Copy as PR note".
 */
import { ClipboardCopy, Radar } from "lucide-react";
import { useReducedMotionConfig } from "motion/react";
import * as m from "motion/react-m";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { ErrorState } from "@/components/feedback/error-state";
import { useGraphColors } from "@/components/graph/graph-colors";
import { writeClipboard } from "@/components/primitives/copy-button";
import { useViewRef } from "@/components/repo/use-ref";
import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { $api } from "@/lib/api/client";
import type { DepEdge, ShownNode } from "@/lib/api/deps";
import { ApiError } from "@/lib/api/errors";
import {
  CATEGORY_LABELS,
  IMPACT_CATEGORIES,
  allAffected,
  blastLayout,
  heatColor,
  impactHref,
  percent,
  prNote,
  type Impact,
  type ImpactCategory,
} from "@/lib/api/impact";
import { codePath } from "@/lib/routes";

const GraphCanvas = dynamic(() => import("@/components/graph/graph-canvas"), {
  ssr: false,
  loading: () => <div className="size-full" />,
});

const SURENESS = [
  { value: "0.3", label: "30% sure or more" },
  { value: "0.5", label: "50% sure or more" },
  { value: "0.7", label: "70% sure or more" },
  { value: "0.9", label: "90% sure or more" },
] as const;
const SIZES: Record<ImpactCategory | "changed", number> = {
  changed: 14,
  routes: 9,
  screens: 9,
  tests: 7,
  tables: 9,
  symbols: 5,
};
const LISTED = 50;

function useImpact(symbol: string | null, files: readonly string[], minConf: number) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/impact",
    {
      params: {
        path: { org, repo },
        query: { ...(symbol ? { symbol } : { files: [...files] }), min_conf: minConf, ref },
      },
    },
    { staleTime: 5 * 60_000, retry: false },
  );
}

export function ImpactView({ symbol, files }: { symbol: string | null; files: string[] }) {
  const { org, repo } = useRepoSlug();
  const [minConf, setMinConf] = useState("0.5");
  const impact = useImpact(symbol, files, Number(minConf));
  const colors = useGraphColors();
  const still = useReducedMotionConfig() ?? false;
  const [selected, setSelected] = useState<string | null>(null);

  const drawn = useMemo(() => {
    if (!impact.data || !colors) return null;
    const data = impact.data;
    const items = allAffected(data);
    const placed = blastLayout(data.changed, items);
    const category = new Map<string, ImpactCategory>();
    for (const c of IMPACT_CATEGORIES)
      for (const item of data.affected[c]) category.set(item.key, c);
    const ramp = { low: colors.lit, mid: colors.warn, high: colors.cycle };
    const nodes: ShownNode[] = [
      ...data.changed.map((n) => ({ key: n.key, name: n.name, score: 1 })),
      ...items.map((a) => ({ key: a.key, name: a.name, score: a.score })),
    ].map(({ key, name }) => ({
      id: key,
      label: name,
      layer: "other",
      size: 1,
      cluster: null,
      x: placed.get(key)?.x ?? 0,
      y: placed.get(key)?.y ?? 0,
      level: "symbol",
    }));
    const edges: DepEdge[] = items.flatMap((a) =>
      a.via === null
        ? []
        : [
            {
              source: a.key,
              target: a.via,
              weight: 1,
              types: { [a.edge ?? "depends"]: 1 },
              confidence: a.score,
              samples: [],
            },
          ],
    );
    const sizes = new Map<string, number>([
      ...data.changed.map((n) => [n.key, SIZES.changed] as const),
      ...items.map((a) => [a.key, SIZES[category.get(a.key) ?? "symbols"]] as const),
    ]);
    const paint = new Map<string, string>([
      ...data.changed.map((n) => [n.key, colors.cycle] as const),
      ...items.map((a) => [a.key, heatColor(a.score, ramp)] as const),
    ]);
    return { nodes, edges, sizes, paint };
  }, [impact.data, colors]);

  if (impact.isError) {
    const notFound = impact.error instanceof ApiError && impact.error.code === "impact_not_found";
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 p-8">
        <ErrorState
          title={notFound ? "Nothing in the code graph is that" : "Couldn't work out the impact"}
          description={
            notFound
              ? `No symbol or file here matches ${symbol ? `“${symbol}”` : "the files"}.`
              : undefined
          }
          {...(notFound ? {} : { onRetry: () => void impact.refetch() })}
        />
        <Link href={impactHref(org, repo)} className="text-sm underline underline-offset-4">
          Pick something else
        </Link>
      </div>
    );
  }
  const data = impact.data;
  const names = data?.changed.map((n) => n.name).join(", ") ?? symbol ?? files.join(", ");
  const total = data ? allAffected(data).length : 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2">
        <Radar aria-hidden className="size-4 text-fg-muted" />
        <h1 className="min-w-0 truncate text-sm font-medium">
          Impact of <span className="font-mono">{names}</span>
        </h1>
        {data ? (
          <p className="text-xs text-fg-muted" data-testid="impact-total">
            {total === 0 ? "Nothing depends on it" : `${String(total)} affected`}
            {data.truncated ? " (the surest shown)" : ""}
          </p>
        ) : null}
        <div className="ml-auto flex items-center gap-2">
          <Select
            value={minConf}
            onValueChange={(value) => {
              if (value) setMinConf(value);
            }}
          >
            <SelectTrigger className="w-44" aria-label="Edges followed">
              <SelectValue>{SURENESS.find((s) => s.value === minConf)?.label}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {SURENESS.map((s) => (
                <SelectItem key={s.value} value={s.value}>
                  {s.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            size="sm"
            variant="outline"
            disabled={!data}
            onClick={() => {
              if (!data) return;
              writeClipboard(prNote(data)).then(
                () => toast.success("Copied the impact as a PR note"),
                () => toast.error("Couldn't copy: the clipboard is out of reach"),
              );
            }}
          >
            <ClipboardCopy aria-hidden />
            Copy as PR note
          </Button>
        </div>
      </header>
      <div className="flex min-h-0 flex-1">
        <div
          className="relative h-[calc(100dvh-12rem)] min-h-[28rem] flex-1 overflow-hidden"
          data-testid="impact-canvas"
          data-nodes={drawn?.nodes.length ?? 0}
        >
          {!drawn || !colors ? (
            <div role="status" aria-label="Working out the impact" className="p-6">
              <Skeleton className="h-64" />
            </div>
          ) : (
            <>
              <GraphCanvas
                view={{
                  nodes: drawn.nodes,
                  edges: drawn.edges,
                  sizes: drawn.sizes,
                  colors,
                  focus: null,
                  found: new Set(),
                  path: null,
                  cycles: new Set(),
                  selected,
                  paint: drawn.paint,
                }}
                label={`The impact of ${names}: ${String(total)} affected, the surest nearest the center`}
                still={still}
                onSelectNode={setSelected}
                // Rings of long test names: fewer labels, apart (the rest named on hover).
                labelSpacing={220}
              />
              {!still ? (
                <m.div
                  aria-hidden
                  data-testid="impact-pulse"
                  className="pointer-events-none absolute top-1/2 left-1/2 size-24 -translate-1/2 rounded-full border-2 border-danger"
                  initial={{ scale: 0.2, opacity: 0.8 }}
                  animate={{ scale: 6, opacity: 0 }}
                  transition={{ duration: 1.4, ease: "easeOut" }}
                />
              ) : null}
              <HeatLegend />
            </>
          )}
        </div>
        <aside
          aria-label="Affected"
          className="flex w-96 shrink-0 flex-col gap-4 overflow-y-auto border-l border-line p-4"
        >
          {!data ? (
            <Skeleton className="h-40" />
          ) : total === 0 ? (
            <p className="text-sm text-fg-muted">
              Nothing in the code graph depends on it (following edges {percent(Number(minConf))}{" "}
              sure or more).
            </p>
          ) : (
            IMPACT_CATEGORIES.map((category) => (
              <AffectedList
                key={category}
                impact={data}
                category={category}
                selected={selected}
                onSelect={setSelected}
              />
            ))
          )}
        </aside>
      </div>
    </div>
  );
}

function HeatLegend() {
  return (
    <div
      aria-hidden
      className="absolute bottom-2 left-2 flex items-center gap-2 rounded-sm border border-line bg-surface-1/90 px-2 py-1 text-xs text-fg-muted"
    >
      Less sure, further
      <span className="h-2 w-24 rounded-full bg-gradient-to-r from-signal via-warn to-danger" />
      Surer, nearer
    </div>
  );
}

function AffectedList({
  impact,
  category,
  selected,
  onSelect,
}: {
  impact: Impact;
  category: ImpactCategory;
  selected: string | null;
  onSelect: (key: string) => void;
}) {
  const { org, repo } = useRepoSlug();
  const items = impact.affected[category];
  const names = new Map([
    ...impact.changed.map((n) => [n.key, n.name] as const),
    ...allAffected(impact).map((a) => [a.key, a.name] as const),
  ]);
  const id = `impact-${category}`;
  const list = (
    <ol aria-labelledby={id} className="flex flex-col gap-0.5">
      {items.slice(0, LISTED).map((item) => (
        <li
          key={item.key}
          data-selected={item.key === selected ? "" : undefined}
          className="rounded-sm px-2 py-1 data-[selected]:bg-surface-2"
        >
          <div className="flex items-baseline gap-2">
            <button
              type="button"
              className="min-w-0 flex-1 truncate text-left font-mono text-xs hover:underline"
              onClick={() => {
                onSelect(item.key);
              }}
            >
              {item.name}
            </button>
            <span className="text-xs tabular-nums">{percent(item.score)}</span>
            <span className="w-12 text-right text-xs text-fg-muted tabular-nums">
              {item.distance} {item.distance === 1 ? "hop" : "hops"}
            </span>
          </div>
          <div className="flex gap-2 text-[11px] text-fg-faint">
            {item.path ? (
              <Link
                href={codePath(org, repo, item.path, {
                  lines: `${String(item.start_line)}-${String(item.end_line)}`,
                })}
                className="min-w-0 truncate hover:underline"
              >
                {item.path}:{item.start_line}
              </Link>
            ) : null}
            {item.via !== null ? (
              <span className="min-w-0 truncate">
                {item.edge?.replaceAll("_", " ")} {names.get(item.via) ?? item.via}
              </span>
            ) : null}
          </div>
        </li>
      ))}
      {items.length > LISTED ? (
        <li className="px-2 text-xs text-fg-faint">and {items.length - LISTED} more</li>
      ) : null}
    </ol>
  );
  if (items.length === 0) return null;
  const heading = (
    <>
      {CATEGORY_LABELS[category]}{" "}
      <span className="text-fg-muted tabular-nums">({items.length})</span>
    </>
  );
  // The other code: folded, as there is often a lot of it.
  if (category === "symbols") {
    return (
      <details className="flex flex-col gap-1" data-testid={id}>
        <summary id={id} className="cursor-pointer text-sm font-medium">
          {heading}
        </summary>
        {list}
      </details>
    );
  }
  return (
    <section className="flex flex-col gap-1" data-testid={id}>
      <h2 id={id} className="text-sm font-medium">
        {heading}
      </h2>
      {list}
    </section>
  );
}
