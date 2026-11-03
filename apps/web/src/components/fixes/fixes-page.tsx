"use client";

/**
 * `/[org]/[repo]/fixes` (8.11): the repository's fixes, newest first: what was reported, where
 * it came from, the stage it is at and how that's going, its risk, its pull request and who
 * asked. Filtered by status (the API's), source and risk (kept in the address).
 */
import { ExternalLink, Plus, Wrench } from "lucide-react";
import Link from "next/link";
import { parseAsStringLiteral, useQueryState } from "nuqs";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { LockedState } from "@/components/feedback/locked-state";
import { RelativeTime } from "@/components/primitives/relative-time";
import { useRepoFeatures, useRepoSlug } from "@/components/repo/use-repo";
import { buttonVariants } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import {
  RISK_LABELS,
  SOURCE_LABELS,
  STAGE_LABELS,
  STATUS_FILTERS,
  STATUS_LABELS as STATUS_NAMES,
  type FixRisk,
  type FixSource,
  type FixSummary,
  prLink,
} from "@/lib/api/fixes";
import { STAGE_LABELS as INDEX_STAGES } from "@/lib/indexing";
import { repoPath } from "@/lib/routes";

import { RiskBadge, StatusBadge } from "./fix-badges";
import { useFixes } from "./use-fixes";

const SOURCES = ["paste", "github_issue", "sentry"] as const satisfies readonly FixSource[];
const RISKS = ["low", "medium", "high"] as const satisfies readonly FixRisk[];
const ALL = "all";

export function FixesPage() {
  const { org, repo } = useRepoSlug();
  const { state, ready, active } = useRepoFeatures();
  if (!ready) return null;
  const fixes = state("fixes");
  if (!fixes.available) {
    const stage = fixes.stage ? INDEX_STAGES[fixes.stage] : "indexing";
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col p-8">
        {active && fixes.coming ? (
          <LockedState
            feature="Fixes"
            stage={stage}
            {...(fixes.etaSeconds !== null ? { etaSeconds: fixes.etaSeconds } : {})}
            href={repoPath(org, repo)}
          />
        ) : (
          <EmptyState
            icon={<Wrench aria-hidden />}
            title="Fixes open after indexing"
            description="A fix investigates the indexed code, so the repository is indexed first."
          />
        )}
      </div>
    );
  }
  return <Fixes />;
}

function NewFixButton() {
  const { org, repo } = useRepoSlug();
  const { role } = useOrgRole();
  if (!roleAtLeast(role, "member")) {
    return (
      <p className="text-xs text-fg-muted" data-testid="viewer-note">
        Viewers can follow fixes; members start them.
      </p>
    );
  }
  return (
    <Link href={repoPath(org, repo, "fixes/new")} className={buttonVariants({ size: "sm" })}>
      <Plus aria-hidden />
      New fix
    </Link>
  );
}

function Fixes() {
  const [status, setStatus] = useQueryState(
    "status",
    parseAsStringLiteral(STATUS_FILTERS).withOptions({ history: "replace" }),
  );
  const [source, setSource] = useQueryState(
    "source",
    parseAsStringLiteral(SOURCES).withOptions({ history: "replace" }),
  );
  const [risk, setRisk] = useQueryState(
    "risk",
    parseAsStringLiteral(RISKS).withOptions({ history: "replace" }),
  );
  const list = useFixes(status);
  const shown = (list.data ?? []).filter(
    (fix) => (!source || fix.source === source) && (!risk || fix.risk === risk),
  );
  const filtered = status !== null || source !== null || risk !== null;
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-6">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">Fixes</h1>
        <div className="ml-auto">
          <NewFixButton />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filters">
        <Filter
          label="Status"
          value={status}
          options={STATUS_FILTERS.map((value) => ({ value, label: STATUS_NAMES[value] }))}
          onChange={(value) => void setStatus(value)}
        />
        <Filter
          label="Source"
          value={source}
          options={SOURCES.map((value) => ({ value, label: SOURCE_LABELS[value] }))}
          onChange={(value) => void setSource(value)}
        />
        <Filter
          label="Risk"
          value={risk}
          options={RISKS.map((value) => ({ value, label: RISK_LABELS[value] }))}
          onChange={(value) => void setRisk(value)}
        />
      </div>
      {list.isPending ? (
        <div role="status" aria-label="Loading the fixes" className="flex flex-col gap-2">
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
        </div>
      ) : list.isError ? (
        <ErrorState title="Couldn't load the fixes" onRetry={() => void list.refetch()} />
      ) : shown.length === 0 ? (
        <EmptyState
          icon={<Wrench aria-hidden />}
          title={filtered ? "No fixes match" : "No fixes yet"}
          description={
            filtered
              ? "Try another status, source or risk."
              : "Paste a stack trace or pick a GitHub issue: a fix reproduces the bug before anything is changed."
          }
        />
      ) : (
        <FixTable fixes={shown} />
      )}
    </div>
  );
}

function Filter<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T | null;
  options: { value: T; label: string }[];
  onChange: (value: T | null) => void;
}) {
  const items = [{ value: ALL, label: `Any ${label.toLowerCase()}` }, ...options];
  const shown = items.find((item) => item.value === (value ?? ALL))?.label;
  return (
    <Select
      value={value ?? ALL}
      onValueChange={(next) => {
        onChange(next === ALL || next === null ? null : (next as T));
      }}
    >
      <SelectTrigger size="sm" aria-label={label} className="min-w-36">
        <SelectValue>{shown}</SelectValue>
      </SelectTrigger>
      <SelectContent>
        {items.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            {item.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function FixTable({ fixes }: { fixes: FixSummary[] }) {
  const { org, repo } = useRepoSlug();
  return (
    <Table data-testid="fixes">
      <TableHeader>
        <TableRow>
          <TableHead>Issue</TableHead>
          <TableHead>Source</TableHead>
          <TableHead>Stage</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Risk</TableHead>
          <TableHead>PR</TableHead>
          <TableHead>Asked by</TableHead>
          <TableHead>When</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {fixes.map((fix) => (
          <TableRow key={fix.id} data-testid="fix-row">
            <TableCell className="max-w-md">
              <Link
                href={repoPath(org, repo, `fixes/${fix.id}`)}
                className="line-clamp-1 hover:underline"
              >
                {fix.title}
              </Link>
            </TableCell>
            <TableCell className="text-xs text-fg-muted">{SOURCE_LABELS[fix.source]}</TableCell>
            <TableCell className="text-xs">{STAGE_LABELS[fix.stage]}</TableCell>
            <TableCell>
              <StatusBadge status={fix.status} />
            </TableCell>
            <TableCell>
              {fix.risk ? (
                <RiskBadge risk={fix.risk} />
              ) : (
                <span className="text-xs text-fg-faint">Not yet</span>
              )}
            </TableCell>
            <TableCell>
              {prLink(fix.pr_url) ? (
                <a
                  href={prLink(fix.pr_url) ?? undefined}
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
              {fix.author ? `@${fix.author}` : "Sentry"}
            </TableCell>
            <TableCell>
              <RelativeTime date={fix.created_at} className="text-xs text-fg-faint" />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
