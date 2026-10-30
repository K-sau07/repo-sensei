"use client";

/**
 * The evidence rail (4.10): what the latest answer stands on. Sources: its citations by file,
 * each range opening beside the answer. Searches: what the run searched for, in order.
 */
import { FileCode2 } from "lucide-react";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  chipLabel,
  searchesOf,
  sourcesByFile,
  type AskMessage,
  type CitationData,
} from "@/lib/api/ask";

export function EvidenceRail({
  message,
  onOpen,
}: {
  message: AskMessage | undefined;
  onOpen: (citation: CitationData) => void;
}) {
  const files = message ? sourcesByFile(message) : [];
  const searches = message ? searchesOf(message) : [];
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
              {searches.map((query, i) => (
                <li key={`${i}-${query}`}>{query}</li>
              ))}
            </ol>
          ) : (
            <p className="text-xs text-fg-faint">What the answer searched shows here.</p>
          )}
        </TabsContent>
      </Tabs>
    </aside>
  );
}
