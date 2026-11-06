"use client";

/**
 * The staff evals dashboard (10.6.2): stored eval runs, newest first or with the most
 * regressions first, each with its kind, headline score and how many metrics got worse than its
 * baseline (the run before it of the same suite, version and retriever). Filtered by suite, in
 * the address. Anyone who isn't staff gets the not-found page, as the API answers them.
 */
import Link from "next/link";
import { notFound } from "next/navigation";
import { parseAsString, parseAsStringLiteral, useQueryState } from "nuqs";

import { ErrorState } from "@/components/feedback/error-state";
import { Badge } from "@/components/ui/badge";
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
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useMe } from "@/hooks/use-me";
import { $api } from "@/lib/api/client";
import {
  formatMetric,
  headline,
  KIND_LABELS,
  runKind,
  type EvalSummary,
} from "@/lib/internal/evals";

const WHEN = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });
const SORTS = ["newest", "regressions"] as const;
const ALL = "__all__";

export function StaffOnly({ children }: { children: React.ReactNode }) {
  const me = useMe();
  if (me.isError)
    return <ErrorState title="Couldn't load your account" onRetry={() => void me.refetch()} />;
  if (!me.data) return <Skeleton className="h-40" role="status" aria-label="Loading" />;
  if (!me.data.is_staff) notFound();
  return <>{children}</>;
}

export function EvalsList() {
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-8">
      <StaffOnly>
        <Runs />
      </StaffOnly>
    </div>
  );
}

function Runs() {
  const [suite, setSuite] = useQueryState("suite", parseAsString);
  const [sort, setSort] = useQueryState("sort", parseAsStringLiteral(SORTS).withDefault("newest"));
  const runs = $api.useQuery("get", "/api/v1/internal/evals", {
    params: { query: { ...(suite ? { suite } : {}), sort, limit: 100 } },
  });
  // The suites to filter by: what the unfiltered list has.
  const all = $api.useQuery("get", "/api/v1/internal/evals", { params: { query: { limit: 100 } } });
  const suites = [...new Set((all.data?.items ?? []).map((r) => r.suite))].sort();
  return (
    <>
      <div className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">Evals</h1>
        <p className="text-sm text-fg-muted">
          Each run against its baseline: the run before it of the same suite, version and retriever.
          Cost, time and errors count as worse when they rise.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={suite ?? ALL}
          onValueChange={(value) => {
            void setSuite(value === ALL || value === null ? null : value);
          }}
        >
          <SelectTrigger aria-label="Suite" size="sm" className="w-64">
            <SelectValue>{suite ?? "Every suite"}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Every suite</SelectItem>
            {suites.map((name) => (
              <SelectItem key={name} value={name}>
                {name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <ToggleGroup
          aria-label="Order"
          value={[sort]}
          onValueChange={(value: string[]) => {
            const next = value[0];
            if (next === "newest" || next === "regressions")
              void setSort(next === "newest" ? null : next);
          }}
        >
          <ToggleGroupItem value="newest" size="sm">
            Newest
          </ToggleGroupItem>
          <ToggleGroupItem value="regressions" size="sm">
            Most regressions
          </ToggleGroupItem>
        </ToggleGroup>
      </div>
      {runs.isError ? (
        <ErrorState title="Runs didn't load" onRetry={() => void runs.refetch()} />
      ) : !runs.data ? (
        <Skeleton className="h-64" role="status" aria-label="Loading runs" />
      ) : runs.data.items.length === 0 ? (
        <p className="text-sm text-fg-muted">
          No runs stored yet. <code className="font-mono">sensei-eval run</code> stores each one.
        </p>
      ) : (
        <Table aria-label="Eval runs">
          <TableHeader>
            <TableRow>
              <TableHead>Run</TableHead>
              <TableHead>Kind</TableHead>
              <TableHead>Suite</TableHead>
              <TableHead>Retriever</TableHead>
              <TableHead className="text-right">Score</TableHead>
              <TableHead>Against its baseline</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.data.items.map((run) => (
              <RunRow key={run.id} run={run} />
            ))}
          </TableBody>
        </Table>
      )}
    </>
  );
}

function RunRow({ run }: { run: EvalSummary }) {
  const { metric, value } = headline(run);
  return (
    <TableRow data-testid="eval-run">
      <TableCell>
        <Link href={`/internal/evals/${run.id}`} className="hover:underline">
          {WHEN.format(new Date(run.created_at))}
        </Link>
      </TableCell>
      <TableCell>{KIND_LABELS[runKind(run)]}</TableCell>
      <TableCell className="font-mono text-xs">
        {run.suite} v{run.suite_version}
      </TableCell>
      <TableCell className="font-mono text-xs">{run.retriever}</TableCell>
      <TableCell className="text-right tabular-nums">
        <span className="text-fg-muted">{metric} </span>
        {formatMetric(metric, value)}
      </TableCell>
      <TableCell>
        {run.regressions === null || run.regressions === undefined ? (
          <span className="text-sm text-fg-faint">No baseline</span>
        ) : run.regressions > 0 ? (
          <Badge variant="destructive">
            {run.regressions} {run.regressions === 1 ? "regression" : "regressions"}
          </Badge>
        ) : (
          <span className="text-sm text-fg-muted">No regressions</span>
        )}
      </TableCell>
    </TableRow>
  );
}
