"use client";

/**
 * The Ask bar (4.13, ⌘L): a question from any page of a repository, with what is open as its
 * scope (the file in the code browser, the lines picked there), a chip that can be dropped. It
 * opens a new conversation on the Ask tab, which asks it. Its button floats from tablets up (a
 * phone's bottom bar has an Ask of its own).
 */
import { FileCode2, MessageSquareText, X } from "lucide-react";
import type { Route } from "next";
import { useParams, useRouter, useSearchParams, useSelectedLayoutSegment } from "next/navigation";
import { useState } from "react";

import { decodeSegment } from "@/components/code/use-code";
import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { Textarea } from "@/components/ui/textarea";
import { useShortcut } from "@/hooks/use-shortcut";
import { scopeLabel, type ScopeData } from "@/lib/api/ask";
import { parseLines } from "@/lib/code/lines";
import { repoPath } from "@/lib/routes";

/** The scope a question asked from here has: the open file and its picked lines, if any. */
function useOpenScope(): ScopeData | null {
  const section = useSelectedLayoutSegment();
  const params = useParams<{ path?: string[] }>();
  const search = useSearchParams();
  if (section !== "code" || !params.path?.length) return null;
  const path = params.path.map(decodeSegment).join("/");
  const lines = parseLines(search.get("L") ?? "");
  return { path, startLine: lines?.start ?? null, endLine: lines?.end ?? null };
}

/** Where a question asked from the bar goes: the Ask tab, which asks it once it opens. */
export function askHref(org: string, repo: string, question: string, scope: ScopeData | null) {
  const query = new URLSearchParams({ q: question });
  if (scope?.path) {
    query.set("scope", scope.path);
    if (scope.startLine !== null) {
      const end = scope.endLine ?? scope.startLine;
      query.set("L", end === scope.startLine ? `${scope.startLine}` : `${scope.startLine}-${end}`);
    }
  }
  return `${repoPath(org, repo, "ask")}?${query.toString()}` as Route;
}

export function AskBar() {
  const { org, repo } = useRepoSlug();
  const router = useRouter();
  const open = useOpenScope();
  const [shown, setShown] = useState(false);
  const [question, setQuestion] = useState("");
  const [dropped, setDropped] = useState(false);
  // On the Ask tab (its own question box): the repository's section, not a folder named so.
  const onAsk = useSelectedLayoutSegment() === "ask";
  useShortcut(
    "ask-bar",
    () => {
      setShown(true);
    },
    { inInputs: true, enabled: !onAsk },
  );
  if (onAsk) return null; // the Ask tab has its own question box
  const scope = dropped ? null : open;

  const send = () => {
    const text = question.trim();
    if (!text) return;
    setShown(false);
    setQuestion("");
    router.push(askHref(org, repo, text, scope));
  };

  return (
    <>
      <Button
        variant="outline"
        className="fixed right-6 bottom-6 z-40 hidden gap-2 rounded-full shadow-md md:inline-flex"
        onClick={() => {
          setDropped(false);
          setShown(true);
        }}
      >
        <MessageSquareText aria-hidden />
        Ask
        <Kbd aria-hidden>⌘L</Kbd>
      </Button>
      <Dialog
        open={shown}
        onOpenChange={(next) => {
          setShown(next);
          if (!next) setDropped(false);
        }}
      >
        <DialogContent data-testid="ask-bar">
          <DialogHeader>
            <DialogTitle>Ask about this repository</DialogTitle>
            <DialogDescription>
              {scope
                ? "About what's open; drop the scope to ask about the whole repository."
                : "Answers cite the code they come from."}
            </DialogDescription>
          </DialogHeader>
          {scope ? (
            <span className="flex w-fit items-center gap-1 rounded-sm border border-border px-1.5 py-0.5 font-mono text-xs">
              <FileCode2 aria-hidden className="size-3.5 text-fg-muted" />
              <span title={scope.path ?? undefined}>{scopeLabel(scope)}</span>
              <button
                type="button"
                aria-label={`Drop the scope ${scopeLabel(scope)}`}
                className="text-fg-muted hover:text-fg"
                onClick={() => {
                  setDropped(true);
                }}
              >
                <X aria-hidden className="size-3" />
              </button>
            </span>
          ) : null}
          <form
            className="flex flex-col gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              send();
            }}
          >
            <Textarea
              aria-label="Your question"
              placeholder="How does this work? Where is it called from?"
              value={question}
              maxLength={4000}
              onChange={(event) => {
                setQuestion(event.target.value);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault();
                  send();
                }
              }}
            />
            <div className="flex justify-end">
              <Button type="submit" disabled={!question.trim()}>
                Ask
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
