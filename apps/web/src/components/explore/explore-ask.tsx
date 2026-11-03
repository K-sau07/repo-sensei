"use client";

/**
 * The public page's floating Ask bar (10.2): a question about the repository, asked as no one
 * and answered briefly (the API's fast mode), each citation a chip that opens its lines on
 * GitHub at the commit the answer read. Each question first passes Cloudflare Turnstile's check
 * (`human`): its token is good for one question, so the check starts again after each.
 */
import { useChat } from "@ai-sdk/react";
import { ArrowUp, ChevronDown, ChevronUp, Loader2 } from "lucide-react";
import Link from "next/link";
import { useMemo, useRef, useState, type SubmitEvent } from "react";

import { Answer, Question } from "@/components/ask/answer";
import { useAnnouncement } from "@/components/ask/use-announcement";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { chipLabel, type AskMessage, type CitationData } from "@/lib/api/ask";
import { askFailure, DUMMY_TOKEN, githubBlob, publicAskTransport } from "@/lib/api/explore";

import { Turnstile } from "./turnstile";

/** How a question proves a person asks it: Turnstile's check, none to pass (mock mode: the
 * check's dummy token), or no questions here (no site key). */
export type HumanCheck = { kind: "widget"; siteKey: string } | { kind: "mock" } | { kind: "off" };

const MAX_CHARS = 1_000; // the API's
const SUGGESTED = 3;

export function ExploreAsk({
  owner,
  name,
  questions,
  human,
}: {
  owner: string;
  name: string;
  /** Questions worth asking (the overview's), offered before the first. */
  questions: readonly string[];
  human: HumanCheck;
}) {
  const fullName = `${owner}/${name}`;
  const transport = useMemo(() => publicAskTransport(owner, name), [owner, name]);
  const asked = useRef("");
  const chat = useChat<AskMessage>({
    transport,
    // Refused (a limit, the check): the question back in the box, to ask again as it was.
    onError: () => {
      chat.setMessages((messages) =>
        messages.at(-1)?.role === "user" ? messages.slice(0, -1) : messages,
      );
      setText((now) => now || asked.current);
    },
  });
  const [text, setText] = useState("");
  const [folded, setFolded] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [token, setToken] = useState<string | null>(human.kind === "mock" ? DUMMY_TOKEN : null);
  const [round, setRound] = useState(0);
  const busy = chat.status === "submitted" || chat.status === "streaming";
  const open = human.kind !== "off";
  const ready = open && token !== null && !busy;

  const ask = (question: string) => {
    const words = question.trim();
    if (!words || !ready) return;
    asked.current = words;
    void chat.sendMessage({ text: words }, { body: { token } });
    setText("");
    setFolded(false);
    if (human.kind === "widget") {
      setToken(null); // spent: the check starts again for the next question
      setRound((n) => n + 1);
    }
  };
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    ask(text);
  };
  const last = chat.messages.at(-1);
  const answered = chat.messages.some((message) => message.role === "assistant");
  const announcement = useAnnouncement(last?.role === "assistant" ? last : undefined, busy);

  return (
    <section
      aria-label={`Ask about ${fullName}`}
      className="pointer-events-none fixed inset-x-0 bottom-0 z-30 flex justify-center p-3"
    >
      <div className="pointer-events-auto flex w-full max-w-2xl flex-col gap-2 rounded-xl border border-border bg-bg p-3 shadow-lg">
        {chat.messages.length > 0 && (
          <div className="flex justify-end">
            <Button
              type="button"
              variant="ghost"
              size="xs"
              aria-expanded={!folded}
              aria-controls="explore-thread"
              onClick={() => {
                setFolded((was) => !was);
              }}
            >
              {folded ? <ChevronUp aria-hidden /> : <ChevronDown aria-hidden />}
              {folded ? "Show answers" : "Hide answers"}
            </Button>
          </div>
        )}
        {chat.messages.length > 0 && !folded && (
          <div
            id="explore-thread"
            className="flex max-h-[35vh] flex-col gap-4 overflow-y-auto pr-1 sm:max-h-[50vh]"
            data-testid="explore-thread"
          >
            {chat.messages.map((message) =>
              message.role === "user" ? (
                <Question key={message.id} message={message} />
              ) : (
                <Answer
                  key={message.id}
                  message={message}
                  streaming={busy && message.id === last?.id}
                  chip={(citation) => <SourceChip citation={citation} fullName={fullName} />}
                />
              ),
            )}
          </div>
        )}
        {chat.error && (
          <p role="alert" className="text-sm text-danger">
            {askFailure(chat.error)}
          </p>
        )}
        {!answered && questions.length > 0 && open && (
          <ul className="flex flex-wrap gap-1.5" aria-label="Questions worth asking">
            {questions.slice(0, SUGGESTED).map((question) => (
              <li key={question}>
                <button
                  type="button"
                  disabled={!ready}
                  onClick={() => {
                    ask(question);
                  }}
                  className="rounded-full border border-border px-2.5 py-1 text-left text-xs text-fg-muted hover:bg-muted hover:text-fg disabled:opacity-50"
                >
                  {question}
                </button>
              </li>
            ))}
          </ul>
        )}
        {open ? (
          <form onSubmit={submit} className="flex items-end gap-2">
            <Textarea
              aria-label="Your question"
              placeholder={`Ask about ${fullName}…`}
              value={text}
              maxLength={MAX_CHARS}
              rows={1}
              className="max-h-32 min-h-9 resize-none"
              onChange={(event) => {
                setText(event.target.value);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  ask(text);
                }
              }}
            />
            <Button type="submit" size="icon" disabled={!ready || !text.trim()} aria-label="Ask">
              {busy ? <Loader2 aria-hidden className="animate-spin" /> : <ArrowUp aria-hidden />}
            </Button>
          </form>
        ) : (
          <p className="text-sm text-fg-muted">
            Questions about public repositories aren&apos;t open here yet.
          </p>
        )}
        {human.kind === "widget" &&
          (unavailable ? (
            <p className="text-sm text-fg-muted">
              The check that you&apos;re a person couldn&apos;t load.{" "}
              <button
                type="button"
                className="underline hover:text-fg"
                onClick={() => {
                  setUnavailable(false);
                  setAttempt((n) => n + 1);
                }}
              >
                Try again
              </button>
            </p>
          ) : (
            <Turnstile
              key={attempt}
              siteKey={human.siteKey}
              round={round}
              onToken={setToken}
              onUnavailable={() => {
                setUnavailable(true);
              }}
            />
          ))}
        <p aria-live="polite" className="sr-only" data-testid="announcer">
          {announcement}
        </p>
        <p className="text-xs text-fg-faint">
          Short answers, asked as no one: each question stands alone.{" "}
          <Link href="/login" className="underline hover:text-fg">
            Sign in
          </Link>{" "}
          to follow up, and to index your own repository.
        </p>
      </div>
    </section>
  );
}

/** A citation as a link to its lines on GitHub, at the commit the answer read. */
export function SourceChip({ citation, fullName }: { citation: CitationData; fullName: string }) {
  if (!citation.path || !citation.sha) {
    return (
      <span className="mx-0.5 font-mono text-[11px] text-fg-faint" data-testid="citation-chip">
        [{citation.n}]
      </span>
    );
  }
  return (
    <a
      href={githubBlob(fullName, citation.sha, citation.path, citation.lines)}
      target="_blank"
      rel="noreferrer noopener"
      data-testid="citation-chip"
      title={`${citation.path}, lines ${citation.lines[0]}–${citation.lines[1]}, on GitHub`}
      className="mx-0.5 inline-flex h-5 items-center gap-1 rounded-sm border border-border bg-muted px-1.5 align-baseline font-mono text-[11px] leading-none text-fg-muted hover:text-fg"
    >
      <span className="text-fg-faint">{citation.n}</span>
      {chipLabel(citation.path, citation.lines)}
    </a>
  );
}
