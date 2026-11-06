"use client";

/**
 * One eval run against its baseline (10.6.2): its overall metrics beside the baseline's (worse
 * marked, the right way round), the items that got worse or better on the run's item score, and
 * every item: for retrieval what it found and the gold it missed, for a CrossLayerBench flow the
 * traced path hop by hop (each matched to a gold hop or not) and the gold hops it never reached,
 * for FixBench each bug's verdict.
 */
import Link from "next/link";
import { useParams } from "next/navigation";

import { ErrorState } from "@/components/feedback/error-state";
import { StaffOnly } from "@/components/internal/evals-list";
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
import {
  formatDelta,
  formatMetric,
  itemMetric,
  KIND_LABELS,
  runKind,
  type EvalComparison,
  type EvalRun,
  type MetricChange,
  type RunKind,
} from "@/lib/internal/evals";

const WHEN = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });

interface Item {
  id: string;
  query: string;
  query_class?: string;
  abstained?: boolean;
  metrics: Record<string, number | null>;
  hits: Record<string, unknown>[];
  missed: Record<string, unknown>[];
  answer?: string | null;
  error?: string | null;
}

/** The run in the address, or `run` (stories). */
export function EvalRunPage({ run }: { run?: string }) {
  const params = useParams<{ run?: string }>();
  const id = run ?? params.run ?? "";
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-8 p-8">
      <StaffOnly>
        <RunView run={id} />
      </StaffOnly>
    </div>
  );
}

function RunView({ run }: { run: string }) {
  const params = { params: { path: { run } } };
  const found = $api.useQuery("get", "/api/v1/internal/evals/{run}", params);
  const compared = $api.useQuery("get", "/api/v1/internal/evals/{run}/compare", params);
  if (found.isError || compared.isError) {
    return (
      <ErrorState
        title="The run didn't load"
        onRetry={() => {
          void found.refetch();
          void compared.refetch();
        }}
      />
    );
  }
  if (!found.data || !compared.data) {
    return <Skeleton className="h-64" role="status" aria-label="Loading the run" />;
  }
  const data = found.data;
  const kind = runKind(data);
  return (
    <>
      <div className="flex flex-col gap-2">
        <Link href="/internal" className="text-sm text-fg-muted hover:text-fg">
          ← Every run
        </Link>
        <h1 className="text-xl font-semibold">
          {KIND_LABELS[kind]}: <span className="font-mono">{data.suite}</span> v{data.suite_version}
          , <span className="font-mono">{data.retriever}</span>
        </h1>
        <p className="text-sm text-fg-muted">
          {WHEN.format(new Date(data.created_at))}
          {data.repo_sha && (
            <>
              {" "}
              · at <span className="font-mono">{data.repo_sha.slice(0, 12)}</span>
            </>
          )}
          {data.git_sha && (
            <>
              {" "}
              · code <span className="font-mono">{data.git_sha.slice(0, 12)}</span>
            </>
          )}
        </p>
      </div>
      <Comparison comparison={compared.data} />
      <Items run={data} kind={kind} comparison={compared.data} />
    </>
  );
}

function Comparison({ comparison }: { comparison: EvalComparison }) {
  const base = comparison.baseline;
  if (!base) {
    return (
      <p className="text-sm text-fg-muted">
        No baseline: the first run of this suite, version and retriever.
      </p>
    );
  }
  const rows: { change: MetricChange; worse: boolean; better: boolean }[] = [
    ...comparison.regressions.map((change) => ({ change, worse: true, better: false })),
    ...comparison.improvements.map((change) => ({ change, worse: false, better: true })),
    ...comparison.unchanged.map((change) => ({ change, worse: false, better: false })),
  ];
  return (
    <section className="flex flex-col gap-3" aria-labelledby="eval-compare">
      <h2 id="eval-compare" className="font-medium">
        Against its baseline
      </h2>
      <p className="text-sm text-fg-muted">
        The run of{" "}
        <Link href={`/internal/evals/${base.id}`} className="hover:underline">
          {WHEN.format(new Date(base.created_at))}
        </Link>
        .
      </p>
      {comparison.warnings.length > 0 && (
        <ul className="flex flex-col gap-1 text-sm text-warn" aria-label="Warnings">
          {comparison.warnings.map((warning) => (
            <li key={warning}>⚠ {warning}</li>
          ))}
        </ul>
      )}
      <Table aria-label="Metrics against the baseline">
        <TableHeader>
          <TableRow>
            <TableHead>Metric</TableHead>
            <TableHead className="text-right">Baseline</TableHead>
            <TableHead className="text-right">This run</TableHead>
            <TableHead className="text-right">Change</TableHead>
            <TableHead>
              <span className="sr-only">Worse or better</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map(({ change, worse, better }) => (
            <TableRow key={change.name}>
              <TableCell className="font-mono text-xs">{change.name}</TableCell>
              <TableCell className="text-right tabular-nums">
                {formatMetric(change.name, change.before)}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {formatMetric(change.name, change.after)}
              </TableCell>
              <TableCell className="text-right tabular-nums">{formatDelta(change)}</TableCell>
              <TableCell>
                {worse ? (
                  <Badge variant="destructive">Worse</Badge>
                ) : better ? (
                  <Badge variant="secondary">Better</Badge>
                ) : null}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}

function Items({
  run,
  kind,
  comparison,
}: {
  run: EvalRun;
  kind: RunKind;
  comparison: EvalComparison;
}) {
  // The API's: runs at other depths compare on the smaller one both have.
  const metric = comparison.metric ?? itemMetric(run);
  const items = run.per_item as unknown as Item[];
  const worse = new Map(comparison.items_worse.map((c) => [c.id, c]));
  const better = new Map(comparison.items_better.map((c) => [c.id, c]));
  // Worse first, then better, then the rest, each in the run's order.
  const order = (item: Item) => (worse.has(item.id) ? 0 : better.has(item.id) ? 1 : 2);
  const sorted = [...items].sort((a, b) => order(a) - order(b));
  return (
    <section className="flex flex-col gap-3" aria-labelledby="eval-items">
      <h2 id="eval-items" className="font-medium">
        Items <span className="text-sm font-normal text-fg-muted">on {metric}</span>
      </h2>
      <ul className="flex flex-col gap-2">
        {sorted.map((item) => {
          const moved = worse.get(item.id) ?? better.get(item.id);
          return (
            <li key={item.id}>
              <details className="rounded-lg border border-line" data-testid="eval-item">
                <summary className="flex cursor-pointer flex-wrap items-center gap-3 px-4 py-2">
                  <span className="font-mono text-xs">{item.id}</span>
                  <span className="min-w-0 flex-1 truncate text-sm">{item.query}</span>
                  <span className="text-sm tabular-nums">
                    {moved && (
                      <span className="text-fg-muted">{formatMetric(metric, moved.before)} → </span>
                    )}
                    {formatMetric(metric, item.metrics[metric] ?? null)}
                  </span>
                  {worse.has(item.id) && <Badge variant="destructive">Worse</Badge>}
                  {better.has(item.id) && <Badge variant="secondary">Better</Badge>}
                </summary>
                <div className="border-t border-line px-4 py-3 text-sm">
                  {kind === "bench" ? (
                    <PathView item={item} />
                  ) : kind === "fixbench" ? (
                    <Verdict item={item} />
                  ) : (
                    <Found item={item} />
                  )}
                </div>
              </details>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

const text = (value: unknown) => (typeof value === "string" ? value : "");
const lines = (value: unknown) =>
  Array.isArray(value) && value.length === 2 ? `:${String(value[0])}-${String(value[1])}` : "";

/** Retrieval and answers: what it found, and the gold it didn't. */
function Found({ item }: { item: Item }) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <div className="flex flex-col gap-1">
        <h3 className="text-xs font-medium tracking-wide text-fg-muted uppercase">Found</h3>
        {item.hits.length === 0 ? (
          <p className="text-fg-faint">Nothing.</p>
        ) : (
          <ol className="flex flex-col gap-0.5 font-mono text-xs">
            {item.hits.map((hit, n) => (
              <li key={n}>
                {text(hit.path)}
                {lines(hit.lines)}
                {text(hit.symbol) && <span className="text-fg-muted"> {text(hit.symbol)}</span>}
              </li>
            ))}
          </ol>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <h3 className="text-xs font-medium tracking-wide text-fg-muted uppercase">
          Gold it missed
        </h3>
        {item.missed.length === 0 ? (
          <p className="text-fg-faint">None: every gold target was found.</p>
        ) : (
          <ul className="flex flex-col gap-0.5 font-mono text-xs">
            {item.missed.map((gold, n) => (
              <li key={n}>
                <span className="text-fg-muted">{text(gold.layer)} </span>
                {text(gold.path)}
                {text(gold.symbol) && ` ${text(gold.symbol)}`}
              </li>
            ))}
          </ul>
        )}
      </div>
      {item.answer && (
        <div className="flex flex-col gap-1 md:col-span-2">
          <h3 className="text-xs font-medium tracking-wide text-fg-muted uppercase">Answer</h3>
          <p className="whitespace-pre-wrap">{item.answer}</p>
        </div>
      )}
      {item.error && <p className="text-danger md:col-span-2">{item.error}</p>}
    </div>
  );
}

/** A CrossLayerBench flow: the traced path, each hop matched to a gold hop or not. */
function PathView({ item }: { item: Item }) {
  return (
    <div className="flex flex-col gap-3">
      <ol className="flex flex-wrap items-stretch gap-2" aria-label="The traced path">
        {item.hits.map((hop, n) => {
          const matched = typeof hop.gold === "number";
          return (
            <li key={n} className="flex items-center gap-2">
              {n > 0 && (
                <span aria-hidden className="text-fg-faint">
                  →
                </span>
              )}
              <div
                className={`flex flex-col rounded-md border px-2 py-1 ${matched ? "border-chart-1" : "border-dashed border-line"}`}
              >
                <span className="text-xs text-fg-muted">
                  {text(hop.layer)} ·{" "}
                  {matched ? `gold hop ${String(Number(hop.gold) + 1)}` : "not in the gold"}
                </span>
                <span className="font-mono text-xs">{text(hop.name)}</span>
                <span className="font-mono text-[11px] text-fg-faint">
                  {text(hop.path)}
                  {lines(hop.lines)}
                </span>
              </div>
            </li>
          );
        })}
      </ol>
      {item.hits.length === 0 && <p className="text-fg-faint">No path traced.</p>}
      <div className="flex flex-col gap-1">
        <h3 className="text-xs font-medium tracking-wide text-fg-muted uppercase">
          Gold hops it never reached
        </h3>
        {item.missed.length === 0 ? (
          <p className="text-fg-faint">None.</p>
        ) : (
          <ul className="flex flex-col gap-0.5 font-mono text-xs">
            {item.missed.map((gold, n) => (
              <li key={n}>
                <span className="text-fg-muted">{text(gold.layer)} </span>
                {text(gold.path)}
                {text(gold.symbol) && ` ${text(gold.symbol)}`}
              </li>
            ))}
          </ul>
        )}
      </div>
      {item.error && <p className="text-danger">{item.error}</p>}
    </div>
  );
}

/** A FixBench bug: whether it was fixed, and if not why. */
function Verdict({ item }: { item: Item }) {
  const fixed = item.metrics.fixed === 1;
  const unsafe = item.metrics.unsafe === 1;
  return (
    <div className="flex flex-col gap-1">
      <p>
        {fixed ? (
          <Badge variant="secondary">Fixed</Badge>
        ) : unsafe ? (
          <Badge variant="destructive">Unsafe</Badge>
        ) : (
          <Badge variant="outline">Not fixed</Badge>
        )}{" "}
        <span className="text-fg-muted">{item.query}</span>
      </p>
      {item.error && <p className="text-fg-muted">{item.error}</p>}
    </div>
  );
}
