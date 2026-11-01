"use client";

/**
 * The graph explorer's side panels (6.7). A node's: what it is, what depends on it and what it
 * depends on (each edge opening its evidence), its coupling, and what to do from it (open it
 * into the level below, a path from or to it, its code, a trace). An edge's: what it counts and
 * the lines of code behind it, each opening the code there.
 */
import { ArrowRight, FolderOpen, Radar, Route as RouteIcon, X } from "lucide-react";
import Link from "next/link";

import { useRepoSlug } from "@/components/repo/use-repo";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  LEVEL_LABELS,
  finer,
  typesLabel,
  type DepEdge,
  type DepLevel,
  type Dsm,
  type ShownNode,
} from "@/lib/api/deps";
import { impactHref } from "@/lib/api/impact";
import { codePath } from "@/lib/routes";
import { traceHref } from "@/lib/trace/url-state";

import type { SelectedEdge } from "./graph-explorer";
import { useEdgeEvidence } from "./use-deps";

const LISTED = 8;

/** Where a node's code is, if it has a place: a file, a folder, a symbol's file. */
function codeOf(node: ShownNode): string | null {
  if (node.id.startsWith("~")) return null;
  if (node.level === "symbol") {
    const path = /^\S+ (.+?\.[A-Za-z0-9]+)\//.exec(node.id)?.[1];
    return path ?? null;
  }
  return node.id === "." ? "" : node.id;
}

function EdgeRow({
  edge,
  other,
  dir,
  onSelect,
}: {
  edge: DepEdge;
  other: string;
  dir: "in" | "out";
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        className="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left text-xs hover:bg-surface-2"
      >
        {dir === "in" ? <span className="min-w-0 flex-1 truncate">{other}</span> : null}
        <ArrowRight aria-hidden className="size-3 shrink-0 text-fg-faint" />
        {dir === "out" ? <span className="min-w-0 flex-1 truncate">{other}</span> : null}
        <span className="text-fg-muted tabular-nums">{edge.weight}</span>
      </button>
    </li>
  );
}

export function NodePanel({
  id,
  nodes,
  edges,
  dsm,
  canOpen,
  opened,
  onOpen,
  onSelectEdge,
  onPath,
}: {
  id: string;
  nodes: readonly ShownNode[];
  edges: readonly DepEdge[];
  dsm: Dsm | null;
  canOpen: boolean;
  opened: boolean;
  onOpen: (id: string) => void;
  onSelectEdge: (source: string, target: string) => void;
  onPath: (end: "from" | "to", id: string) => void;
}) {
  const { org, repo } = useRepoSlug();
  const node = nodes.find((n) => n.id === id);
  if (!node) return null;
  const labels = new Map(nodes.map((n) => [n.id, n.label]));
  const into = edges.filter((e) => e.target === id).sort((a, b) => b.weight - a.weight);
  const out = edges.filter((e) => e.source === id).sort((a, b) => b.weight - a.weight);
  const coupling = dsm?.order.find((n) => n.id === id);
  const code = codeOf(node);
  const below = finer(node.level);
  return (
    <section
      aria-label={`About ${node.label}`}
      data-testid="graph-node-panel"
      className="flex flex-col gap-4 p-4"
    >
      <header className="flex flex-col gap-1">
        <h2 className="text-sm font-medium break-all">{node.label}</h2>
        <p className="text-xs break-all text-fg-muted">
          {LEVEL_LABELS[node.level].replace(/s$/, "")}
          {node.id !== node.label ? ` · ${node.id}` : ""}
        </p>
        <p className="text-xs text-fg-muted">
          {node.size} {node.size === 1 ? "definition" : "definitions"} · {node.layer}
        </p>
      </header>
      {coupling ? (
        <dl className="grid grid-cols-3 gap-2 text-xs" aria-label="Coupling">
          <div>
            <dt className="text-fg-faint">Depended on by</dt>
            <dd className="tabular-nums">{coupling.afferent}</dd>
          </div>
          <div>
            <dt className="text-fg-faint">Depends on</dt>
            <dd className="tabular-nums">{coupling.efferent}</dd>
          </div>
          <div>
            <dt className="text-fg-faint">Instability</dt>
            <dd className="tabular-nums">
              {coupling.instability === null ? "–" : coupling.instability.toFixed(2)}
            </dd>
          </div>
        </dl>
      ) : null}
      <div className="flex flex-wrap gap-1.5">
        {canOpen && below ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              onOpen(id);
            }}
          >
            <FolderOpen aria-hidden />
            {opened ? "Close" : `Open into ${LEVEL_LABELS[below].toLowerCase()}`}
          </Button>
        ) : null}
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            onPath("from", id);
          }}
        >
          Path from here
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            onPath("to", id);
          }}
        >
          Path to here
        </Button>
        {code !== null ? (
          <Link
            href={codePath(org, repo, code)}
            className={buttonVariants({ size: "sm", variant: "outline" })}
          >
            Open the code
          </Link>
        ) : null}
        {node.level === "symbol" ? (
          <Link
            href={traceHref(org, repo, { from: node.id })}
            className={buttonVariants({ size: "sm", variant: "outline" })}
          >
            <RouteIcon aria-hidden />
            Trace from here
          </Link>
        ) : null}
        {node.level === "symbol" ? (
          <Link
            href={impactHref(org, repo, { symbol: node.id })}
            className={buttonVariants({ size: "sm", variant: "outline" })}
          >
            <Radar aria-hidden />
            Impact
          </Link>
        ) : null}
      </div>
      <EdgeList
        title="Depended on by"
        edges={into}
        empty="Nothing here depends on it."
        render={(edge) => (
          <EdgeRow
            key={edge.source}
            edge={edge}
            other={labels.get(edge.source) ?? edge.source}
            dir="in"
            onSelect={() => {
              onSelectEdge(edge.source, edge.target);
            }}
          />
        )}
      />
      <EdgeList
        title="Depends on"
        edges={out}
        empty="It depends on nothing else here."
        render={(edge) => (
          <EdgeRow
            key={edge.target}
            edge={edge}
            other={labels.get(edge.target) ?? edge.target}
            dir="out"
            onSelect={() => {
              onSelectEdge(edge.source, edge.target);
            }}
          />
        )}
      />
    </section>
  );
}

function EdgeList({
  title,
  edges,
  empty,
  render,
}: {
  title: string;
  edges: readonly DepEdge[];
  empty: string;
  render: (edge: DepEdge) => React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <h3 className="text-xs font-medium text-fg-muted">
        {title} <span className="tabular-nums">({edges.length})</span>
      </h3>
      {edges.length === 0 ? (
        <p className="text-xs text-fg-faint">{empty}</p>
      ) : (
        <ul className="flex flex-col">
          {edges.slice(0, LISTED).map(render)}
          {edges.length > LISTED ? (
            <li className="px-2 text-xs text-fg-faint">and {edges.length - LISTED} more</li>
          ) : null}
        </ul>
      )}
    </div>
  );
}

export function EdgePanel({
  edge: selected,
  edges,
  levelOf,
  onClose,
}: {
  edge: SelectedEdge;
  edges: readonly DepEdge[];
  levelOf: ReadonlyMap<string, DepLevel>;
  onClose: () => void;
}) {
  const { org, repo } = useRepoSlug();
  const edge = edges.find((e) => e.source === selected.source && e.target === selected.target);
  const from = levelOf.get(selected.source);
  const to = levelOf.get(selected.target);
  // Between two nodes of one level: its evidence; across levels (an opened node's child and a
  // node not opened): the samples the edge carries.
  const level = from !== undefined && from === to ? from : null;
  const evidence = useEdgeEvidence(
    level ?? "module",
    level !== null ? selected.source : null,
    level !== null ? selected.target : null,
  );
  const lines =
    level === null
      ? (edge?.samples ?? []).map((s) => ({ path: s.path, line: s.line, type: null }))
      : (evidence.data?.edges ?? []).flatMap((e) =>
          e.path === null ? [] : [{ path: e.path, line: e.line ?? 1, type: e.type }],
        );
  return (
    <section aria-label="Edge" data-testid="graph-edge-panel" className="flex flex-col gap-3 p-4">
      <header className="flex items-start gap-2">
        <h2 className="min-w-0 flex-1 text-sm font-medium break-all">
          {selected.source} <span aria-hidden>→</span>
          <span className="sr-only">depends on</span> {selected.target}
        </h2>
        <Button size="icon" variant="ghost" aria-label="Close the edge" onClick={onClose}>
          <X aria-hidden />
        </Button>
      </header>
      {edge ? (
        <p className="text-xs text-fg-muted">
          {typesLabel(edge.types)} · {Math.round(edge.confidence * 100)}% sure
        </p>
      ) : null}
      <h3 className="text-xs font-medium text-fg-muted">
        The code behind it
        {evidence.data && level !== null ? (
          <span className="tabular-nums"> ({evidence.data.total})</span>
        ) : null}
      </h3>
      {level !== null && evidence.isPending ? (
        <Skeleton className="h-20" />
      ) : lines.length === 0 ? (
        <p className="text-xs text-fg-faint">No lines recorded for this edge.</p>
      ) : (
        <ul className="flex flex-col gap-0.5" data-testid="graph-evidence">
          {lines.map((l, i) => (
            <li key={`${l.path}:${String(l.line)}:${String(i)}`}>
              <Link
                href={codePath(org, repo, l.path, { lines: String(l.line) })}
                className="flex gap-2 rounded-sm px-2 py-1 font-mono text-xs hover:bg-surface-2"
              >
                <span className="min-w-0 flex-1 truncate">
                  {l.path}:{l.line}
                </span>
                {l.type ? (
                  <span className="text-fg-faint">{l.type.replaceAll("_", " ")}</span>
                ) : null}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
