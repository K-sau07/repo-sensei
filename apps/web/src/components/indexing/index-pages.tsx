"use client";

import { cn } from "cn";
import { parseAsString, useQueryState } from "nuqs";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { RepoReady } from "@/components/overview/repo-ready";
import { RelativeTime } from "@/components/primitives/relative-time";
import { useRepo, useRepoSlug } from "@/components/repo/use-repo";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { IndexJob } from "@/lib/api/types";
import { STAGE_LABELS, took } from "@/lib/indexing";

import { IndexView } from "./index-view";

const STATUS_BADGE: Record<IndexJob["status"], { label: string; className: string }> = {
  queued: { label: "Queued", className: "text-fg-muted" },
  running: { label: "Running", className: "text-signal" },
  succeeded: { label: "Succeeded", className: "text-ok" },
  failed: { label: "Failed", className: "text-danger" },
  cancelled: { label: "Cancelled", className: "text-fg-muted" },
};

function useJobs() {
  const { org, repo } = useRepoSlug();
  return $api.useQuery("get", "/api/v1/orgs/{org}/repos/{repo}/index-jobs", {
    params: { path: { org, repo } },
  });
}

/** `/[org]/[repo]`: the live indexing view while a run is on (or after one failed); otherwise
 * what the last index found. */
export function RepoOverview() {
  const { org, repo } = useRepoSlug();
  const detail = useRepo();
  const jobs = useJobs();
  if (jobs.isError) {
    return (
      <div className="mx-auto w-full max-w-3xl p-6">
        <ErrorState
          title="Couldn't load this repository's index runs"
          description={jobs.error instanceof ApiError ? jobs.error.message : undefined}
          onRetry={() => void jobs.refetch()}
        />
      </div>
    );
  }
  if (!detail.data || jobs.isPending) {
    return (
      <div className="p-6" role="status" aria-label="Loading the overview">
        <Skeleton className="h-64" />
      </div>
    );
  }
  const latest = jobs.data[0];
  // The live view while a run is on, or after one failed (to show why, with Retry), or until
  // the repository has an index at all; otherwise the summary of the last index. A cancelled
  // run leaves the last index as it was.
  const showRun =
    latest !== undefined &&
    (latest.status === "queued" ||
      latest.status === "running" ||
      latest.status === "failed" ||
      !detail.data.last_indexed_sha);
  if (showRun) {
    return (
      <div className="mx-auto w-full max-w-6xl p-6">
        <IndexView org={org} repo={repo} jobId={latest.id} />
      </div>
    );
  }
  const data = detail.data;
  if (!latest) {
    return (
      <div className="mx-auto w-full max-w-3xl p-6">
        <EmptyState
          title="Not indexed yet"
          description="Indexing starts when a repository is connected, or from the Index tab."
        />
      </div>
    );
  }
  return <RepoReady org={org} repo={repo} data={data} latest={latest} />;
}

/** `/[org]/[repo]/index`: every run, newest first, and the one picked (`?job=`) in full. */
export function IndexHistory() {
  const { org, repo } = useRepoSlug();
  const jobs = useJobs();
  const [picked, setPicked] = useQueryState("job", parseAsString);
  if (jobs.isError) {
    return (
      <div className="mx-auto w-full max-w-3xl p-6">
        <ErrorState
          title="Couldn't load the index runs"
          description={jobs.error instanceof ApiError ? jobs.error.message : undefined}
          onRetry={() => void jobs.refetch()}
        />
      </div>
    );
  }
  if (jobs.isPending) {
    return (
      <div className="p-6" role="status" aria-label="Loading index runs">
        <Skeleton className="h-48" />
      </div>
    );
  }
  const list = jobs.data;
  if (list.length === 0) {
    return (
      <div className="mx-auto w-full max-w-3xl p-6">
        <EmptyState title="No index runs yet" />
      </div>
    );
  }
  const selected = list.find((job) => job.id === picked) ?? list[0];
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-6">
      <section aria-labelledby="index-runs" className="flex flex-col gap-2">
        <h2 id="index-runs" className="text-sm font-medium">
          Index runs
        </h2>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Status</TableHead>
              <TableHead>Kind</TableHead>
              <TableHead>Stage</TableHead>
              <TableHead>Started</TableHead>
              <TableHead className="text-right">Took</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.map((job) => {
              const badge = STATUS_BADGE[job.status];
              const current = job.id === selected?.id;
              return (
                <TableRow
                  key={job.id}
                  aria-selected={current}
                  className={cn(current && "bg-surface-2")}
                >
                  <TableCell>
                    <button
                      type="button"
                      onClick={() => void setPicked(job.id)}
                      className="text-left underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                    >
                      <Badge variant="outline" className={badge.className}>
                        {badge.label}
                      </Badge>
                      <span className="sr-only"> run {job.id}: show it</span>
                    </button>
                  </TableCell>
                  <TableCell className="text-fg-muted">{job.kind}</TableCell>
                  <TableCell className="text-fg-muted">
                    {job.stage ? STAGE_LABELS[job.stage] : "–"}
                  </TableCell>
                  <TableCell className="text-fg-muted">
                    <RelativeTime date={job.created_at} />
                  </TableCell>
                  <TableCell className="text-right text-fg-muted tabular-nums">
                    {took(job)}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </section>
      {selected && <IndexView key={selected.id} org={org} repo={repo} jobId={selected.id} />}
    </div>
  );
}
