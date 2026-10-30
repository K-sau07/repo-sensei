"use client";

/**
 * `/[org]/[repo]/ask` and `/ask/[conversationId]` (4.10, frontend spec §6.7): questions about
 * the repository, answered with citations. The thread, then the question box; beside them the
 * evidence rail, or the code a citation was opened at; before them (wide screens) the history.
 * A new conversation's address becomes its own as its first answer starts (without leaving the
 * page: the answer keeps streaming).
 */
import { MessageSquare } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { CodeViewer } from "@/components/code/code-viewer";
import { useFileAt } from "@/components/code/use-code";
import { EmptyState } from "@/components/feedback/empty-state";
import { LockedState } from "@/components/feedback/locked-state";
import { useRepoFeatures, useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import {
  answerText,
  citationsOf,
  draftKey,
  runOf,
  titleOr,
  type AskMessage,
  type CitationData,
  type FeedbackKind,
  type ScopeData,
} from "@/lib/api/ask";
import { parseLines } from "@/lib/code/lines";
import { STAGE_LABELS } from "@/lib/indexing";
import { codePath, repoPath } from "@/lib/routes";

import { Answer, Question } from "./answer";
import { ArtifactPanel, useArtifacts } from "./artifact-panel";
import { Composer, NO_SCOPE } from "./composer";
import { EvidenceRail } from "./evidence-rail";
import { ConversationHistory } from "./history";
import { ShareButton } from "./share-dialog";
import type { CodeHref } from "./tool-cards";
import { useAsk, useConversations } from "./use-ask";

/** The conversation an address names (`/ask/<id>`; none: a new one). */
function conversationIn(pathname: string): string | null {
  return /\/ask\/([^/]+)\/?$/.exec(pathname)?.[1] ?? null;
}

/**
 * The thread, started again whenever the address names another conversation than the one it
 * holds: "New conversation", a conversation picked in the history, or the open one deleted.
 * Not when the thread names its own new conversation (it writes the address itself, keeping
 * the page as it is, so the answer being written isn't cut off).
 */
function useThreadKey(): { key: number; named: (id: string) => void } {
  const pathname = usePathname();
  const held = useRef<string | null>(conversationIn(pathname));
  const [key, setKey] = useState(0);
  const shown = conversationIn(pathname);
  useEffect(() => {
    if (shown !== held.current) {
      held.current = shown;
      setKey((k) => k + 1);
    }
  }, [shown]);
  return {
    key,
    named: (id) => {
      held.current = id;
    },
  };
}

export function AskPage() {
  const { state, ready } = useRepoFeatures();
  const thread = useThreadKey();
  if (!ready) return null; // what the repository can do is still being read
  if (!state("ask").available) return <NotAskable />;
  return <Ask key={thread.key} onNamed={thread.named} />;
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

function Ask({ onNamed }: { onNamed: (id: string) => void }) {
  const { org, repo } = useRepoSlug();
  const router = useRouter();
  const pathname = usePathname();
  // As the address has it (after the first answer it names it here, with the page kept).
  const [conversationId, setConversationId] = useState<string | null>(() =>
    conversationIn(pathname),
  );
  const chat = useAsk(conversationId, (id) => {
    setConversationId(id);
    onNamed(id);
    window.history.replaceState(null, "", repoPath(org, repo, `ask/${id}`));
  });
  const [opened, setOpened] = useState<CitationData | null>(null);
  const [scope, setScope] = useState<ScopeData>(NO_SCOPE);
  const artifacts = useArtifacts();
  const codeHref: CodeHref = (path, [start, end]) =>
    codePath(org, repo, path, { lines: start === end ? `${start}` : `${start}-${end}` });
  const traceHref = (entry: string) =>
    `${repoPath(org, repo, "trace")}?from=${encodeURIComponent(entry)}` as Route;
  const streaming = chat.status === "submitted" || chat.status === "streaming";
  const latest = [...chat.messages].reverse().find((m) => m.role === "assistant");
  const announcement = useAnnouncement(latest, streaming);
  const said = new Map<string, FeedbackKind>(
    (chat.stored?.messages ?? []).flatMap((m) => (m.feedback ? [[m.id, m.feedback.kind]] : [])),
  );
  const runs = new Map<string, string>(
    (chat.stored?.messages ?? []).flatMap((m) => (m.run_id ? [[m.id, m.run_id]] : [])),
  );
  const runHref = (message: AskMessage) => {
    const id = runOf(message) ?? runs.get(message.id);
    return id ? repoPath(org, repo, `runs/${id}`) : null;
  };
  const asked = chat.messages.filter((m) => m.role === "user").map(answerText);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [chat.messages.length]);
  useAskedFromBar(conversationId === null, chat.ask);

  return (
    <div className="grid h-full min-h-0 grid-cols-1 md:grid-cols-[minmax(0,1fr)_22rem] lg:grid-cols-[15rem_minmax(0,1fr)_22rem]">
      <div className="hidden min-h-0 lg:block">
        <ConversationHistory
          current={conversationId}
          onDeleted={(id) => {
            if (id === conversationId) router.push(repoPath(org, repo, "ask"));
          }}
        />
      </div>
      <section aria-label="Conversation" className="flex min-h-0 flex-col">
        {conversationId !== null && chat.messages.length > 0 ? (
          <header className="flex items-center gap-2 border-b border-border px-6 py-2">
            <h1 className="min-w-0 flex-1 truncate text-sm font-medium">
              {titleOr(chat.stored?.title, "New conversation")}
            </h1>
            {!streaming ? (
              <ShareButton
                key={conversationId}
                conversationId={conversationId}
                shared={chat.stored?.shared ?? false}
              />
            ) : null}
          </header>
        ) : null}
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
                  onAsk={streaming ? undefined : (text) => void chat.ask(text)}
                  feedback={said.get(message.id) ?? null}
                  runHref={runHref(message)}
                  codeHref={codeHref}
                  traceHref={traceHref}
                  onPromote={(artifact) => {
                    setOpened(null);
                    artifacts.promote(artifact);
                  }}
                  asked={asked}
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
            scope={scope}
            onScope={setScope}
            onSend={(text, scoped) => void chat.ask(text, scoped)}
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
        ) : artifacts.current ? (
          <ArtifactPanel
            pinned={artifacts.pinned}
            current={artifacts.current}
            onShow={artifacts.show}
            onClose={artifacts.close}
            codeHref={codeHref}
            traceHref={traceHref}
          />
        ) : (
          <EvidenceRail message={latest} onOpen={setOpened} codeHref={codeHref} />
        )}
      </div>
    </div>
  );
}

/** A question the Ask bar sent here (`?q=`, its scope `?scope=` and `?L=`): asked once, the
 * address left clean. */
function useAskedFromBar(fresh: boolean, ask: (text: string, scope: ScopeData | null) => unknown) {
  const search = useSearchParams();
  const asked = useRef(false);
  const latest = useRef(ask);
  useEffect(() => {
    latest.current = ask;
  });
  useEffect(() => {
    const question = search.get("q");
    if (!fresh || asked.current || !question) return;
    const path = search.get("scope");
    const lines = parseLines(search.get("L") ?? "");
    const scope = path
      ? { path, startLine: lines?.start ?? null, endLine: lines?.end ?? null }
      : null;
    // Once the page has settled: a chat mounted twice (development's strict mode) drops what
    // was sent from its first mount.
    const timer = setTimeout(() => {
      asked.current = true;
      window.history.replaceState(null, "", window.location.pathname);
      void latest.current(question, scope);
    }, 0);
    return () => {
      clearTimeout(timer);
    };
  }, [fresh, search]);
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
        <nav aria-label="Recent conversations" className="flex flex-col gap-1 lg:hidden">
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
  const file = useFileAt(citation.path, citation.sha);
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
          <p className="p-3 text-xs text-fg-faint">
            {file.isPending ? "Loading…" : "This file can’t be shown here."}
          </p>
        )}
      </div>
    </aside>
  );
}
