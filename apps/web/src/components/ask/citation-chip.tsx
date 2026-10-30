"use client";

/**
 * A citation in an answer (4.10): an inline chip (`file.tsx:12–48`) that opens a peek at the
 * lines it cites (with whether the check found them backing the claim), from which the code
 * opens beside the answer. Inline-block and of a fixed height: chips never shift the text.
 */
import { cn } from "cn";
import { BadgeCheck, FileCode2 } from "lucide-react";

import { useFileAt } from "@/components/code/use-code";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { chipLabel, type CitationData } from "@/lib/api/ask";

const PEEK_LINES = 40;

export function CitationChip({
  citation,
  onOpen,
}: {
  citation: CitationData;
  onOpen?: ((citation: CitationData) => void) | undefined;
}) {
  return (
    <Popover>
      <PopoverTrigger
        data-testid="citation-chip"
        className={cn(
          "mx-0.5 inline-flex h-5 items-center gap-1 rounded-sm border border-border bg-muted px-1.5",
          "align-baseline font-mono text-[11px] leading-none text-fg-muted hover:text-fg",
          citation.verified && "border-accent/40",
        )}
      >
        <span className="text-fg-faint">{citation.n}</span>
        <span className="sr-only"> </span>
        {chipLabel(citation.path, citation.lines)}
        <span className="sr-only">
          , {citation.path}, lines {citation.lines[0]} to {citation.lines[1]}
        </span>
      </PopoverTrigger>
      <PopoverContent
        aria-label={`Lines ${citation.lines[0]} to ${citation.lines[1]} of ${citation.path}`}
        className="w-[34rem] max-w-[90vw] p-0"
        data-testid="citation-peek"
      >
        <CitationPeek citation={citation} onOpen={onOpen} />
      </PopoverContent>
    </Popover>
  );
}

function CitationPeek({
  citation,
  onOpen,
}: {
  citation: CitationData;
  onOpen?: ((citation: CitationData) => void) | undefined;
}) {
  const file = useFileAt(citation.path, citation.sha);
  const [start, end] = citation.lines;
  const content = file.data && "content" in file.data ? file.data.content : null;
  const lines = content?.split("\n").slice(start - 1, Math.min(end, start + PEEK_LINES - 1)) ?? [];
  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs">
        <FileCode2 aria-hidden className="size-3.5 text-fg-muted" />
        <span className="truncate font-mono">{citation.path}</span>
        <span className="text-fg-faint">
          {start}–{end} · {citation.sha.slice(0, 7)}
        </span>
        {citation.verified ? (
          <span className="ml-auto flex items-center gap-1 text-accent">
            <BadgeCheck aria-hidden className="size-3.5" /> verified
          </span>
        ) : (
          <span className="ml-auto text-fg-faint">not verified</span>
        )}
      </div>
      <pre className="max-h-72 overflow-auto px-3 py-2 font-mono text-[12px] leading-5">
        {file.isPending ? (
          <span className="text-fg-faint">Loading…</span>
        ) : lines.length ? (
          lines.map((line, i) => (
            <div key={start + i} className="flex gap-3">
              <span className="w-8 shrink-0 text-right text-fg-faint select-none">{start + i}</span>
              <span className="whitespace-pre">{line}</span>
            </div>
          ))
        ) : (
          <span className="text-fg-faint">These lines can&apos;t be shown here.</span>
        )}
      </pre>
      {onOpen ? (
        <div className="flex justify-end border-t border-border px-3 py-2">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              onOpen(citation);
            }}
          >
            Open beside the answer
          </Button>
        </div>
      ) : null}
    </div>
  );
}
