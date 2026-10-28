"use client";

/**
 * The code page's right rail, "Symbol": the open file's outline (the definition the cursor is in
 * marked), and the callers and callees of that definition (or of one picked in the graph, which
 * re-centres on it), with "Trace this".
 */
import { cn } from "cn";
import { ExternalLink, Route as RouteIcon } from "lucide-react";
import dynamic from "next/dynamic";

import { KeyHint } from "@/components/primitives/key-hint";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/errors";
import {
  countLabel,
  hopLocation,
  innermostAt,
  outlineRows,
  type GraphHop,
  type GraphNeighbors,
} from "@/lib/api/graph";
import { shortcut } from "@/lib/shortcut-registry";

import { definitionSymbol, useCodeActions } from "./code-intel";
import { useCodeLocation } from "./use-code";
import {
  CALLEES,
  CALLERS,
  symbolOf,
  useIntelStore,
  useNeighbors,
  useOutline,
  type CodeSymbol,
} from "./use-intel";

const MiniGraph = dynamic(() => import("./mini-graph").then((module) => module.MiniGraph), {
  ssr: false,
  loading: () => <Skeleton className="h-64" />,
});

// The rail shows the nearest; the references drawer has them all.
const RAIL_LIMIT = 50;

function Outline({
  path,
  current,
  onPick,
}: {
  path: string;
  current: string | null;
  onPick: (symbol: CodeSymbol) => void;
}) {
  const outline = useOutline(path, path !== "");
  const error = outline.error instanceof ApiError ? outline.error : null;
  if (path === "" || error?.code === "path_not_found") {
    return <p className="text-fg-muted">Open a file to see its symbols.</p>;
  }
  if (error) return <p className="text-fg-muted">{error.message}</p>;
  if (!outline.data) {
    return (
      <div role="status" aria-label="Loading the outline" className="flex flex-col gap-1.5">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </div>
    );
  }
  if (outline.data.symbols.length === 0) {
    return <p className="text-fg-muted">No definitions in this file.</p>;
  }
  return (
    <ul className="flex flex-col">
      {outlineRows(outline.data.symbols).map(({ symbol, depth }) => (
        <li key={symbol.symbol_key}>
          <button
            type="button"
            aria-current={symbol.symbol_key === current ? "location" : undefined}
            onClick={() => {
              onPick(definitionSymbol(symbol, path));
            }}
            className={cn(
              "flex w-full items-baseline gap-2 rounded-sm py-0.5 pr-1 text-left outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-accent",
              symbol.symbol_key === current && "bg-surface-2 text-fg",
            )}
            style={{ paddingLeft: `${String(4 + depth * 12)}px` }}
          >
            <span className="min-w-0 flex-1 truncate font-mono text-xs">{symbol.name}</span>
            <span className="shrink-0 text-[11px] text-fg-faint">{symbol.kind}</span>
          </button>
        </li>
      ))}
      {outline.data.truncated && (
        <li className="px-1 pt-1 text-xs text-fg-faint">The first 2,000 are listed.</li>
      )}
    </ul>
  );
}

function Neighbours({
  title,
  result,
  onCentre,
  onOpen,
}: {
  title: string;
  result: { data: GraphNeighbors | undefined; isPending: boolean; isError: boolean };
  onCentre: (hop: GraphHop) => void;
  onOpen: (hop: GraphHop) => void;
}) {
  const hops = result.data?.hops ?? [];
  return (
    <section aria-label={title} className="flex flex-col gap-1">
      <h4 className="text-xs font-medium text-fg-muted">
        {title}{" "}
        {result.data && (
          <span className="text-fg-faint tabular-nums">
            {countLabel(hops.length, result.data.truncated)}
          </span>
        )}
      </h4>
      {result.isPending ? (
        <Skeleton className="h-4 w-1/2" />
      ) : result.isError ? (
        <p className="text-xs text-fg-faint">Not available for this commit yet.</p>
      ) : hops.length === 0 ? (
        <p className="text-xs text-fg-faint">None in the graph.</p>
      ) : (
        <ul className="flex flex-col">
          {hops.map((hop) => (
            <li key={hop.node.symbol_key} className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => {
                  onCentre(hop);
                }}
                title={`Centre on ${hop.node.qualified_name}`}
                className="min-w-0 flex-1 truncate rounded-sm px-1 py-0.5 text-left font-mono text-xs outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-accent"
              >
                {hop.node.name}
              </button>
              {hopLocation(hop) && (
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Open ${hop.node.name}`}
                  onClick={() => {
                    onOpen(hop);
                  }}
                >
                  <ExternalLink aria-hidden />
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function SymbolRail() {
  const { path } = useCodeLocation();
  const actions = useCodeActions();
  const cursor = useIntelStore((state) => state.cursor);
  const picked = useIntelStore((state) => state.centre);
  const setCentre = useIntelStore((state) => state.setCentre);
  const outline = useOutline(path, path !== "");
  const symbols = outline.data?.symbols ?? [];
  const around = cursor?.path === path ? innermostAt(symbols, cursor.line) : null;
  const at = around?.symbol_key ?? null;
  const centre: CodeSymbol | null =
    picked?.at === at ? picked.symbol : around ? definitionSymbol(around, path) : null;
  const key = centre?.symbol_key ?? null;
  const callers = useNeighbors(key, { ...CALLERS, limit: RAIL_LIMIT });
  const callees = useNeighbors(key, { ...CALLEES, limit: RAIL_LIMIT });
  const root = callers.data?.root ?? callees.data?.root ?? null;
  const centreOn = (symbol: CodeSymbol) => {
    setCentre({ symbol, at });
  };
  const open = (hop: GraphHop) => {
    const place = hopLocation({ ...hop, edge: { ...hop.edge, evidence: null } });
    if (place) actions.open(place);
  };

  return (
    <div className="flex flex-col gap-5">
      <section aria-label="Outline" className="flex flex-col gap-1.5">
        <h3 className="text-xs font-medium tracking-wide text-fg-muted uppercase">Outline</h3>
        <Outline
          path={path}
          current={at}
          onPick={(symbol) => {
            centreOn(symbol);
            actions.open({ path, line: symbol.start_line });
          }}
        />
      </section>
      {centre && (
        <section aria-label="Callers and callees" className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <h3 className="min-w-0 flex-1 truncate text-xs font-medium tracking-wide text-fg-muted uppercase">
              Calls around <span className="font-mono normal-case">{centre.name}</span>
            </h3>
            <Button
              variant="outline"
              size="xs"
              onClick={() => {
                actions.trace(centre);
              }}
            >
              <RouteIcon aria-hidden />
              Trace this
              <KeyHint shortcut={shortcut("trace").keys} />
            </Button>
          </div>
          {root && (
            <MiniGraph
              centre={root}
              callers={callers.data?.hops ?? []}
              callees={callees.data?.hops ?? []}
              onSelect={(node) => {
                centreOn(symbolOf(node));
              }}
            />
          )}
          <Neighbours
            title="Callers"
            result={callers}
            onCentre={(hop) => {
              centreOn(symbolOf(hop.node));
            }}
            onOpen={open}
          />
          <Neighbours
            title="Callees"
            result={callees}
            onCentre={(hop) => {
              centreOn(symbolOf(hop.node));
            }}
            onOpen={open}
          />
        </section>
      )}
    </div>
  );
}
