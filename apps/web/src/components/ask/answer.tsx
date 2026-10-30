"use client";

/**
 * A message of the thread (4.10). A question is its text. An answer is its text as markdown
 * (Streamdown: rendered as it streams), each citation a chip after the words it backs; above
 * it, while it is written, what the run is doing; after it, what its tools found as cards
 * (4.11.3), an abstention if the run couldn't stand behind it (4.11), how sure it is, what it
 * searched (collapsed), the user's feedback, and questions to ask next about what it found.
 */
import { cn } from "cn";
import { ChevronRight, CornerDownRight, FileCode2, Loader2 } from "lucide-react";
import type { Route } from "next";
import NextLink from "next/link";
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
  scopeLabel,
  scopeOf,
  searchesOf,
  statusLine,
  withMarkers,
  type AskMessage,
  type CitationData,
  type ConfidenceData,
  type FeedbackKind,
} from "@/lib/api/ask";
import { cardsOf, followUps } from "@/lib/api/cards";

import { AbstainCard } from "./abstain-card";
import { CitationChip } from "./citation-chip";
import { ConfidenceBadge } from "./confidence-badge";
import { FeedbackButtons } from "./feedback";
import { ToolCards, type Artifact, type CodeHref } from "./tool-cards";

const MARKER = /^#cite-(\d+)$/;

export function Question({ message }: { message: AskMessage }) {
  const scope = scopeOf(message);
  return (
    <div className="ml-auto flex max-w-[80%] flex-col items-end gap-1">
      {scope ? (
        <span
          className="flex items-center gap-1 rounded-sm border border-border px-1.5 font-mono text-[11px] text-fg-muted"
          title={scope.path ?? undefined}
          data-testid="question-scope"
        >
          <FileCode2 aria-hidden className="size-3" />
          <span className="sr-only">About </span>
          {scopeLabel(scope)}
        </span>
      ) : null}
      <div className="rounded-lg bg-muted px-3 py-2 text-sm whitespace-pre-wrap">
        {answerText(message)}
      </div>
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
  runHref,
  codeHref,
  traceHref,
  onPromote,
  asked = [],
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
  /** Its run's timeline (none on a shared page). */
  runHref?: Route | null | undefined;
  /** Where the cards' code opens (none on a shared page: names without links). */
  codeHref?: CodeHref | undefined;
  /** A trace card's entry on the Trace tab. */
  traceHref?: ((entry: string) => Route | null) | undefined;
  /** Keeps a card open in the artifact panel (none: no Promote). */
  onPromote?: ((artifact: Artifact) => void) | undefined;
  /** The thread's questions so far, so a follow-up isn't one of them. */
  asked?: readonly string[] | undefined;
}) {
  const citations = citationsOf(message);
  const byNumber = new Map(citations.map((c) => [c.n, c]));
  const text = answerText(message);
  const status = statusLine(message, streaming);
  const sure = confidenceOf(message);
  const abstained = abstainedOf(message);
  const drawn = chip ?? ((c: CitationData) => <CitationChip citation={c} onOpen={onOpen} />);
  const slots = cardsOf(message);
  const next = onAsk && !streaming && !abstained && text ? followUps(slots, asked) : [];

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
      <ToolCards slots={slots} codeHref={codeHref} traceHref={traceHref} onPromote={onPromote} />
      {abstained && !streaming ? <AbstainCard abstain={abstained} onAsk={onAsk} /> : null}
      {!streaming ? (
        <Footer
          message={message}
          confidence={sure}
          cached={fromCache(message)}
          feedback={text ? feedback : undefined}
          runHref={runHref ?? null}
        />
      ) : null}
      {next.length > 0 ? (
        <nav aria-label="Ask next" className="flex flex-col gap-1" data-testid="follow-ups">
          {next.map((question) => (
            <button
              key={question}
              type="button"
              className="flex w-fit items-center gap-1.5 rounded-md px-1.5 py-0.5 text-left text-xs text-fg-muted hover:bg-surface-2 hover:text-fg"
              onClick={() => onAsk?.(question)}
            >
              <CornerDownRight aria-hidden className="size-3 shrink-0" />
              {question}
            </button>
          ))}
        </nav>
      ) : null}
    </div>
  );
}

function Footer({
  message,
  confidence,
  cached,
  feedback,
  runHref,
}: {
  message: AskMessage;
  confidence: ConfidenceData | null;
  cached: boolean;
  feedback: FeedbackKind | null | undefined;
  runHref: Route | null;
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
      {runHref ? (
        <NextLink href={runHref} className="hover:text-fg hover:underline">
          View run
        </NextLink>
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
