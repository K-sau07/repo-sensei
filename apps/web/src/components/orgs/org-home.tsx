"use client";

import { FolderGit2 } from "lucide-react";
import Link from "next/link";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { LanguageBar } from "@/components/repos/language-bar";
import { RepoStatus } from "@/components/repos/repo-status";
import { useOrgSlug } from "@/components/shell/use-org-slug";
import { buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { orgPath, repoPath } from "@/lib/routes";

const SHOWN = 6;

export function OrgHome() {
  const slug = useOrgSlug();
  const { org, role } = useOrgRole();
  const repos = $api.useQuery("get", "/api/v1/orgs/{org}/repos", {
    params: { path: { org: slug } },
  });
  const admin = roleAtLeast(role, "admin");
  const list = repos.data ?? [];
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 p-8">
      <h1 className="text-2xl font-semibold tracking-[-0.015em]">{org?.name ?? "Home"}</h1>
      {repos.isPending && <Skeleton className="h-32" />}
      {repos.isError && (
        <ErrorState
          title="Couldn't load your repositories"
          description={repos.error instanceof ApiError ? repos.error.message : undefined}
          onRetry={() => void repos.refetch()}
        />
      )}
      {repos.isSuccess && list.length === 0 && (
        <EmptyState
          icon={<FolderGit2 />}
          title="Connect your first repository"
          description="repo-sensei indexes a repository so you can browse it, ask questions, trace requests across layers and get fixes with citations."
          action={
            admin ? (
              <Link href={orgPath(slug, "repos/new")} className={buttonVariants()}>
                Connect a repository
              </Link>
            ) : (
              <p className="text-sm text-fg-muted">
                An admin of this organization can connect one.
              </p>
            )
          }
        />
      )}
      {list.length > 0 && (
        <section aria-labelledby="home-repos" className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 id="home-repos" className="text-sm font-medium text-fg-muted">
              Your repositories
            </h2>
            <Link href={orgPath(slug, "repos")} className="text-sm underline underline-offset-4">
              All repositories
            </Link>
          </div>
          <ul className="grid gap-3 sm:grid-cols-2">
            {list.slice(0, SHOWN).map((repo) => (
              <li key={repo.id}>
                <Link
                  href={repoPath(slug, repo.slug)}
                  className="flex flex-col gap-3 rounded-lg border border-line p-4 hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate font-medium">{repo.name}</span>
                    <RepoStatus repo={repo} showLabel />
                  </span>
                  <LanguageBar languages={repo.languages} />
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
