"use client";

/**
 * "New commits indexed" (5.6): a ref's index moved on while the repository's page was open, at
 * the ref the page is read at. What the page shows was read before it, so "Refresh view" reads
 * it all again (a pinned commit stays pinned: its banner offers the latest instead).
 */
import { useQueryClient } from "@tanstack/react-query";
import { GitCommitHorizontal, X } from "lucide-react";
import { useState } from "react";

import { useRepoUpdates, updateLabel, type RepoUpdate } from "@/components/repos/repo-updates";
import { Button } from "@/components/ui/button";
import { isCommit } from "@/lib/api/refs";
import type { Repo } from "@/lib/api/types";

import { useViewRef } from "./use-ref";

/** Whether an update concerns a page read at `ref` (none: the default branch). */
export function concerns(update: RepoUpdate, ref: string | null, defaultBranch: string): boolean {
  if (ref !== null && isCommit(ref)) return update.ref === defaultBranch;
  return update.ref === (ref ?? defaultBranch);
}

export function NewCommitsBanner({ repo }: { repo: Repo }) {
  const [opened] = useState(() => Date.now());
  const [ref, setRef] = useViewRef();
  const update = useRepoUpdates((state) => state.updates[repo.id]);
  const dismiss = useRepoUpdates((state) => state.dismiss);
  const queryClient = useQueryClient();
  if (!update || update.at < opened || !concerns(update, ref, repo.default_branch)) return null;
  const pinned = ref !== null && isCommit(ref);
  const files = update.changedFiles ?? [];
  return (
    <NewCommitsNotice
      label={updateLabel(update)}
      sha={update.sha}
      files={files}
      action={pinned ? `View ${repo.default_branch}'s latest` : "Refresh view"}
      onAction={() => {
        dismiss(repo.id);
        if (pinned) {
          void setRef(null);
          return;
        }
        // Everything shown was read at the index before: read it again (a constellation's
        // pages start over, as they do when a run ends).
        void queryClient.resetQueries({ queryKey: ["constellation"] });
        void queryClient.invalidateQueries();
      }}
      onDismiss={() => {
        dismiss(repo.id);
      }}
    />
  );
}

/** The banner itself (stories draw it without a stream). */
export function NewCommitsNotice({
  label,
  sha,
  files,
  action,
  onAction,
  onDismiss,
}: {
  label: string;
  sha: string;
  files: readonly string[];
  action: string;
  onAction: () => void;
  onDismiss: () => void;
}) {
  return (
    <div
      role="status"
      data-testid="new-commits"
      className="mx-6 mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-signal/30 bg-signal/5 px-3 py-2 text-sm"
    >
      <GitCommitHorizontal aria-hidden className="size-4 text-signal" />
      <span className="font-medium">{label}</span>
      <span className="font-mono text-xs text-fg-muted">{sha.slice(0, 7)}</span>
      {files.length > 0 ? (
        <span className="min-w-0 truncate text-xs text-fg-muted" title={files.join("\n")}>
          {files.slice(0, 3).join(", ")}
          {files.length > 3 ? ` and ${String(files.length - 3)} more` : ""}
        </span>
      ) : null}
      <span className="ml-auto flex items-center gap-1">
        <Button size="sm" variant="outline" onClick={onAction}>
          {action}
        </Button>
        <Button size="icon-sm" variant="ghost" aria-label="Dismiss" onClick={onDismiss}>
          <X aria-hidden className="size-4" />
        </Button>
      </span>
    </div>
  );
}
