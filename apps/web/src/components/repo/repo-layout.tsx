"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { useOrgSlug } from "@/components/shell/use-org-slug";
import { ApiError } from "@/lib/api/errors";
import { orgPath } from "@/lib/routes";

import { RepoHeader, RepoHeaderSkeleton } from "./repo-header";
import { RepoTabs } from "./repo-tabs";
import { useRepo } from "./use-repo";

/** The frame of every repository page: header, tabs, then the tab's content. (Visited tabs keep
 * their state: with Cache Components, Next keeps them mounted in a hidden `<Activity>`.) */
export function RepoLayout({ children }: { children: ReactNode }) {
  const org = useOrgSlug();
  const repo = useRepo();
  if (repo.isError) {
    const error = repo.error as unknown;
    return (
      <div className="p-8">
        <ErrorState
          title={
            error instanceof ApiError && error.status === 404
              ? "Repository not found"
              : "Couldn't load the repository"
          }
          description={
            error instanceof ApiError && error.status === 404
              ? "It doesn't exist, or you can't read it on GitHub."
              : error instanceof ApiError
                ? error.message
                : undefined
          }
          onRetry={() => void repo.refetch()}
          action={
            <Link href={orgPath(org, "repos")} className="text-sm underline underline-offset-4">
              All repositories
            </Link>
          }
        />
      </div>
    );
  }
  return (
    <div className="flex min-h-full flex-col">
      {repo.data ? <RepoHeader repo={repo.data} /> : <RepoHeaderSkeleton />}
      <div className="mt-3">
        <RepoTabs />
      </div>
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  );
}
