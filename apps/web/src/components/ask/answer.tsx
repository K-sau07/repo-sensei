"use client";

/**
 * A message of the thread (4.10). A question is its text. An answer is its text as markdown
 * (Streamdown: rendered as it streams), each citation a chip after the words it backs; above
 * it, while it is written, what the run is doing; after it, what it searched (collapsed).
 */
import { cn } from "cn";
import { ChevronRight, Loader2 } from "lucide-react";
import { useState } from "react";
import { Streamdown, type Components } from "streamdown";

import {
  activity,
  answerText,
  citationsOf,
  confidenceOf,
  fromCache,
  searchesOf,
  statusLine,
  withMarkers,
  type AskMessage,
  type CitationData,
} from "@/lib/api/ask";

import { CitationChip } from "./citation-chip";

const MARKER = /^#cite-(\d+)$/;

export function Question({ message }: { message: AskMessage }) {
  return (
    <div className="ml-auto max-w-[80%] rounded-lg bg-muted px-3 py-2 text-sm whitespace-pre-wrap">
      {answerText(message)}
    </div>
  );
}

export function Answer({
  message,
  streaming,
  onOpen,
}: {
  message: AskMessage;
  streaming: boolean;
  onOpen: (citation: CitationData) => void;
}) {
  const citations = citationsOf(message);
  const byNumber = new Map(citations.map((c) => [c.n, c]));
  const text = answerText(message);
  const status = statusLine(message, streaming);
  const sure = confidenceOf(message);

  // A citation's marker (`[n](#cite-n)`) as its chip; any other link as a link.
  const Link: NonNullable<Components["a"]> = ({ href, children }) => {
    const marked = href ? MARKER.exec(href) : null;
    const n = marked ? Number(marked[1]) : typeof children === "string" ? Number(children) : NaN;
    const citation = Number.isFinite(n) ? byNumber.get(n) : undefined;
    if (citation !== undefined && (marked || href === undefined)) {
      return <CitationChip citation={citation} onOpen={onOpen} />;
    }
    return (
      <a href={href} target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  };

  return (
    <div className="flex flex-col gap-2" data-testid="answer">
      {(streaming || !text) && status ? (
        <p className="flex items-center gap-2 text-xs text-fg-muted" data-testid="status-line">
          {streaming ? <Loader2 aria-hidden className="size-3.5 animate-spin" /> : null}
          {status}
        </p>
      ) : null}
      {text ? (
        <div className="prose prose-sm max-w-none text-fg">
          <Streamdown
            mode={streaming ? "streaming" : "static"}
            isAnimating={streaming}
            linkSafety={{ enabled: false }}
            components={{ a: Link }}
          >
            {withMarkers(text, citations)}
          </Streamdown>
        </div>
      ) : null}
      {!streaming ? (
        <Footer message={message} level={sure?.level ?? null} cached={fromCache(message)} />
      ) : null}
    </div>
  );
}

function Footer({
  message,
  level,
  cached,
}: {
  message: AskMessage;
  level: "high" | "medium" | "low" | null;
  cached: boolean;
}) {
  const [open, setOpen] = useState(false);
  const searches = searchesOf(message);
  const done = activity(message);
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs text-fg-muted">
      {level ? (
        <span data-testid="confidence" className={cn(level === "low" && "text-warning")}>
          Confidence: {level}
        </span>
      ) : null}
      {cached ? <span>Answered from an earlier answer</span> : null}
      {searches.length || done.length ? (
        <button
          type="button"
          className="flex items-center gap-1 hover:text-fg"
          aria-expanded={open}
          onClick={() => {
            setOpen((was) => !was);
          }}
        >
          <ChevronRight aria-hidden className={cn("size-3 transition", open && "rotate-90")} />
          Show searches ({searches.length})
        </button>
      ) : null}
      {open ? (
        <ul className="w-full list-disc pl-5 font-mono" data-testid="searches">
          {searches.map((query, i) => (
            <li key={`${i}-${query}`}>{query}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
