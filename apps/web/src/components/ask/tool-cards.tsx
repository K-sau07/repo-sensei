"use client";

/**
 * What the run's tools found, drawn beside the answer (4.11.3): the definitions it looked up,
 * a symbol's neighbours on a mini graph, a traced path as a strip through the layers, and the
 * endpoints those pass through, as a table. A tool still running is a card loading; one that
 * failed says so. Each card can be promoted to the artifact panel, to keep it open beside the
 * thread. Every name and line is the index's, read by the tool: nothing the model wrote.
 */
import { cn } from "cn";
import { ArrowRight, Pin } from "lucide-react";
import type { Route } from "next";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";

import { LAYERS } from "@/components/primitives/layer-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { VERBS } from "@/lib/api/ask";
import {
  cardTitle,
  endpointsOf,
  type CardSlot,
  type EndpointRow,
  type GraphCard,
  type SymbolsCard,
  type ToolCard,
  type TraceCard,
} from "@/lib/api/cards";
import { badgeLayer, EDGE_VERBS, type GraphNode } from "@/lib/api/graph";

const MiniGraph = dynamic(
  () => import("@/components/code/mini-graph").then((module) => module.MiniGraph),
  { ssr: false, loading: () => <Skeleton className="h-64" /> },
);

/** Where a file's lines open (null: nowhere, as on a shared page). */
export type CodeHref = (path: string, lines: readonly [number, number]) => Route | null;

/** A card, or the endpoints table, promoted to the artifact panel. */
export type Artifact =
  { kind: "card"; card: ToolCard } | { kind: "endpoints"; rows: EndpointRow[] };

export function artifactTitle(artifact: Artifact): string {
  return artifact.kind === "card" ? cardTitle(artifact.card) : "Endpoints";
}

function Place({
  path,
  lines,
  codeHref,
  children,
}: {
  path: string | null;
  lines: readonly [number, number];
  codeHref: CodeHref | undefined;
  children: ReactNode;
}) {
  const href = path !== null && lines[0] > 0 ? codeHref?.(path, lines) : null;
  if (!href) return <span className="font-mono">{children}</span>;
  return (
    <Link href={href} className="font-mono underline-offset-2 hover:underline">
      {children}
    </Link>
  );
}

function Lane({ node }: { node: GraphNode }) {
  const layer = badgeLayer(node.lane);
  if (!layer) return null;
  const { glyph, label } = LAYERS[layer];
  return (
    <span aria-label={`${label} layer`} role="img" className={cn("shrink-0", TEXT[layer])}>
      {glyph}
    </span>
  );
}

const TEXT = {
  ui: "text-layer-ui",
  api: "text-layer-api",
  svc: "text-layer-svc",
  db: "text-layer-db",
} as const;

/** An edge's verb ("calls"); a type the page doesn't know, as it is. */
function verb(type: string): string {
  return (EDGE_VERBS as Record<string, string | undefined>)[type] ?? type;
}

function lines(node: GraphNode): [number, number] {
  return [node.start_line, node.end_line];
}

export function SymbolsView({ card, codeHref }: { card: SymbolsCard; codeHref?: CodeHref }) {
  return (
    <ul className="flex flex-col gap-1.5" aria-label="Definitions">
      {card.symbols.map((s) => (
        <li key={s.symbolKey} className="flex min-w-0 flex-col gap-0.5 text-xs">
          <span className="flex items-baseline gap-2">
            <span className="font-mono font-medium text-fg">{s.name}</span>
            <span className="text-fg-muted">{s.kind}</span>
            <span className="ml-auto truncate text-fg-muted">
              <Place path={s.path} lines={s.lines} codeHref={codeHref}>
                {s.path}:{s.lines[0]}–{s.lines[1]}
              </Place>
            </span>
          </span>
          {s.signature ? (
            <code className="truncate text-[11px] text-fg-muted" title={s.signature}>
              {s.signature}
            </code>
          ) : null}
        </li>
      ))}
      {card.more > 0 ? <li className="text-xs text-fg-muted">and {card.more} more</li> : null}
    </ul>
  );
}

export function GraphView({ card, codeHref }: { card: GraphCard; codeHref?: CodeHref }) {
  const router = useRouter();
  const callers = card.hops.filter((h) => h.edge.direction === "in");
  const callees = card.hops.filter((h) => h.edge.direction === "out");
  const list = (title: string, hops: typeof callers) =>
    hops.length ? (
      <div className="flex flex-col gap-1">
        <p className="text-[11px] font-medium text-fg-muted uppercase" aria-hidden>
          {title}
        </p>
        <ul className="flex flex-col gap-0.5 text-xs" aria-label={title}>
          {hops.map((h) => (
            <li key={`${h.via}-${h.node.symbol_key}`} className="flex items-center gap-1.5">
              <Lane node={h.node} />
              <Place path={h.node.path} lines={lines(h.node)} codeHref={codeHref}>
                {h.node.name}
              </Place>
              <span className="text-fg-muted">{EDGE_VERBS[h.edge.type]}</span>
            </li>
          ))}
        </ul>
      </div>
    ) : null;
  return (
    <div className="flex flex-col gap-2">
      <MiniGraph
        centre={card.root}
        callers={callers}
        callees={callees}
        onSelect={(node) => {
          const href = node.path ? codeHref?.(node.path, lines(node)) : null;
          if (href) router.push(href);
        }}
      />
      <div className="grid gap-3 sm:grid-cols-2">
        {list("Reached from", callers)}
        {list("Reaches", callees)}
      </div>
      {card.more > 0 ? <p className="text-xs text-fg-muted">and {card.more} more</p> : null}
    </div>
  );
}

export function TraceStrip({
  card,
  codeHref,
  traceHref,
}: {
  card: TraceCard;
  codeHref?: CodeHref;
  traceHref?: Route | null;
}) {
  const best = card.paths[0];
  if (!best) return null;
  const breakAfter = new Map(best.breaks.map((b) => [b.afterHop, b]));
  return (
    <div className="flex flex-col gap-2">
      <ol className="flex flex-wrap items-center gap-1 text-xs" aria-label="Traced path">
        {best.hops.map((hop, i) => {
          const gap = breakAfter.get(i);
          return (
            <li key={`${String(i)}-${hop.node.symbol_key}`} className="flex items-center gap-1">
              {i > 0 && hop.edge ? (
                <span
                  className="flex items-center text-fg-faint"
                  title={`${verb(hop.edge.type)} (${hop.edge.provenance})`}
                >
                  <ArrowRight aria-hidden className="size-3" />
                  <span className="sr-only">{verb(hop.edge.type)}</span>
                </span>
              ) : null}
              <span className="flex items-center gap-1 rounded-sm border border-line px-1.5 py-0.5">
                <Lane node={hop.node} />
                <Place path={hop.node.path} lines={lines(hop.node)} codeHref={codeHref}>
                  {hop.node.name}
                </Place>
              </span>
              {gap ? (
                <span
                  className="rounded-sm border border-dashed border-warn px-1.5 py-0.5 text-warn"
                  title={gap.reason}
                >
                  stops: {gap.reason}
                </span>
              ) : null}
            </li>
          );
        })}
        {best.cut > 0 ? <li className="text-fg-muted">and {best.cut} more hops</li> : null}
      </ol>
      <p className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
        <span>
          {Math.round(best.confidence * 100)}% sure
          {best.reachesGoal ? ", reaches the data" : ""}
        </span>
        {card.paths.length + card.more > 1 ? (
          <span>· {card.paths.length + card.more} paths, the surest shown</span>
        ) : null}
        {traceHref ? (
          <Link href={traceHref} className="underline underline-offset-2 hover:text-fg">
            Open in Trace
          </Link>
        ) : null}
      </p>
    </div>
  );
}

export function EndpointTable({ rows, codeHref }: { rows: EndpointRow[]; codeHref?: CodeHref }) {
  return (
    <table className="w-full text-left text-xs" aria-label="Endpoints">
      <thead className="text-fg-muted">
        <tr>
          <th className="py-1 pr-2 font-medium">Method</th>
          <th className="py-1 pr-2 font-medium">Path</th>
          <th className="py-1 font-medium">Served by</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.node.symbol_key} className="border-t border-line">
            <td className="py-1 pr-2 font-mono font-medium">{row.method}</td>
            <td className="py-1 pr-2 font-mono">{row.path}</td>
            <td className="py-1">
              {row.handler ? (
                <Place path={row.handler.path} lines={lines(row.handler)} codeHref={codeHref}>
                  {row.handler.name}
                </Place>
              ) : (
                <span className="text-fg-muted">–</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function CardBody({
  artifact,
  codeHref,
  traceHref,
}: {
  artifact: Artifact;
  codeHref?: CodeHref | undefined;
  traceHref?: ((entry: string) => Route | null) | undefined;
}) {
  const links = codeHref ? { codeHref } : {};
  if (artifact.kind === "endpoints") return <EndpointTable rows={artifact.rows} {...links} />;
  const { card } = artifact;
  switch (card.kind) {
    case "symbols":
      return <SymbolsView card={card} {...links} />;
    case "graph":
      return <GraphView card={card} {...links} />;
    case "trace":
      return <TraceStrip card={card} {...links} traceHref={traceHref?.(card.entry) ?? null} />;
  }
}

function Frame({
  title,
  onPromote,
  children,
}: {
  title: string;
  onPromote?: (() => void) | undefined;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={title}
      className="flex flex-col gap-2 rounded-md border border-line p-3"
      data-testid="tool-card"
    >
      <header className="flex items-center gap-2">
        <p className="min-w-0 flex-1 truncate text-xs font-medium" aria-hidden>
          {title}
        </p>
        {onPromote ? (
          <Button
            variant="ghost"
            size="sm"
            className="h-6 px-1.5 text-xs"
            aria-label={`Promote ${title} to the side panel`}
            onClick={onPromote}
          >
            <Pin aria-hidden data-icon="inline-start" />
            Promote
          </Button>
        ) : null}
      </header>
      {children}
    </section>
  );
}

/** The message's cards, in the order its tools ran, and the endpoints they pass through. */
export function ToolCards({
  slots,
  codeHref,
  traceHref,
  onPromote,
}: {
  slots: readonly CardSlot[];
  codeHref?: CodeHref | undefined;
  traceHref?: ((entry: string) => Route | null) | undefined;
  onPromote?: ((artifact: Artifact) => void) | undefined;
}) {
  if (slots.length === 0) return null;
  const endpoints = endpointsOf(slots);
  const promote = (artifact: Artifact) =>
    onPromote
      ? () => {
          onPromote(artifact);
        }
      : undefined;
  return (
    <div className="flex flex-col gap-2" data-testid="tool-cards">
      {slots.map((slot) => {
        if (slot.state === "loading") {
          return (
            <div
              key={slot.id}
              role="status"
              aria-label={`${VERBS[slot.tool] ?? slot.tool}…`}
              className="flex flex-col gap-2 rounded-md border border-line p-3"
            >
              <span className="text-xs text-fg-muted">{VERBS[slot.tool] ?? slot.tool}…</span>
              <Skeleton className="h-8" />
            </div>
          );
        }
        if (slot.state === "error" || slot.card === null) {
          return (
            <p key={slot.id} className="rounded-md border border-line p-3 text-xs text-fg-muted">
              {VERBS[slot.tool] ?? slot.tool} found nothing to show
              {slot.summary ? `: ${slot.summary}` : "."}
            </p>
          );
        }
        const artifact: Artifact = { kind: "card", card: slot.card };
        return (
          <Frame key={slot.id} title={cardTitle(slot.card)} onPromote={promote(artifact)}>
            <CardBody artifact={artifact} codeHref={codeHref} traceHref={traceHref} />
          </Frame>
        );
      })}
      {endpoints.length > 0 ? (
        <Frame title="Endpoints" onPromote={promote({ kind: "endpoints", rows: endpoints })}>
          <EndpointTable rows={endpoints} {...(codeHref ? { codeHref } : {})} />
        </Frame>
      ) : null}
    </div>
  );
}
