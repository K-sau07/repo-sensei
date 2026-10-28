"use client";

/**
 * Where a symbol is used (`g r`), docked under the code: grouped by layer (UI, API, Service, DB,
 * then the rest), each with how it uses it, where, how that's known (precise, heuristic or
 * inferred) and how sure. A row opens the place. It stays open as files change, so the list can
 * be walked.
 */
import { cn } from "cn";
import { ListTree, X } from "lucide-react";
import { useEffect, useRef } from "react";

import { ProvenanceTag } from "@/components/primitives/provenance-tag";
import { LANES } from "@/components/search/meta";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/errors";
import {
  byLane,
  countLabel,
  EDGE_VERBS,
  hopLocation,
  provenanceTag,
  REFERENCE_TYPES,
  type GraphHop,
} from "@/lib/api/graph";
import type { Layer } from "@/lib/api/search";

import { useCodeActions } from "./code-intel";
import { useIntelStore, useNeighbors } from "./use-intel";

const LANE_LABELS: Record<Layer, string> = {
  ui: "UI",
  api: "API",
  svc: "Service",
  db: "Data",
  other: "Other: tests, config, libraries",
};

const percent = (confidence: number) => `${String(Math.round(confidence * 100))}%`;

function Reference({ hop, onOpen }: { hop: GraphHop; onOpen: () => void }) {
  const place = hopLocation(hop);
  return (
    <li className="flex items-center gap-2 rounded-sm px-2 py-1 hover:bg-surface-2">
      <button
        type="button"
        onClick={onOpen}
        disabled={!place}
        className="flex min-w-0 flex-1 items-baseline gap-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default"
      >
        <span className="shrink-0 font-mono text-sm text-fg">{hop.node.name}</span>
        <span className="shrink-0 text-xs text-fg-muted">{EDGE_VERBS[hop.edge.type]}</span>
        {place && (
          <span className="min-w-0 truncate font-mono text-xs text-fg-faint">
            {place.path}:{place.line}
          </span>
        )}
      </button>
      <ProvenanceTag provenance={provenanceTag(hop.edge.provenance)} />
      <span
        className="w-10 shrink-0 text-right text-xs text-fg-muted tabular-nums"
        title={`${hop.edge.provenance.replace("_", " ")}, ${percent(hop.edge.confidence)} sure`}
      >
        {percent(hop.edge.confidence)}
      </span>
    </li>
  );
}

export function ReferencesDrawer() {
  const symbol = useIntelStore((state) => state.references);
  const setReferences = useIntelStore((state) => state.setReferences);
  const actions = useCodeActions();
  const drawer = useRef<HTMLElement>(null);
  const open = symbol !== null;
  // Escape closes it, from anywhere in it.
  useEffect(() => {
    const element = drawer.current;
    if (!element) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setReferences(null);
    };
    element.addEventListener("keydown", close);
    return () => {
      element.removeEventListener("keydown", close);
    };
  }, [open, setReferences]);
  const references = useNeighbors(symbol?.symbol_key ?? null, {
    direction: "in",
    types: REFERENCE_TYPES,
  });
  if (!symbol) return null;
  const error = references.error instanceof ApiError ? references.error : null;
  const hops = references.data?.hops ?? [];
  const groups = byLane(hops);

  let body;
  if (references.isPending) {
    body = (
      <div role="status" aria-label="Loading the references" className="flex flex-col gap-2 p-3">
        <Skeleton className="h-4 w-1/2" />
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-1/3" />
      </div>
    );
  } else if (error?.code === "graph_not_ready") {
    body = <p className="p-3 text-sm text-fg-muted">{error.message}</p>;
  } else if (references.isError) {
    body = (
      <div className="flex items-center gap-2 p-3 text-sm text-fg-muted">
        Couldn&apos;t load the references.
        <Button variant="outline" size="xs" onClick={() => void references.refetch()}>
          Try again
        </Button>
      </div>
    );
  } else if (hops.length === 0) {
    body = <p className="p-3 text-sm text-fg-muted">Nothing in the graph refers to it.</p>;
  } else {
    body = (
      <div className="flex flex-col gap-3 p-2">
        {groups.map(({ lane, hops: rows }) => (
          <section key={lane} aria-label={`${LANE_LABELS[lane]} (${String(rows.length)})`}>
            <h3
              className={cn(
                "flex items-center gap-1.5 px-2 pb-1 text-xs font-medium",
                LANES[lane].text,
              )}
            >
              <span aria-hidden>{LANES[lane].glyph}</span>
              {LANE_LABELS[lane]}
              <span className="text-fg-faint tabular-nums">{rows.length}</span>
            </h3>
            <ul className="flex flex-col">
              {rows.map((hop) => (
                <Reference
                  key={`${hop.node.symbol_key} ${hop.edge.type}`}
                  hop={hop}
                  onOpen={() => {
                    const place = hopLocation(hop);
                    if (place) actions.open(place);
                  }}
                />
              ))}
            </ul>
          </section>
        ))}
      </div>
    );
  }

  return (
    <section
      aria-label={`References to ${symbol.name}`}
      data-testid="references-drawer"
      ref={drawer}
      className="flex h-72 shrink-0 flex-col border-t border-line bg-surface-1"
    >
      <header className="flex h-9 shrink-0 items-center gap-2 border-b border-line px-3 text-sm">
        <ListTree aria-hidden className="size-4 text-fg-muted" />
        <h2 className="font-medium">References</h2>
        <span className="truncate font-mono text-fg-muted">{symbol.name}</span>
        {references.data && (
          <span className="text-xs text-fg-faint tabular-nums">
            {countLabel(hops.length, references.data.truncated)}
          </span>
        )}
        <Button
          variant="ghost"
          size="icon-sm"
          className="ml-auto"
          aria-label="Close the references"
          onClick={() => {
            setReferences(null);
          }}
        >
          <X aria-hidden />
        </Button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">{body}</div>
    </section>
  );
}
