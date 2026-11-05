"use client";

/**
 * An org's insights (10.6.1): the last 30 days in the repositories the reader can read. The
 * questions (a figure, then one bar a day, each with its numbers on hover and in a table), how
 * the answers went, which repositories are asked about, the modules answers cite most (a treemap,
 * each tile's area its share, and a table), and each week's adoption. Costs show for admins.
 */
import Link from "next/link";

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
import { useOrgRole } from "@/hooks/use-org-role";
import { FEEDBACK_KINDS } from "@/lib/api/ask";
import { $api } from "@/lib/api/client";
import type { Schemas } from "@/lib/api/types";
import { treemap } from "@/lib/insights/treemap";
import { repoPath } from "@/lib/routes";

type Insights = Schemas["InsightsOut"];

const USD = new Intl.NumberFormat("en", { style: "currency", currency: "USD" });
const COUNT = new Intl.NumberFormat("en");
const PERCENT = new Intl.NumberFormat("en", { style: "percent" });
const DAY = new Intl.DateTimeFormat("en", { month: "short", day: "numeric", timeZone: "UTC" });
const CHART_HEIGHT = 120;
const MAP_WIDTH = 720;
const MAP_HEIGHT = 280;
const GAP = 2; // the surface between tiles

const WRONG = Object.fromEntries(FEEDBACK_KINDS.map((k) => [k.kind, k.label])) as Record<
  string,
  string
>;

export function InsightsPage() {
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col p-8">
      <InsightsBody />
    </div>
  );
}

function InsightsBody() {
  const { slug, me } = useOrgRole();
  const insights = $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/insights",
    { params: { path: { org: slug } } },
    { enabled: slug !== "" },
  );
  if (me.isError || insights.isError) {
    return <ErrorState title="Insights didn't load" onRetry={() => void insights.refetch()} />;
  }
  if (!insights.data) {
    return (
      <div role="status" aria-label="Loading insights" className="flex flex-col gap-4">
        <Skeleton className="h-24" />
        <Skeleton className="h-40" />
        <Skeleton className="h-64" />
      </div>
    );
  }
  return <InsightsView org={slug} data={insights.data} />;
}

function InsightsView({ org, data }: { org: string; data: Insights }) {
  const questions = data.days.reduce((sum, d) => sum + d.questions, 0);
  const { answered, abstained, helpful, not_helpful } = data.answers;
  const rated = helpful + Object.values(not_helpful).reduce((a, b) => a + b, 0);
  const finished = answered + abstained;
  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">Insights</h1>
        <p className="text-sm text-fg-muted">The last 30 days, in the repositories you can read.</p>
      </div>
      {questions === 0 ? (
        <p className="text-sm text-fg-muted">
          No questions in the last 30 days. Ask one from any repository and this page fills in.
        </p>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            <Stat
              title="Questions"
              value={COUNT.format(questions)}
              note="First questions and follow-ups."
            />
            <Stat
              title="Answered from the code"
              value={finished > 0 ? PERCENT.format(answered / finished) : "—"}
              note={`${COUNT.format(abstained)} said the code doesn't answer it.`}
            />
            <Stat
              title="Rated helpful"
              value={rated > 0 ? PERCENT.format(helpful / rated) : "—"}
              note={rated > 0 ? `Of ${COUNT.format(rated)} rated.` : "No answer rated yet."}
            />
          </div>
          <Days days={data.days} costs={data.costs} />
          <Repos org={org} repos={data.repos} costs={data.costs} />
          <Wrong wrong={not_helpful} />
          <Modules modules={data.modules} />
        </>
      )}
      <Weeks weeks={data.weeks} />
    </div>
  );
}

function Stat({ title, value, note }: { title: string; value: string; note: string }) {
  return (
    <section className="flex flex-col gap-1 rounded-lg border border-line p-4" aria-label={title}>
      <h2 className="text-sm text-fg-muted">{title}</h2>
      <p className="text-2xl font-semibold tabular-nums">{value}</p>
      <p className="text-xs text-fg-muted">{note}</p>
    </section>
  );
}

function Days({ days, costs }: { days: Insights["days"]; costs: boolean }) {
  const most = Math.max(...days.map((d) => d.questions), 0);
  const total = days.reduce((sum, d) => sum + d.questions, 0);
  return (
    <section className="flex flex-col gap-3" aria-labelledby="insight-days">
      <h2 id="insight-days" className="font-medium">
        Questions each day
      </h2>
      <div
        className="flex items-end gap-0.5 border-b border-line"
        style={{ height: CHART_HEIGHT }}
        role="img"
        aria-label={`Questions each day for the last 30 days, ${COUNT.format(total)} in all; the table below has each day's.`}
      >
        {days.map((d) => {
          const height =
            most > 0 ? Math.max(d.questions > 0 ? 2 : 0, (d.questions / most) * CHART_HEIGHT) : 0;
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
                <p className="tabular-nums">
                  {COUNT.format(d.questions)} {d.questions === 1 ? "question" : "questions"}, by{" "}
                  {COUNT.format(d.people)} {d.people === 1 ? "person" : "people"}
                </p>
                {costs && d.cost_usd !== null && (
                  <p className="text-xs tabular-nums opacity-80">
                    {USD.format(Number(d.cost_usd))}
                  </p>
                )}
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
        <Table aria-label="Questions each day">
          <TableHeader>
            <TableRow>
              <TableHead>Day (UTC)</TableHead>
              <TableHead className="text-right">Questions</TableHead>
              <TableHead className="text-right">People</TableHead>
              {costs && <TableHead className="text-right">Spend</TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {[...days].reverse().map((d) => (
              <TableRow key={d.day}>
                <TableCell>{DAY.format(new Date(d.day))}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {COUNT.format(d.questions)}
                </TableCell>
                <TableCell className="text-right tabular-nums">{COUNT.format(d.people)}</TableCell>
                {costs && (
                  <TableCell className="text-right tabular-nums">
                    {USD.format(Number(d.cost_usd ?? 0))}
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </details>
    </section>
  );
}

function Repos({ org, repos, costs }: { org: string; repos: Insights["repos"]; costs: boolean }) {
  const asked = repos.filter((r) => r.questions > 0);
  if (asked.length === 0) return null;
  const most = Math.max(...asked.map((r) => r.questions));
  return (
    <section className="flex flex-col gap-3" aria-labelledby="insight-repos">
      <h2 id="insight-repos" className="font-medium">
        By repository
      </h2>
      <ul className="flex flex-col gap-2">
        {asked.map((repo) => (
          <li
            key={repo.repo_id}
            className="grid grid-cols-[minmax(0,12rem)_1fr_auto] items-center gap-3"
          >
            <Link
              href={repoPath(org, repo.slug, "ask")}
              className="truncate font-mono text-sm hover:underline"
            >
              {repo.full_name}
            </Link>
            <div className="h-2 rounded-full bg-muted" aria-hidden>
              <div
                className="h-full rounded-full bg-chart-1"
                style={{ width: `${(repo.questions / most) * 100}%` }}
              />
            </div>
            <span className="text-sm text-fg-muted tabular-nums">
              {COUNT.format(repo.questions)}
              {costs && repo.cost_usd !== null && ` · ${USD.format(Number(repo.cost_usd))}`}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Wrong({ wrong }: { wrong: Record<string, number> }) {
  const said = Object.entries(wrong).sort((a, b) => b[1] - a[1]);
  if (said.length === 0) return null;
  return (
    <section className="flex flex-col gap-2" aria-labelledby="insight-wrong">
      <h2 id="insight-wrong" className="font-medium">
        What people said was wrong
      </h2>
      <ul className="flex flex-wrap gap-2 text-sm">
        {said.map(([kind, count]) => (
          <li key={kind} className="rounded-md border border-line px-2 py-1">
            {WRONG[kind] ?? kind}{" "}
            <span className="text-fg-muted tabular-nums">{COUNT.format(count)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Modules({ modules }: { modules: Insights["modules"] }) {
  if (modules.length === 0) return null;
  const repos = new Set(modules.map((m) => m.repo_id)).size;
  const label = (m: Insights["modules"][number]) =>
    `${repos > 1 ? `${m.full_name}: ` : ""}${m.path || "(root)"}`;
  const tiles = treemap(modules, (m) => m.cited, MAP_WIDTH, MAP_HEIGHT);
  return (
    <section className="flex flex-col gap-3" aria-labelledby="insight-modules">
      <h2 id="insight-modules" className="font-medium">
        Most cited modules
      </h2>
      <p className="text-sm text-fg-muted">
        Each tile is a folder; its size is how many answers cite a file in it.
      </p>
      <svg
        viewBox={`0 0 ${MAP_WIDTH} ${MAP_HEIGHT}`}
        className="h-auto w-full"
        role="img"
        aria-label="Most cited modules as a treemap; the table below lists them."
      >
        {tiles.map((tile) => {
          const w = Math.max(0, tile.width - GAP);
          const h = Math.max(0, tile.height - GAP);
          const name = label(tile.item);
          const fits = w > 70 && h > 34;
          return (
            <g key={`${tile.item.repo_id}:${tile.item.path}`}>
              <title>{`${name}: ${COUNT.format(tile.item.cited)} answers`}</title>
              <rect
                x={tile.x + GAP / 2}
                y={tile.y + GAP / 2}
                width={w}
                height={h}
                rx={4}
                className="fill-chart-1/15 stroke-chart-1"
                strokeWidth={1}
              />
              {fits && (
                <>
                  <text x={tile.x + 10} y={tile.y + 20} className="fill-fg font-mono text-[12px]">
                    {name.length > w / 7.5 ? `${name.slice(0, Math.floor(w / 7.5) - 1)}…` : name}
                  </text>
                  <text x={tile.x + 10} y={tile.y + 36} className="fill-fg-muted text-[11px]">
                    {COUNT.format(tile.item.cited)}
                  </text>
                </>
              )}
            </g>
          );
        })}
      </svg>
      <details className="text-sm">
        <summary className="cursor-pointer text-fg-muted hover:text-fg">
          The modules, as a table
        </summary>
        <Table aria-label="Most cited modules">
          <TableHeader>
            <TableRow>
              <TableHead>Folder</TableHead>
              <TableHead className="text-right">Answers citing it</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {modules.map((m) => (
              <TableRow key={`${m.repo_id}:${m.path}`}>
                <TableCell className="font-mono text-xs">{label(m)}</TableCell>
                <TableCell className="text-right tabular-nums">{COUNT.format(m.cited)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </details>
    </section>
  );
}

function Weeks({ weeks }: { weeks: Insights["weeks"] }) {
  return (
    <section className="flex flex-col gap-3" aria-labelledby="insight-weeks">
      <h2 id="insight-weeks" className="font-medium">
        Each week
      </h2>
      <Table aria-label="Each week">
        <TableHeader>
          <TableRow>
            <TableHead>Week of (UTC)</TableHead>
            <TableHead className="text-right">People asking</TableHead>
            <TableHead className="text-right">Answered</TableHead>
            <TableHead className="text-right">Abstained</TableHead>
            <TableHead className="text-right">Helpful / not</TableHead>
            <TableHead className="text-right">Fixes</TableHead>
            <TableHead className="text-right">Changes</TableHead>
            <TableHead className="text-right">Diagrams</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {[...weeks].reverse().map((w) => (
            <TableRow key={w.week}>
              <TableCell>{DAY.format(new Date(w.week))}</TableCell>
              <TableCell className="text-right tabular-nums">{COUNT.format(w.askers)}</TableCell>
              <TableCell className="text-right tabular-nums">{COUNT.format(w.answered)}</TableCell>
              <TableCell className="text-right tabular-nums">{COUNT.format(w.abstained)}</TableCell>
              <TableCell className="text-right tabular-nums">
                {COUNT.format(w.helpful)} / {COUNT.format(w.not_helpful)}
              </TableCell>
              <TableCell className="text-right tabular-nums">{COUNT.format(w.fixes)}</TableCell>
              <TableCell className="text-right tabular-nums">{COUNT.format(w.changes)}</TableCell>
              <TableCell className="text-right tabular-nums">{COUNT.format(w.diagrams)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}
