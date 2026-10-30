"use client";

/**
 * `/[org]/[repo]/ask` and `/ask/[conversationId]` (4.10, frontend spec §6.7): questions about
 * the repository, answered with citations. The thread, then the question box; beside them the
 * evidence rail, or the code a citation was opened at. A new conversation's address becomes
 * its own as its first answer starts (without leaving the page: the answer keeps streaming).
 */
import { MessageSquare } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { CodeViewer } from "@/components/code/code-viewer";
import { useFile } from "@/components/code/use-code";
import { EmptyState } from "@/components/feedback/empty-state";
import { LockedState } from "@/components/feedback/locked-state";
import { useRepoFeatures, useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { citationsOf, draftKey, type AskMessage, type CitationData } from "@/lib/api/ask";
import { STAGE_LABELS } from "@/lib/indexing";
import { repoPath } from "@/lib/routes";

import { Answer, Question } from "./answer";
import { Composer } from "./composer";
import { EvidenceRail } from "./evidence-rail";
import { useAsk, useConversations } from "./use-ask";

export function AskPage() {
  const { state, ready } = useRepoFeatures();
  if (!ready) return null; // what the repository can do is still being read
  if (!state("ask").available) return <NotAskable />;
  return <Ask />;
}

/** Not indexed for questions yet: locked while a run brings it, else waiting for an index. */
function NotAskable() {
  const { org, repo } = useRepoSlug();
  const { state, active } = useRepoFeatures();
  const ask = state("ask");
  const stage = ask.stage ? STAGE_LABELS[ask.stage] : "indexing";
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col p-8">
      {active && ask.coming ? (
        <LockedState
          feature="Ask"
          stage={stage}
          {...(ask.etaSeconds !== null ? { etaSeconds: ask.etaSeconds } : {})}
          href={repoPath(org, repo)}
        />
      ) : (
        <EmptyState
          icon={<MessageSquare aria-hidden />}
          title="Questions open after indexing"
          description="Ask reads the symbols and code chunks made when this repository is indexed."
          action={
            <Link
              href={repoPath(org, repo, "index")}
              className="text-sm underline underline-offset-4"
            >
              Index this repository
            </Link>
          }
        />
      )}
    </div>
  );
}

function Ask() {
  const { org, repo } = useRepoSlug();
  const params = useParams<{ conversationId?: string }>();
  const [conversationId, setConversationId] = useState<string | null>(
    params.conversationId ?? null,
  );
  const chat = useAsk(conversationId, (id) => {
    setConversationId(id);
    window.history.replaceState(null, "", repoPath(org, repo, `ask/${id}`));
  });
  const [opened, setOpened] = useState<CitationData | null>(null);
  const streaming = chat.status === "submitted" || chat.status === "streaming";
  const latest = [...chat.messages].reverse().find((m) => m.role === "assistant");
  const announcement = useAnnouncement(latest, streaming);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [chat.messages.length]);

  return (
    <div className="grid h-full min-h-0 grid-cols-1 md:grid-cols-[minmax(0,1fr)_22rem]">
      <section aria-label="Conversation" className="flex min-h-0 flex-col">
        <div className="flex-1 overflow-auto">
          <div className="mx-auto flex max-w-3xl flex-col gap-6 p-6">
            {chat.messages.length === 0 && !chat.loading ? <Start /> : null}
            {chat.messages.map((message, i) =>
              message.role === "user" ? (
                <Question key={message.id} message={message} />
              ) : (
                <Answer
                  key={message.id}
                  message={message}
                  streaming={streaming && i === chat.messages.length - 1}
                  onOpen={setOpened}
                />
              ),
            )}
            {chat.error ? (
              <p role="alert" className="text-sm text-danger">
                The answer couldn&apos;t be written: {chat.error.message}
              </p>
            ) : null}
            <div ref={end} />
          </div>
        </div>
        <div className="mx-auto w-full max-w-3xl px-6 pb-6">
          <Composer
            key={draftKey(org, repo, conversationId)}
            draftKey={draftKey(org, repo, conversationId)}
            busy={streaming}
            mode={chat.mode}
            onMode={chat.setMode}
            onSend={(text) => void chat.ask(text)}
            onStop={() => void chat.stop()}
          />
        </div>
        <p aria-live="polite" className="sr-only" data-testid="announcer">
          {announcement}
        </p>
      </section>
      <div className="hidden min-h-0 md:block">
        {opened ? (
          <CitedCode
            citation={opened}
            onClose={() => {
              setOpened(null);
            }}
          />
        ) : (
          <EvidenceRail message={latest} onOpen={setOpened} />
        )}
      </div>
    </div>
  );
}

/** What a screen reader hears when an answer is done: "Answer ready, 6 citations". */
function useAnnouncement(latest: AskMessage | undefined, streaming: boolean): string {
  const [said, setSaid] = useState("");
  const was = useRef(false);
  useEffect(() => {
    if (was.current && !streaming && latest) {
      const n = citationsOf(latest).length;
      setSaid(`Answer ready, ${n} citation${n === 1 ? "" : "s"}`);
    }
    was.current = streaming;
  }, [streaming, latest]);
  return said;
}

function Start() {
  const { org, repo } = useRepoSlug();
  const recent = useConversations();
  return (
    <div className="flex flex-col gap-6">
      <EmptyState
        icon={<MessageSquare aria-hidden />}
        title="Ask about this repository"
        description="How a feature works end to end, where something is configured, what calls a function. Every answer cites the code it comes from."
      />
      {recent.data?.length ? (
        <nav aria-label="Recent conversations" className="flex flex-col gap-1">
          <p className="text-xs font-medium text-fg-muted">Recent</p>
          {recent.data.map((c) => (
            <Link
              key={c.id}
              href={repoPath(org, repo, `ask/${c.id}`)}
              className="truncate rounded-sm px-2 py-1 text-sm hover:bg-muted"
            >
              {c.title || "Untitled"}
            </Link>
          ))}
        </nav>
      ) : null}
    </div>
  );
}

function CitedCode({ citation, onClose }: { citation: CitationData; onClose: () => void }) {
  const file = useFile(citation.path);
  const content = file.data && "content" in file.data ? file.data.content : null;
  const language = file.data && "language" in file.data ? (file.data.language ?? null) : null;
  return (
    <aside
      aria-label="Cited code"
      className="flex h-full flex-col border-l border-border"
      data-testid="cited-code"
    >
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="truncate font-mono text-xs">{citation.path}</span>
        <Button size="sm" variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
      <div className="min-h-0 flex-1">
        {content !== null ? (
          <CodeViewer
            path={citation.path}
            language={language}
            content={content}
            lines={{ start: citation.lines[0], end: citation.lines[1] }}
            onLinesChange={() => undefined}
            wrap={false}
          />
        ) : (
          <p className="text-fg-subtle p-3 text-xs">
            {file.isPending ? "Loading…" : "This file can’t be shown here."}
          </p>
        )}
      </div>
    </aside>
  );
}
