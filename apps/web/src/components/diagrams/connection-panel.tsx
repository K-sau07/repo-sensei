"use client";

/**
 * Beside an architecture (6.6): how it works, and how its parts talk.
 * - **How it works**: the walkthrough, a step per line in the order a request goes; a step
 *   picked lights up its line on the canvas.
 * - **A line**: which part talks to which, what it does, how many of the code's edges it stands
 *   for, and some of them (caller → callee), each opening its code at the diagram's commit; an
 *   inferred line says why it is drawn.
 * - **Talks to / used by**: a part's lines, out and in, each one opening as a line.
 */
import { ArrowRight, ShieldCheck, X } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";

import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import type { SpecEdge, SpecNode, SpecStep } from "@/lib/api/diagrams";
import { repoPath } from "@/lib/routes";

/** A line's index in the diagram's edges, by its ends (-1: none). */
export function lineOf(edges: readonly SpecEdge[], from: string, to: string): number {
  return edges.findIndex((edge) => edge.from === from && edge.to === to);
}

function nameOf(nodes: readonly SpecNode[], id: string): string {
  return nodes.find((node) => node.id === id)?.label ?? id;
}

export function HowItWorks({
  steps,
  nodes,
  edges,
  picked,
  onPick,
}: {
  steps: readonly SpecStep[];
  nodes: readonly SpecNode[];
  edges: readonly SpecEdge[];
  picked: number | null;
  onPick: (index: number | null) => void;
}) {
  return (
    <section
      aria-labelledby="how-it-works"
      className="flex h-full min-h-0 flex-col border-l border-line"
      data-testid="how-it-works"
    >
      <h2
        id="how-it-works"
        className="border-b border-line px-3 py-2 text-xs font-medium text-fg-muted"
      >
        How it works (pick a step, a box or a line)
      </h2>
      <ol className="flex min-h-0 flex-1 flex-col gap-1 overflow-auto p-2">
        {steps.map((step, i) => {
          const line = lineOf(edges, step.from, step.to);
          const on = line >= 0 && line === picked;
          return (
            <li key={`${step.from}>${step.to}:${String(i)}`}>
              <button
                type="button"
                aria-pressed={on}
                className={`flex w-full flex-col gap-1 rounded-md px-2 py-1.5 text-left text-xs hover:bg-surface-2 ${on ? "bg-surface-2 ring-1 ring-line-strong" : ""}`}
                onClick={() => {
                  onPick(on ? null : line);
                }}
              >
                <span className="flex items-center gap-1.5 font-medium text-fg">
                  <span className="flex size-4 shrink-0 items-center justify-center rounded-full bg-surface-3 text-[10px] tabular-nums">
                    {i + 1}
                  </span>
                  {nameOf(nodes, step.from)}
                  <ArrowRight aria-label="to" className="size-3 text-fg-faint" />
                  {nameOf(nodes, step.to)}
                </span>
                <span className="text-fg-muted">
                  <Said text={step.text} />
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export function ConnectionPanel({
  edge,
  nodes,
  sha,
  onClose,
}: {
  edge: SpecEdge;
  nodes: readonly SpecNode[];
  sha: string;
  onClose: () => void;
}) {
  const { org, repo } = useRepoSlug();
  const samples = edge.samples ?? [];
  const from = nameOf(nodes, edge.from);
  const to = nameOf(nodes, edge.to);
  return (
    <aside
      aria-label={`${from} to ${to}`}
      className="flex h-full min-h-0 flex-col border-l border-line"
      data-testid="connection-panel"
    >
      <header className="flex items-start gap-2 border-b border-line px-3 py-2">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="text-[10px] tracking-wide text-fg-faint uppercase">Connection</span>
          <h2 className="flex flex-wrap items-center gap-1 text-sm font-semibold">
            {from}
            <ArrowRight aria-label="to" className="size-3.5 text-fg-faint" />
            {to}
          </h2>
          {edge.label ? <span className="text-xs text-fg-muted">{edge.label}</span> : null}
        </div>
        <Button size="icon-sm" variant="ghost" aria-label="Close" onClick={onClose}>
          <X aria-hidden />
        </Button>
      </header>
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto p-3 text-xs">
        {edge.verified ? (
          <p className="flex items-center gap-1.5 text-ok">
            <ShieldCheck aria-hidden className="size-3.5" />
            {edge.count
              ? `Read from the code: ${String(edge.count)} ${edge.count === 1 ? "edge" : "edges"} (calls, imports, reads).`
              : "Read from the code."}
          </p>
        ) : (
          <p className="text-warn">
            Inferred: {edge.inferred?.reason ?? "the code doesn't show it"}.
          </p>
        )}
        {samples.length > 0 ? (
          <section aria-labelledby="behind-it" className="flex flex-col gap-1.5">
            <h3 id="behind-it" className="font-medium text-fg-muted">
              The code behind it
            </h3>
            <ul className="flex flex-col gap-1.5">
              {samples.map((sample) => (
                <li
                  key={`${sample.from}>${sample.to}`}
                  className="flex flex-col gap-0.5 rounded-md border border-line px-2 py-1.5"
                >
                  <span className="flex flex-wrap items-center gap-1 font-mono text-[11px] text-fg">
                    {sample.from}
                    <ArrowRight aria-label="calls" className="size-3 text-fg-faint" />
                    {sample.to}
                  </span>
                  {sample.path ? (
                    <Link
                      href={
                        `${repoPath(org, repo, `code/${sample.path}`)}?ref=${sha}${sample.line ? `&L=${String(sample.line)}` : ""}` as Route
                      }
                      className="w-fit font-mono text-[10px] text-fg-muted hover:underline"
                    >
                      {sample.path}
                      {sample.line ? `:${String(sample.line)}` : ""}
                    </Link>
                  ) : null}
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </div>
    </aside>
  );
}

/** A part's lines: what it talks to, and what talks to it, each opening as a line. */
export function TalksTo({
  node,
  nodes,
  edges,
  onPick,
}: {
  node: SpecNode;
  nodes: readonly SpecNode[];
  edges: readonly SpecEdge[];
  onPick: (index: number) => void;
}) {
  const out = edges.flatMap((e, i) => (e.from === node.id ? [{ e, i, other: e.to }] : []));
  const into = edges.flatMap((e, i) => (e.to === node.id ? [{ e, i, other: e.from }] : []));
  const list = (title: string, items: typeof out, arrow: string) =>
    items.length > 0 ? (
      <section className="flex flex-col gap-1">
        <h3 className="text-xs font-medium text-fg-muted">{title}</h3>
        <ul className="flex flex-col">
          {items.map(({ e, i, other }) => (
            <li key={i}>
              <button
                type="button"
                className="flex w-full items-baseline gap-2 rounded-sm px-1 py-0.5 text-left text-xs hover:bg-surface-2"
                onClick={() => {
                  onPick(i);
                }}
              >
                <span aria-hidden className="text-fg-faint">
                  {arrow}
                </span>
                <span className="font-medium text-fg">{nameOf(nodes, other)}</span>
                <span className="min-w-0 flex-1 truncate text-fg-muted">
                  {e.label ?? e.inferred?.reason ?? ""}
                </span>
                {e.count ? <span className="text-fg-faint tabular-nums">{e.count}</span> : null}
              </button>
            </li>
          ))}
        </ul>
      </section>
    ) : null;
  return (
    <div className="flex flex-col gap-3" data-testid="talks-to">
      {list("Talks to", out, "→")}
      {list("Used by", into, "←")}
    </div>
  );
}

/** A step's words, its `code` spans set as code. */
function Said({ text }: { text: string }) {
  return (
    <>
      {text.split(/(`[^`]+`)/).map((part, i) =>
        part.startsWith("`") && part.endsWith("`") && part.length > 2 ? (
          <code key={i} className="rounded-sm bg-surface-3 px-1 font-mono text-[11px] text-fg">
            {part.slice(1, -1)}
          </code>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}
