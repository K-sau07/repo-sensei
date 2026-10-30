"use client";

/**
 * `/[org]/[repo]/runs/[runId]` (4.12, frontend spec §6.18): what an agent run did, step by
 * step. On top the waterfall (each step's time, calls made at once side by side); on the left
 * the steps in words (aggregated: repeated calls folded); in the centre the step picked; on the
 * right the files the run read. Time travel: a file picked goes to the step that first read it,
 * and the replay slider ghosts every step after the one it is at. A run under way grows as its
 * steps are recorded.
 */
import { cn } from "cn";
import { useParams } from "next/navigation";
import { useState } from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { DelayedSpinner } from "@/components/primitives/delayed-spinner";
import { Badge } from "@/components/ui/badge";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { filesRead, formatMs } from "@/lib/api/runs";

import { StageRail } from "./stage-rail";
import { StepCard } from "./step-card";
import { useRun } from "./use-runs";
import { RunWaterfall } from "./waterfall";

export function RunView() {
  const { runId } = useParams<{ runId: string }>();
  const run = useRun(runId);
  const [aggregated, setAggregated] = useState(true);
  const [picked, setPicked] = useState<number | null>(null);
  const [replay, setReplay] = useState<number | null>(null); // null: the whole run

  if (run.isPending) return <DelayedSpinner active label="Loading the run" />;
  if (run.isError) {
    return (
      <div className="p-8">
        <ErrorState title="This run couldn't be loaded" description={run.error.message} />
      </div>
    );
  }
  const { data, steps, live } = run;
  const last = steps.at(-1)?.idx ?? 0;
  const until = replay ?? last;
  const selected = picked ?? steps[0]?.idx ?? null;
  const step = steps.find((s) => s.idx === selected);
  const files = filesRead(steps);
  const go = (idx: number) => {
    setPicked(idx);
    if (replay !== null) setReplay(idx); // travelling: the replay follows
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex flex-col gap-3 border-b border-border px-6 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="min-w-0 flex-1 truncate text-base font-semibold">{data.question}</h1>
          <Badge variant={data.status === "failed" ? "destructive" : "secondary"}>
            {live ? "Running" : data.status}
          </Badge>
          {live ? (
            <span className="flex items-center gap-1 text-xs text-ok" data-testid="live">
              <span aria-hidden className="size-1.5 animate-pulse rounded-full bg-ok" /> Live
            </span>
          ) : null}
        </div>
        <dl className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-fg-muted">
          <Fact label="Model" value={data.model} />
          <Fact label="Mode" value={data.mode} />
          {data.user ? <Fact label="Asked by" value={data.user} /> : null}
          <Fact label="Steps" value={String(steps.length)} />
          <Fact label="Tokens" value={(data.tokens_in + data.tokens_out).toLocaleString()} />
          <Fact label="Cost" value={`$${Number(data.cost_usd).toFixed(4)}`} />
          {data.latency_ms !== null ? (
            <Fact label="Took" value={formatMs(data.latency_ms)} />
          ) : null}
          {data.stopped ? <Fact label="Stopped" value={data.stopped} /> : null}
          {data.error ? <Fact label="Error" value={data.error} /> : null}
        </dl>
        <RunWaterfall
          steps={steps}
          latencyMs={data.latency_ms}
          selected={selected}
          until={until}
          onSelect={go}
        />
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex flex-1 items-center gap-2 text-xs text-fg-muted">
            Replay
            <input
              type="range"
              min={0}
              max={last}
              value={until}
              aria-label="Replay to step"
              aria-valuetext={`Step ${String(until + 1)} of ${String(last + 1)}`}
              onChange={(event) => {
                const at = Number(event.target.value);
                setReplay(at === last ? null : at);
                setPicked(at);
              }}
              className="flex-1 accent-brand"
            />
            <span className="w-24 font-mono">
              {String(until + 1)} / {String(last + 1)}
            </span>
          </label>
          <ToggleGroup
            aria-label="Steps shown"
            value={[aggregated ? "aggregated" : "expanded"]}
            onValueChange={(value: string[]) => {
              if (value[0]) setAggregated(value[0] === "aggregated");
            }}
          >
            <ToggleGroupItem value="aggregated" size="sm">
              Aggregated
            </ToggleGroupItem>
            <ToggleGroupItem value="expanded" size="sm">
              Expanded
            </ToggleGroupItem>
          </ToggleGroup>
        </div>
      </header>
      <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[18rem_minmax(0,1fr)] lg:grid-cols-[18rem_minmax(0,1fr)_16rem]">
        <nav aria-label="Run steps" className="min-h-0 border-r border-border">
          <StageRail
            steps={steps}
            aggregated={aggregated}
            selected={selected}
            until={until}
            onSelect={go}
          />
        </nav>
        <section className="min-h-0 overflow-auto p-6">
          {step ? <StepCard step={step} /> : <p className="text-sm text-fg-muted">No steps yet.</p>}
        </section>
        <aside
          aria-label="Files the run read"
          className="hidden min-h-0 overflow-auto border-l border-border p-4 lg:block"
        >
          <h2 className="mb-2 text-xs font-medium text-fg-muted">Files read ({files.length})</h2>
          <ul className="flex flex-col gap-1" data-testid="files-read">
            {files.map(({ path, idx }) => (
              <li key={path}>
                <button
                  type="button"
                  onClick={() => {
                    setPicked(idx);
                    setReplay(idx);
                  }}
                  className={cn(
                    "w-full truncate rounded-sm px-1.5 py-0.5 text-left font-mono text-[11px] hover:bg-muted",
                    idx > until && "text-fg-muted line-through decoration-dotted",
                  )}
                  title={`First read at step ${String(idx + 1)}`}
                >
                  {path}
                </button>
              </li>
            ))}
          </ul>
        </aside>
      </div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1">
      <dt>{label}</dt>
      <dd className="font-mono text-fg">{value}</dd>
    </div>
  );
}
