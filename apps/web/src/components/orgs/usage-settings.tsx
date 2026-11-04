"use client";

/**
 * An org's usage (admins, 10.7.2): today's agent spend against the day's cap and this month's
 * against its budget (each a figure with its meter), the last 30 days' spend (one bar a day,
 * each with its numbers on hover and in a table), what it went on (models) and by whom, and the
 * limits each person works within. The caps and limits are the deployment's.
 */
import { ErrorState } from "@/components/feedback/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api } from "@/lib/api/client";
import type { Schemas } from "@/lib/api/types";

type Usage = Schemas["UsageOut"];

const USD = new Intl.NumberFormat("en", { style: "currency", currency: "USD" });
const COUNT = new Intl.NumberFormat("en");
const DAY = new Intl.DateTimeFormat("en", { month: "short", day: "numeric", timeZone: "UTC" });
const WHEN = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });
const CHART_HEIGHT = 120;
const KIND_LABELS: Record<string, string> = {
  llm: "Models",
  embedding: "Embeddings",
  rerank: "Reranking",
  index_minutes: "Index time",
  sandbox_seconds: "Sandboxes",
};

const money = (value: string | number) => USD.format(Number(value));

export function UsageSettings() {
  const { slug, role, me } = useOrgRole();
  if (me.isError) {
    return <ErrorState title="Couldn't load the organization" onRetry={() => void me.refetch()} />;
  }
  if (role === null) return <Skeleton className="h-40" role="status" aria-label="Loading" />;
  if (!roleAtLeast(role, "admin")) {
    return (
      <p className="text-sm text-fg-muted">Only the organization&apos;s admins read its usage.</p>
    );
  }
  return <UsageView org={slug} />;
}

function UsageView({ org }: { org: string }) {
  const usage = $api.useQuery("get", "/api/v1/orgs/{org}/usage", { params: { path: { org } } });
  if (usage.isError) {
    return <ErrorState title="Usage didn't load" onRetry={() => void usage.refetch()} />;
  }
  if (!usage.data) {
    return (
      <div role="status" aria-label="Loading usage" className="flex flex-col gap-4">
        <Skeleton className="h-28" />
        <Skeleton className="h-40" />
      </div>
    );
  }
  const data = usage.data;
  return (
    <div className="flex flex-col gap-8">
      <div className="grid gap-4 sm:grid-cols-2">
        <Figure
          title="Today's agent spend"
          spent={Number(data.today.spent_usd) + Number(data.today.held_usd)}
          cap={data.today.cap_usd === null ? null : Number(data.today.cap_usd)}
          note={
            Number(data.today.held_usd) > 0
              ? `${money(data.today.held_usd)} of it set aside for runs under way.`
              : "Asks, diagrams, fixes and changes stop starting at the cap."
          }
          resets={data.today.resets_at}
        />
        <Figure
          title="This month"
          spent={Number(data.month.spent_usd)}
          cap={Number(data.month.cap_usd)}
          note="Everything metered. A summaries run that would pass the budget isn't started."
          resets={data.month.resets_at}
        />
      </div>
      <Days days={data.days} />
      <Models models={data.models} />
      <People people={data.people} />
      <Limits limits={data.limits} />
    </div>
  );
}

function Figure({
  title,
  spent,
  cap,
  note,
  resets,
}: {
  title: string;
  spent: number;
  cap: number | null;
  note: string;
  resets: string;
}) {
  const share = cap !== null && cap > 0 ? Math.min(1, spent / cap) : null;
  return (
    <section className="flex flex-col gap-2 rounded-lg border border-line p-4" aria-label={title}>
      <h2 className="text-sm text-fg-muted">{title}</h2>
      <p className="text-2xl font-semibold tabular-nums">
        {USD.format(spent)}
        {cap !== null && (
          <span className="text-base font-normal text-fg-muted"> of {USD.format(cap)}</span>
        )}
      </p>
      {share !== null && (
        <div
          role="meter"
          aria-label={`${title}, of its cap`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(share * 100)}
          className="h-1.5 overflow-hidden rounded-full bg-muted"
        >
          <div className="h-full rounded-full bg-chart-1" style={{ width: `${share * 100}%` }} />
        </div>
      )}
      <p className="text-xs text-fg-muted">{note}</p>
      <p className="text-xs text-fg-faint">Resets {WHEN.format(new Date(resets))}.</p>
    </section>
  );
}

function Days({ days }: { days: Usage["days"] }) {
  const most = Math.max(...days.map((d) => Number(d.cost_usd)), 0);
  const total = days.reduce((sum, d) => sum + Number(d.cost_usd), 0);
  return (
    <section className="flex flex-col gap-3" aria-labelledby="days-title">
      <div className="flex items-baseline justify-between gap-2">
        <h2 id="days-title" className="font-medium">
          The last 30 days
        </h2>
        <p className="text-sm text-fg-muted tabular-nums">{USD.format(total)} in all</p>
      </div>
      <div
        className="flex items-end gap-0.5 border-b border-line"
        style={{ height: CHART_HEIGHT }}
        role="img"
        aria-label={`Spend each day for the last 30 days, ${USD.format(total)} in all; the table below has each day's.`}
      >
        {days.map((d) => {
          const cost = Number(d.cost_usd);
          const height = most > 0 ? Math.max(cost > 0 ? 2 : 0, (cost / most) * CHART_HEIGHT) : 0;
          return (
            <Tooltip key={d.day}>
              <TooltipTrigger
                render={<div />}
                className="flex h-full flex-1 items-end"
                aria-hidden
                tabIndex={-1}
              >
                <div className="w-full rounded-t-sm bg-chart-1" style={{ height }} />
              </TooltipTrigger>
              <TooltipContent>
                <p className="font-medium">{DAY.format(new Date(d.day))}</p>
                <p className="tabular-nums">{money(d.cost_usd)}</p>
                {Object.entries(d.by_kind).map(([kind, value]) => (
                  <p key={kind} className="text-xs tabular-nums opacity-80">
                    {KIND_LABELS[kind] ?? kind}: {money(value)}
                  </p>
                ))}
              </TooltipContent>
            </Tooltip>
          );
        })}
      </div>
      <div className="flex justify-between text-xs text-fg-faint" aria-hidden>
        <span>{days[0] ? DAY.format(new Date(days[0].day)) : ""}</span>
        <span>{days.at(-1) ? DAY.format(new Date(days.at(-1)?.day ?? "")) : ""}</span>
      </div>
      <details className="text-sm">
        <summary className="cursor-pointer text-fg-muted hover:text-fg">
          Each day, as a table
        </summary>
        <Table aria-label="Spend each day">
          <TableHeader>
            <TableRow>
              <TableHead>Day (UTC)</TableHead>
              <TableHead className="text-right">Spend</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {[...days].reverse().map((d) => (
              <TableRow key={d.day}>
                <TableCell>{DAY.format(new Date(d.day))}</TableCell>
                <TableCell className="text-right tabular-nums">{money(d.cost_usd)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </details>
    </section>
  );
}

function Models({ models }: { models: Usage["models"] }) {
  return (
    <section className="flex flex-col gap-3" aria-labelledby="models-title">
      <h2 id="models-title" className="font-medium">
        What it went on
      </h2>
      {models.length === 0 ? (
        <p className="text-sm text-fg-faint">Nothing spent in the last 30 days.</p>
      ) : (
        <Table aria-label="Spend by model">
          <TableHeader>
            <TableRow>
              <TableHead>Model</TableHead>
              <TableHead>For</TableHead>
              <TableHead className="text-right">Calls</TableHead>
              <TableHead className="text-right">Tokens in / out</TableHead>
              <TableHead className="text-right">Spend</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {models.map((m) => (
              <TableRow key={`${m.provider}:${m.model}:${m.kind}`}>
                <TableCell className="font-mono text-xs">{m.model}</TableCell>
                <TableCell>{KIND_LABELS[m.kind] ?? m.kind}</TableCell>
                <TableCell className="text-right tabular-nums">{COUNT.format(m.calls)}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {COUNT.format(m.tokens_in)} / {COUNT.format(m.tokens_out)}
                </TableCell>
                <TableCell className="text-right tabular-nums">{money(m.cost_usd)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}

function People({ people }: { people: Usage["people"] }) {
  if (people.length === 0) return null;
  return (
    <section className="flex flex-col gap-3" aria-labelledby="people-title">
      <h2 id="people-title" className="font-medium">
        By whom
      </h2>
      <Table aria-label="Spend by person">
        <TableHeader>
          <TableRow>
            <TableHead>Who</TableHead>
            <TableHead className="text-right">Spend</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {people.map((p) => (
            <TableRow key={p.login ?? "-"}>
              <TableCell>{p.login ? `@${p.login}` : "No one (index runs, public pages)"}</TableCell>
              <TableCell className="text-right tabular-nums">{money(p.cost_usd)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}

function Limits({ limits }: { limits: Usage["limits"] }) {
  const rows: [string, string][] = [
    ["Questions", `${limits.asks_per_minute} a minute, ${COUNT.format(limits.asks_per_day)} a day`],
    ["Diagrams", `${limits.diagrams_per_minute} a minute`],
    ["Index runs started by hand", `${limits.index_runs_per_day} a day`],
    ["Imports by public URL", `${limits.public_imports_per_day} a day`],
  ];
  return (
    <section className="flex flex-col gap-3" aria-labelledby="limits-title">
      <h2 id="limits-title" className="font-medium">
        Each person&apos;s limits
      </h2>
      <dl className="divide-y divide-line rounded-lg border border-line px-4 text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="grid grid-cols-[14rem_1fr] gap-4 py-2.5">
            <dt className="text-fg-muted">{label}</dt>
            <dd className="tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="text-xs text-fg-muted">
        The same for every organization here: whoever runs repo-sensei sets them.
      </p>
    </section>
  );
}
