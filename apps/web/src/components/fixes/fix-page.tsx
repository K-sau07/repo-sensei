"use client";

/**
 * `/[org]/[repo]/fixes/[fixId]`: one fix, as it stands: what was reported (the trace's frames
 * opening the code), its stage and status, and why it stopped, if it did.
 */
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { ErrorState } from "@/components/feedback/error-state";
import { RelativeTime } from "@/components/primitives/relative-time";
import { useRepoSlug } from "@/components/repo/use-repo";
import { Skeleton } from "@/components/ui/skeleton";
import { SOURCE_LABELS, STAGE_LABELS } from "@/lib/api/fixes";
import { repoPath } from "@/lib/routes";

import { RiskBadge, StatusBadge } from "./fix-badges";
import { TracePreview } from "./trace-preview";
import { useFix } from "./use-fixes";

export function FixPage() {
  const { org, repo } = useRepoSlug();
  const { fixId } = useParams<{ fixId: string }>();
  const fix = useFix(fixId);
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 p-6" data-testid="fix-page">
      <Link
        href={repoPath(org, repo, "fixes")}
        className="inline-flex items-center gap-1 text-xs text-fg-muted hover:underline"
      >
        <ArrowLeft aria-hidden className="size-3" />
        Fixes
      </Link>
      {fix.isPending ? (
        <Skeleton className="h-40" />
      ) : fix.isError ? (
        <ErrorState title="Couldn't load the fix" onRetry={() => void fix.refetch()} />
      ) : (
        <>
          <div className="flex flex-col gap-2">
            <h1 className="text-lg font-semibold">{fix.data.title}</h1>
            <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
              <StatusBadge status={fix.data.status} />
              {fix.data.risk && <RiskBadge risk={fix.data.risk} />}
              <span>At {STAGE_LABELS[fix.data.stage]}</span>
              <span>·</span>
              <span>{SOURCE_LABELS[fix.data.source]}</span>
              <span>·</span>
              <span>{fix.data.author ? `@${fix.data.author}` : "Sentry"}</span>
              <RelativeTime date={fix.data.created_at} className="text-fg-faint" />
            </div>
          </div>
          {fix.data.stop_reason && (
            <p className="rounded-md border border-line p-3 text-sm" data-testid="stop-reason">
              {fix.data.stop_reason}
            </p>
          )}
          <section aria-labelledby="report-heading" className="flex flex-col gap-2">
            <h2 id="report-heading" className="text-sm font-medium text-fg-muted">
              The report
            </h2>
            <TracePreview ticket={fix.data.ticket} />
          </section>
        </>
      )}
    </div>
  );
}
