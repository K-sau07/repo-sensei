"use client";

/**
 * `/[org]/[repo]/changes` (9.3): the repository's requested changes, newest first: what was
 * asked, the stage it is at and how that's going, its risk, its pull request and who asked.
 */
import { ExternalLink, GitPullRequestArrow, Plus } from "lucide-react";
import Link from "next/link";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { LockedState } from "@/components/feedback/locked-state";
import { RiskBadge, StatusBadge } from "@/components/fixes/fix-badges";
import { RelativeTime } from "@/components/primitives/relative-time";
import { useRepoFeatures, useRepoSlug } from "@/components/repo/use-repo";
import { buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { STAGE_LABELS } from "@/lib/api/fixes";
import { STAGE_LABELS as INDEX_STAGES } from "@/lib/indexing";
import { repoPath } from "@/lib/routes";

import { useChanges } from "./use-changes";

export function ChangesPage() {
  const { org, repo } = useRepoSlug();
  const { state, ready, active } = useRepoFeatures();
  if (!ready) return null;
  const changes = state("changes");
  if (!changes.available) {
    const stage = changes.stage ? INDEX_STAGES[changes.stage] : "indexing";
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col p-8">
        {active && changes.coming ? (
          <LockedState
            feature="Changes"
            stage={stage}
            {...(changes.etaSeconds !== null ? { etaSeconds: changes.etaSeconds } : {})}
            href={repoPath(org, repo)}
          />
        ) : (
          <EmptyState
            icon={<GitPullRequestArrow aria-hidden />}
            title="Changes open after indexing"
            description="A change is planned from the indexed code, so the repository is indexed first."
          />
        )}
      </div>
    );
  }
  return <Changes />;
}

function Changes() {
  const { org, repo } = useRepoSlug();
  const { role } = useOrgRole();
  const list = useChanges();
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-6">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">Changes</h1>
        <div className="ml-auto">
          {roleAtLeast(role, "member") ? (
            <Link
              href={repoPath(org, repo, "changes/new")}
              className={buttonVariants({ size: "sm" })}
            >
              <Plus aria-hidden />
              New change
            </Link>
          ) : (
            <p className="text-xs text-fg-muted" data-testid="viewer-note">
              Viewers can follow changes; members request them.
            </p>
          )}
        </div>
      </div>
      {list.isPending ? (
        <div role="status" aria-label="Loading the changes" className="flex flex-col gap-2">
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
        </div>
      ) : list.isError ? (
        <ErrorState title="Couldn't load the changes" onRetry={() => void list.refetch()} />
      ) : list.data.length === 0 ? (
        <EmptyState
          icon={<GitPullRequestArrow aria-hidden />}
          title="No changes yet"
          description="Describe a change: it is planned file by file for you to approve, then built with its tests."
        />
      ) : (
        <Table data-testid="changes">
          <TableHeader>
            <TableRow>
              <TableHead>Change</TableHead>
              <TableHead>Stage</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Risk</TableHead>
              <TableHead>PR</TableHead>
              <TableHead>Asked by</TableHead>
              <TableHead>When</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.data.map((change) => (
              <TableRow key={change.id} data-testid="change-row">
                <TableCell className="max-w-md">
                  <Link
                    href={repoPath(org, repo, `changes/${change.id}`)}
                    className="line-clamp-1 hover:underline"
                  >
                    {change.title}
                  </Link>
                </TableCell>
                <TableCell className="text-xs">{STAGE_LABELS[change.stage]}</TableCell>
                <TableCell>
                  <StatusBadge status={change.status} />
                </TableCell>
                <TableCell>
                  {change.risk ? (
                    <RiskBadge risk={change.risk} />
                  ) : (
                    <span className="text-xs text-fg-faint">Not yet</span>
                  )}
                </TableCell>
                <TableCell>
                  {change.pr_url ? (
                    <a
                      href={change.pr_url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-xs hover:underline"
                    >
                      Draft PR
                      <ExternalLink aria-hidden className="size-3" />
                      <span className="sr-only"> (opens GitHub)</span>
                    </a>
                  ) : (
                    <span className="text-xs text-fg-faint">—</span>
                  )}
                </TableCell>
                <TableCell className="text-xs text-fg-muted">
                  {change.author ? `@${change.author}` : "—"}
                </TableCell>
                <TableCell>
                  <RelativeTime date={change.created_at} className="text-xs text-fg-faint" />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
