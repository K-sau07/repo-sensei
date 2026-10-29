"use client";

/**
 * `/[org]/[repo]/trace` (3.15, frontend spec §6.10): a feature followed across the layers. Without
 * `?from=`, the entry picker; with it, the trace's paths (at most three, best first) as tabs, the
 * path picked as swimlanes with the Trace Beam (or as an ordered list), and the code of the hop
 * picked below. A trace the links stage hasn't reached yet (`linked: false`) says it stays in the
 * code; a path that stops short shows where and why.
 */
import { cn } from "cn";
import {
  Hourglass,
  List,
  Network,
  Orbit,
  Radar,
  Route as RouteIcon,
  Rows3,
  SearchX,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import { PartialBanner } from "@/components/feedback/banners";
import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { LockedState } from "@/components/feedback/locked-state";
import { useRepoFeatures, useRepoSlug } from "@/components/repo/use-repo";
import { RateLimited } from "@/components/search/rate-limited";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api/errors";
import {
  downHops,
  hopName,
  hopPlace,
  reachedAt,
  pathOutcome,
  pathStart,
  pathTitle,
  percent,
  placeLabel,
  returnGroups,
  type Trace,
  type TracePath,
} from "@/lib/api/trace";
import { STAGE_LABELS } from "@/lib/indexing";
import { repoPath } from "@/lib/routes";
import { overviewTraceHref, pathIndex, traceHref, type TraceView } from "@/lib/trace/url-state";

import { EntryPicker } from "./entry-picker";
import { HopCode } from "./hop-code";
import { TRACE_LANES } from "./meta";
import { TraceList } from "./trace-list";
import { TraceSwimlane } from "./trace-swimlane";
import { useTrace, useTraceState } from "./use-trace";

// Traces whose beam has run (it runs once, when a trace first resolves on this page).
const PULSED = new Set<string>();

function Frame({ children }: { children: ReactNode }) {
  return <div className="mx-auto flex w-full max-w-3xl flex-col p-8">{children}</div>;
}

/** An action a later phase builds: shown, not disabled away (its reason has to be readable). */
function Soon({ icon, label }: { icon: ReactNode; label: string }) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={<Button variant="ghost" size="sm" aria-disabled="true" className="opacity-50" />}
      >
        {icon}
        {label}
      </TooltipTrigger>
      <TooltipContent>Coming in a later phase</TooltipContent>
    </Tooltip>
  );
}

function TraceSkeleton() {
  return (
    <div role="status" aria-label="Tracing" className="flex flex-col gap-3 px-6 py-4">
      <Skeleton className="h-7 w-80" />
      <Skeleton className="h-8 w-96" />
      <Skeleton className="h-[30rem]" />
    </div>
  );
}

/** The graph isn't built: locked while a run brings it, otherwise waiting for an index. */
function NotTraceable() {
  const { org, repo } = useRepoSlug();
  const { state, active } = useRepoFeatures();
  const trace = state("trace");
  const stage = trace.stage ? STAGE_LABELS[trace.stage] : "indexing";
  if (active && trace.coming) {
    return (
      <Frame>
        <LockedState
          feature="Trace"
          stage={stage}
          {...(trace.etaSeconds !== null ? { etaSeconds: trace.etaSeconds } : {})}
          href={repoPath(org, repo)}
        />
      </Frame>
    );
  }
  return (
    <Frame>
      <EmptyState
        icon={<Hourglass />}
        title="Traces appear after indexing"
        description="The code graph a trace follows is built when this repository is indexed."
        action={
          <Link
            href={repoPath(org, repo, "index")}
            className="text-sm underline underline-offset-4"
          >
            Indexing
          </Link>
        }
      />
    </Frame>
  );
}

function TraceFailed({ error, onRetry }: { error: Error; onRetry: () => void }) {
  const { org, repo } = useRepoSlug();
  const api = error instanceof ApiError ? error : null;
  if (api?.code === "graph_not_ready" || api?.code === "repo_not_indexed") return <NotTraceable />;
  if (api?.status === 429) {
    return (
      <Frame>
        <RateLimited error={api} onRetry={onRetry} title="Too many traces for now" verb="trace" />
      </Frame>
    );
  }
  if (api?.code === "trace_entry_not_found" || api?.status === 422) {
    return (
      <Frame>
        <EmptyState
          icon={<SearchX />}
          title="Nothing to trace from there"
          description={api.message}
          action={
            <Link href={traceHref(org, repo)} className="text-sm underline underline-offset-4">
              Pick where to start
            </Link>
          }
        />
      </Frame>
    );
  }
  return (
    <Frame>
      <ErrorState
        title="Couldn't trace this"
        description={api?.message ?? "The trace didn't finish. Trying again usually works."}
        onRetry={onRetry}
        {...(api?.requestId ? { requestId: api.requestId } : {})}
      />
    </Frame>
  );
}

/** A path's tab: its number, where it goes, whether it gets there, how sure. */
function PathTab({ path, index, trace }: { path: TracePath; index: number; trace: Trace }) {
  return (
    <TabsTrigger value={index} className="h-auto flex-none gap-2 px-2.5 py-1">
      <span>Path {index + 1}</span>
      <span className="max-w-48 truncate font-mono text-xs font-normal text-fg-muted">
        {pathTitle(path)}
      </span>
      <span
        className={cn("text-xs", path.reaches_goal ? "text-ok" : "text-fg-muted")}
        title={pathOutcome(path, trace.goal)}
      >
        <span aria-hidden>{path.reaches_goal ? "●" : "○"}</span>
        <span className="sr-only">{pathOutcome(path, trace.goal)},</span>
      </span>
      <span className="text-xs font-normal text-fg-muted tabular-nums">
        {percent(path.path_confidence)}
      </span>
    </TabsTrigger>
  );
}

/** What a path comes to, and the lanes it crosses. */
function PathSummary({ path, trace }: { path: TracePath; trace: Trace }) {
  return (
    <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-fg-muted">
      {pathStart(path) && (
        <span className={path.reaches_origin ? "text-fg" : ""}>{pathStart(path)}</span>
      )}
      <span className={path.reaches_goal ? "text-fg" : ""}>{pathOutcome(path, trace.goal)}</span>
      <span className="tabular-nums">
        {downHops(path).length} {downHops(path).length === 1 ? "hop" : "hops"},{" "}
        {percent(path.path_confidence)} sure
        {returnGroups(path).length > 0 &&
          `, then back to ${returnGroups(path)
            .map((group) => hopName(group.hop.node))
            .join(", ")}`}
      </span>
      <span className="flex flex-wrap items-center gap-1.5 text-xs">
        {path.layers.map((layer) => (
          <span key={layer} className={TRACE_LANES[layer].text}>
            <span aria-hidden>{TRACE_LANES[layer].glyph} </span>
            {TRACE_LANES[layer].label}
          </span>
        ))}
      </span>
    </p>
  );
}

function ViewToggle({ view, onChange }: { view: TraceView; onChange: (view: TraceView) => void }) {
  const options: { value: TraceView; label: string; icon: ReactNode }[] = [
    { value: "lanes", label: "Swimlanes", icon: <Rows3 aria-hidden /> },
    { value: "list", label: "List", icon: <List aria-hidden /> },
  ];
  return (
    <div
      role="group"
      aria-label="View"
      className="flex items-center rounded-sm border border-line p-0.5"
    >
      {options.map((option) => (
        <Button
          key={option.value}
          variant={view === option.value ? "secondary" : "ghost"}
          size="xs"
          aria-pressed={view === option.value}
          onClick={() => {
            onChange(option.value);
          }}
        >
          {option.icon}
          {option.label}
        </Button>
      ))}
    </div>
  );
}

function TraceView({ entry }: { entry: string }) {
  const { org, repo } = useRepoSlug();
  const [state, setState] = useTraceState();
  const trace = useTrace(entry);
  const key = `${org}/${repo}/${entry}`;
  // The beam runs on the path first shown, the first time this trace resolves here.
  const [fresh] = useState(() => !PULSED.has(key));
  const [firstPath] = useState(state.path);
  const [played, setPlayed] = useState(false);
  useEffect(() => {
    if (trace.data) PULSED.add(key);
  }, [trace.data, key]);

  if (trace.isPending) return <TraceSkeleton />;
  if (trace.isError)
    return <TraceFailed error={trace.error} onRetry={() => void trace.refetch()} />;

  const data = trace.data;
  const [first] = data.entries;
  const at = pathIndex(state.path, data.paths.length);
  const path = data.paths[at];
  const selected = state.hop !== null && path?.hops[state.hop] ? state.hop : null;
  const hop = selected !== null ? path?.hops[selected] : undefined;
  const entryHop = path?.hops[path.entry_index];
  const firstPlace = entryHop ? hopPlace(entryHop) : null;
  const select = (next: number | null) => void setState({ hop: next });

  return (
    <div className="flex flex-col gap-3 px-6 py-4" data-testid="trace-view">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <RouteIcon aria-hidden className="size-4 text-fg-muted" />
            Trace from <span className="truncate font-mono">{first?.name ?? entry}</span>
          </h2>
          <p className="flex flex-wrap items-center gap-x-3 text-xs text-fg-muted">
            {firstPlace && <span className="font-mono">{placeLabel(firstPlace)}</span>}
            {data.entries.length > 1 && (
              <span>
                The name has {data.entries.length} definitions: the paths start from each.
              </span>
            )}
            {data.goal && (
              <span>
                To <span className="font-mono">{data.goal.name}</span>
              </span>
            )}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <ViewToggle view={state.view} onChange={(view) => void setState({ view })} />
          {path && (
            <Link
              href={overviewTraceHref(org, repo, entry, at + 1)}
              className="inline-flex h-7 items-center gap-1.5 rounded-sm px-2 text-sm text-fg-muted hover:bg-surface-2 hover:text-fg [&_svg]:size-4"
            >
              <Orbit aria-hidden />
              Show on the constellation
            </Link>
          )}
          <Soon icon={<Network aria-hidden />} label="Open as diagram" />
          <Soon icon={<Radar aria-hidden />} label="Impact" />
          <Link
            href={traceHref(org, repo)}
            className="inline-flex h-7 items-center rounded-sm px-2 text-sm text-fg-muted hover:bg-surface-2 hover:text-fg"
          >
            Trace from elsewhere
          </Link>
        </div>
      </header>

      {!data.linked && (
        <PartialBanner
          action={
            <Link
              href={repoPath(org, repo, "index")}
              className="shrink-0 text-sm underline underline-offset-4"
            >
              Indexing
            </Link>
          }
        >
          <strong className="font-medium">This trace stays in the code.</strong> The links between
          the layers (calls over HTTP to their endpoints, code to its tables) aren&apos;t in this
          commit&apos;s graph until its links stage has run, so no path can reach the data yet.
        </PartialBanner>
      )}

      <p role="status" aria-live="polite" className="sr-only">
        {data.paths.length === 0
          ? "No path from here."
          : `Trace ready: ${String(data.paths.length)} ${data.paths.length === 1 ? "path" : "paths"}. ${path ? pathOutcome(path, data.goal) : ""}.`}
      </p>

      {data.paths.length === 0 || !path ? (
        <EmptyState
          icon={<RouteIcon />}
          title="No path from here"
          description="Nothing the trace follows leaves this entry: no render, hook, call, request or query."
        />
      ) : (
        <Tabs
          value={at}
          onValueChange={(value: number) => {
            void setState({ path: value + 1, hop: null });
          }}
        >
          <TabsList aria-label="Paths, best first" variant="line" className="h-auto flex-wrap">
            {data.paths.map((candidate, i) => (
              <PathTab key={i} path={candidate} index={i} trace={data} />
            ))}
          </TabsList>
          {data.paths.map((candidate, i) => (
            <TabsContent key={i} value={i} className="flex flex-col gap-3 pt-1">
              <PathSummary path={candidate} trace={data} />
              {state.view === "list" ? (
                <TraceList path={candidate} selected={selected} onSelect={select} />
              ) : (
                <TraceSwimlane
                  path={candidate}
                  selected={selected}
                  onSelect={select}
                  pulse={
                    fresh &&
                    !played &&
                    i === pathIndex(firstPath, data.paths.length) &&
                    candidate.reaches_goal
                  }
                  onPulsed={() => {
                    setPlayed(true);
                  }}
                />
              )}
            </TabsContent>
          ))}
        </Tabs>
      )}

      {hop && (
        <HopCode
          key={`${String(at)}:${String(selected)}`}
          hop={hop}
          how={path && selected !== null ? reachedAt(path, selected) : "entry"}
          onClose={() => {
            select(null);
          }}
        />
      )}
    </div>
  );
}

export function TracePage() {
  const [{ from }] = useTraceState();
  const entry = from?.trim() ?? "";
  if (!entry) return <EntryPicker />;
  return <TraceView key={entry} entry={entry} />;
}
