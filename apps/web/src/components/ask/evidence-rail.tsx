"use client";

/**
 * The evidence rail (4.10): what the latest answer stands on. Sources: its citations by file,
 * each range opening beside the answer. Searches: what the run searched for, in order, each one
 * to run again on the search page. Graph: the part of the graph the run walked (its neighbour
 * and trace lookups' cards).
 */
import { FileCode2 } from "lucide-react";
import Link from "next/link";

import { useRepoSlug } from "@/components/repo/use-repo";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  chipLabel,
  searchesOf,
  searchOf,
  sourcesByFile,
  type AskMessage,
  type CitationData,
} from "@/lib/api/ask";
import { cardsOf } from "@/lib/api/cards";
import { searchPath } from "@/lib/search/url-state";

import { GraphView, TraceStrip, type CodeHref } from "./tool-cards";

export function EvidenceRail({
  message,
  onOpen,
  codeHref,
}: {
  message: AskMessage | undefined;
  onOpen: (citation: CitationData) => void;
  codeHref?: CodeHref | undefined;
}) {
  const { org, repo } = useRepoSlug();
  const files = message ? sourcesByFile(message) : [];
  const searches = message ? searchesOf(message) : [];
  const walked = (message ? cardsOf(message) : []).flatMap((slot) =>
    slot.card && slot.card.kind !== "symbols" ? [{ id: slot.id, card: slot.card }] : [],
  );
  const links = codeHref ? { codeHref } : {};
  return (
    <aside
      aria-label="Evidence"
      className="flex h-full flex-col border-l border-border"
      data-testid="evidence-rail"
    >
      <Tabs defaultValue="sources" className="flex h-full flex-col">
        <TabsList className="mx-3 mt-3">
          <TabsTrigger value="sources">Sources ({files.length})</TabsTrigger>
          <TabsTrigger value="searches">Searches ({searches.length})</TabsTrigger>
          <TabsTrigger value="graph">Graph ({walked.length})</TabsTrigger>
        </TabsList>
        <TabsContent value="sources" className="flex-1 overflow-auto px-3 py-2">
          {files.length ? (
            <ul className="flex flex-col gap-3">
              {files.map(({ path, citations }) => (
                <li key={path}>
                  <p className="flex items-center gap-1.5 truncate font-mono text-xs">
                    <FileCode2 aria-hidden className="size-3.5 shrink-0 text-fg-muted" />
                    {path}
                  </p>
                  <div className="mt-1 flex flex-wrap gap-1 pl-5">
                    {citations.map((citation) => (
                      <button
                        key={citation.n}
                        type="button"
                        onClick={() => {
                          onOpen(citation);
                        }}
                        className="rounded-sm border border-border px-1.5 font-mono text-[11px] text-fg-muted hover:text-fg"
                      >
                        {citation.n} · {chipLabel(path, citation.lines)}
                      </button>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-fg-faint">The answer&apos;s sources show here.</p>
          )}
        </TabsContent>
        <TabsContent value="searches" className="flex-1 overflow-auto px-3 py-2">
          {searches.length ? (
            <ol className="list-decimal pl-5 font-mono text-xs">
              {searches.map((logged, i) => {
                const search = searchOf(logged);
                return (
                  <li key={`${String(i)}-${logged}`}>
                    {search ? (
                      <Link
                        href={searchPath(org, repo, {
                          q: search.query,
                          mode: search.tool === "grep" ? "regex" : null,
                        })}
                        className="underline-offset-2 hover:underline"
                        title="Run it again on the search page"
                      >
                        {logged}
                      </Link>
                    ) : (
                      logged
                    )}
                  </li>
                );
              })}
            </ol>
          ) : (
            <p className="text-xs text-fg-faint">What the answer searched shows here.</p>
          )}
        </TabsContent>
        <TabsContent value="graph" className="flex-1 overflow-auto px-3 py-2">
          {walked.length ? (
            <div className="flex flex-col gap-4">
              {walked.map(({ id, card }) =>
                card.kind === "graph" ? (
                  <GraphView key={id} card={card} {...links} />
                ) : (
                  <TraceStrip key={id} card={card} {...links} />
                ),
              )}
            </div>
          ) : (
            <p className="text-xs text-fg-faint">
              The graph the answer walked shows here: it followed no edges this time.
            </p>
          )}
        </TabsContent>
      </Tabs>
    </aside>
  );
}
