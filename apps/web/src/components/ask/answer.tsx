"use client";

/**
 * A message of the thread (4.10). A question is its text. An answer is its text as markdown
 * (Streamdown: rendered as it streams), each citation a chip after the words it backs; above
 * it, while it is written, what the run is doing; after it, an abstention if the run couldn't
 * stand behind it (4.11), how sure it is, what it searched (collapsed) and the user's feedback.
 */
import { cn } from "cn";
import { ChevronRight, Loader2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Streamdown, type Components } from "streamdown";

import {
  abstainedOf,
  activity,
  answerText,
  citationsOf,
  confidenceOf,
  fromCache,
  safeHref,
  searchesOf,
  statusLine,
  withMarkers,
  type AskMessage,
  type CitationData,
  type ConfidenceData,
  type FeedbackKind,
} from "@/lib/api/ask";

import { AbstainCard } from "./abstain-card";
import { CitationChip } from "./citation-chip";
import { ConfidenceBadge } from "./confidence-badge";
import { FeedbackButtons } from "./feedback";

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
  onAsk,
  feedback,
  chip,
}: {
  message: AskMessage;
  streaming: boolean;
  onOpen?: ((citation: CitationData) => void) | undefined;
  /** Asks a follow-up (an abstention's suggestion); none on a read-only page. */
  onAsk?: ((question: string) => void) | undefined;
  /** What the user said of it (undefined: no feedback here, as on a shared page). */
  feedback?: FeedbackKind | null | undefined;
  /** Draws a citation (default: its chip with a peek at the code). */
  chip?: (citation: CitationData) => ReactNode;
}) {
  const citations = citationsOf(message);
  const byNumber = new Map(citations.map((c) => [c.n, c]));
  const text = answerText(message);
  const status = statusLine(message, streaming);
  const sure = confidenceOf(message);
  const abstained = abstainedOf(message);
  const drawn = chip ?? ((c: CitationData) => <CitationChip citation={c} onOpen={onOpen} />);

  // A citation's marker (`[n](#cite-n)`) as its chip; any other link as a link.
  const Link: NonNullable<Components["a"]> = ({ href, children }) => {
    const marked = href ? MARKER.exec(href) : null;
    const n = marked ? Number(marked[1]) : typeof children === "string" ? Number(children) : NaN;
    const citation = Number.isFinite(n) ? byNumber.get(n) : undefined;
    if (citation !== undefined && (marked || href === undefined)) {
      return drawn(citation);
    }
    const safe = safeHref(href);
    if (safe === null) return <span>{children}</span>; // a link it may not open: its words
    return (
      <a href={safe} target="_blank" rel="noreferrer noopener">
        {children}
      </a>
    );
  };

  return (
    <div className="flex flex-col gap-2" data-testid="answer" aria-busy={streaming}>
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
      {abstained && !streaming ? <AbstainCard abstain={abstained} onAsk={onAsk} /> : null}
      {!streaming ? (
        <Footer
          message={message}
          confidence={sure}
          cached={fromCache(message)}
          feedback={text ? feedback : undefined}
        />
      ) : null}
    </div>
  );
}

function Footer({
  message,
  confidence,
  cached,
  feedback,
}: {
  message: AskMessage;
  confidence: ConfidenceData | null;
  cached: boolean;
  feedback: FeedbackKind | null | undefined;
}) {
  const [open, setOpen] = useState(false);
  const searches = searchesOf(message);
  const done = activity(message);
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs text-fg-muted">
      {confidence ? <ConfidenceBadge confidence={confidence} /> : null}
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
      {feedback !== undefined ? (
        <div className="ml-auto">
          <FeedbackButtons messageId={message.id} given={feedback} />
        </div>
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
