"use client";

/**
 * `/[org]/[repo]/runs` (4.12): the repository's agent runs, the latest first: what was asked,
 * how it went, what it cost and took, who asked. Each opens its timeline.
 */
import { Activity } from "lucide-react";
import Link from "next/link";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { RelativeTime } from "@/components/primitives/relative-time";
import { useRepoSlug } from "@/components/repo/use-repo";
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
import { formatMs } from "@/lib/api/runs";
import { repoPath } from "@/lib/routes";

import { useRuns } from "./use-runs";

export function RunsList() {
  const { org, repo } = useRepoSlug();
  const runs = useRuns();
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4 p-6">
      <h1 className="text-lg font-semibold">Runs</h1>
      {runs.isPending ? (
        <Skeleton className="h-40" />
      ) : runs.isError ? (
        <ErrorState title="Runs couldn't be loaded" description={runs.error.message} />
      ) : runs.data.runs.length === 0 ? (
        <EmptyState
          icon={<Activity aria-hidden />}
          title="No runs yet"
          description="Every question asked in Ask is a run: its steps, time and cost show here."
        />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Question</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Cost</TableHead>
              <TableHead className="text-right">Took</TableHead>
              <TableHead>Asked by</TableHead>
              <TableHead>When</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.data.runs.map((run) => (
              <TableRow key={run.id}>
                <TableCell className="max-w-md">
                  <Link
                    href={repoPath(org, repo, `runs/${run.id}`)}
                    className="line-clamp-1 hover:underline"
                  >
                    {run.question}
                  </Link>
                  <span className="text-xs text-fg-muted">
                    {run.kind} · {run.mode}
                  </span>
                </TableCell>
                <TableCell>
                  <Badge variant={run.status === "failed" ? "destructive" : "secondary"}>
                    {run.finished_at === null ? "running" : run.status}
                  </Badge>
                </TableCell>
                <TableCell className="text-right font-mono text-xs">
                  ${Number(run.cost_usd).toFixed(4)}
                </TableCell>
                <TableCell className="text-right font-mono text-xs">
                  {run.latency_ms !== null ? formatMs(run.latency_ms) : "…"}
                </TableCell>
                <TableCell className="text-xs">{run.user ?? "—"}</TableCell>
                <TableCell className="text-xs text-fg-muted">
                  <RelativeTime date={run.created_at} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
