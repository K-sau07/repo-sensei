"use client";

import { ExternalLink, GitBranch } from "lucide-react";

import { RepoStatus } from "@/components/repos/repo-status";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Repo } from "@/lib/api/types";

/** The branch the repository is indexed at. Other branches arrive with freshness (P5), so for
 * now it only names the default one. */
export function RefSwitcher({ branch }: { branch: string }) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            aria-disabled="true"
            aria-label={`Branch: ${branch}. Other branches come in a later release.`}
            className="inline-flex h-control-sm items-center gap-1.5 rounded-sm border border-line px-2 text-xs text-fg-muted"
          />
        }
      >
        <GitBranch aria-hidden className="size-3.5" />
        {branch}
      </TooltipTrigger>
      <TooltipContent>Branches in a later phase</TooltipContent>
    </Tooltip>
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
      <RefSwitcher branch={repo.default_branch} />
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
