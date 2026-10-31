"use client";

import { cn } from "cn";
import {
  ChevronDown,
  ExternalLink,
  GitBranch,
  GitCommitHorizontal,
  GitPullRequest,
} from "lucide-react";
import { useState } from "react";

import { LiveDot } from "@/components/primitives/live-dot";
import { RepoStatus } from "@/components/repos/repo-status";
import { Badge } from "@/components/ui/badge";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import {
  isCommit,
  readable,
  refLabel,
  refParam,
  refStatusLabel,
  type RepoRef,
} from "@/lib/api/refs";
import type { Repo } from "@/lib/api/types";

import { useRefs, useViewRef } from "./use-ref";

/** The ref the repository's pages are read at (5.6): its default branch, the branches it tracks
 * and the pull requests it indexes, each with what its index is, searchable; the one picked is
 * `?ref=` in the URL. A ref with no index yet is listed, but can't be picked. */
export function RefSwitcher({ defaultBranch }: { defaultBranch: string }) {
  const [ref, setRef] = useViewRef();
  const [open, setOpen] = useState(false);
  const refs = useRefs(open);
  const label = refLabel(ref, defaultBranch);
  const pinned = ref !== null && isCommit(ref);
  const Icon = pinned ? GitCommitHorizontal : ref?.startsWith("pull/") ? GitPullRequest : GitBranch;
  const current = ref ?? defaultBranch;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        aria-label={`Branch: ${label}. Switch branch or pull request`}
        data-testid="ref-switcher"
        className="inline-flex h-control-sm items-center gap-1.5 rounded-sm border border-line px-2 font-mono text-xs text-fg-muted hover:bg-surface-2 hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
      >
        <Icon aria-hidden className="size-3.5" />
        {label}
        <ChevronDown aria-hidden className="size-3" />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-0" aria-label="Branches and pull requests">
        <Command loop>
          <CommandInput placeholder="Find a branch or pull request…" />
          {refs.isPending ? (
            <p className="px-3 py-4 text-xs text-fg-muted">Loading…</p>
          ) : refs.isError ? (
            <p role="alert" className="px-3 py-4 text-xs text-danger">
              Couldn&apos;t load the branches.
            </p>
          ) : (
            <>
              <CommandList>
                <RefGroup
                  heading="Branches"
                  refs={refs.data.refs.filter((r) => r.kind === "branch")}
                  current={current}
                  onPick={(picked) => {
                    void setRef(refParam(picked));
                    setOpen(false);
                  }}
                />
                <RefGroup
                  heading="Pull requests"
                  refs={refs.data.refs.filter((r) => r.kind === "pull_request")}
                  current={current}
                  onPick={(picked) => {
                    void setRef(refParam(picked));
                    setOpen(false);
                  }}
                />
              </CommandList>
              <CommandEmpty className="py-4 text-xs text-fg-muted">
                No branch or pull request by that name.
              </CommandEmpty>
            </>
          )}
          {pinned ? (
            <button
              type="button"
              className="border-t border-line px-3 py-2 text-left text-xs text-fg-muted hover:bg-surface-2 hover:text-fg"
              onClick={() => {
                void setRef(null);
                setOpen(false);
              }}
            >
              Pinned to {label}: back to {defaultBranch}&apos;s latest index
            </button>
          ) : null}
        </Command>
      </PopoverContent>
    </Popover>
  );
}

function RefGroup({
  heading,
  refs,
  current,
  onPick,
}: {
  heading: string;
  refs: readonly RepoRef[];
  current: string;
  onPick: (ref: RepoRef) => void;
}) {
  if (refs.length === 0) return null;
  return (
    <CommandGroup heading={heading}>
      {refs.map((ref) => (
        <CommandItem
          key={ref.name}
          value={ref.name}
          disabled={!readable(ref)}
          data-checked={ref.name === current}
          onSelect={() => {
            onPick(ref);
          }}
          className="items-start"
        >
          <span className="flex min-w-0 flex-1 flex-col gap-0.5">
            <span className="flex items-center gap-1.5">
              <span className="truncate font-mono text-xs">{ref.name}</span>
              {ref.default ? (
                <Badge variant="outline" className="h-4 px-1 text-[10px]">
                  default
                </Badge>
              ) : null}
            </span>
            <span
              className={cn(
                "flex items-center gap-1.5 text-[11px] text-fg-muted",
                ref.status === "failed" && "text-danger",
              )}
            >
              {ref.status === "indexing" ? <LiveDot label="" /> : null}
              {refStatusLabel(ref)}
            </span>
          </span>
        </CommandItem>
      ))}
    </CommandGroup>
  );
}

export function RepoHeaderSkeleton() {
  return (
    <div className="flex flex-col gap-2 px-6 pt-5" aria-hidden>
      <Skeleton className="h-7 w-56" />
      <Skeleton className="h-5 w-80" />
    </div>
  );
}

export function RepoHeader({ repo }: { repo: Repo }) {
  return (
    <header className="flex flex-wrap items-center gap-x-3 gap-y-2 px-6 pt-5">
      <h1 className="text-xl font-semibold tracking-[-0.01em]">{repo.name}</h1>
      <Badge variant="outline">{repo.visibility}</Badge>
      <RefSwitcher defaultBranch={repo.default_branch} />
      <RepoStatus repo={repo} showLabel className="rounded-full border border-line px-2 py-0.5" />
      <a
        href={`https://github.com/${repo.full_name}`}
        target="_blank"
        rel="noreferrer"
        className="ml-auto inline-flex items-center gap-1 text-sm text-fg-muted hover:text-fg"
      >
        {repo.full_name}
        <ExternalLink aria-hidden className="size-3.5" />
        <span className="sr-only">(opens GitHub in a new tab)</span>
      </a>
    </header>
  );
}
