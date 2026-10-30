"use client";

import { useQueryClient } from "@tanstack/react-query";
import { cn } from "cn";
import { ArrowRight, Check, CircleDashed, Minus, OctagonX, RotateCw, Sparkles } from "lucide-react";
import { animate, useReducedMotion } from "motion/react";
import Link from "next/link";
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { formatEta } from "@/components/feedback/locked-state";
import { LiveDot } from "@/components/primitives/live-dot";
import { RelativeTime } from "@/components/primitives/relative-time";
import { tabForFeature } from "@/components/repo/repo-tabs-config";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api, api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { IndexJob, Stage } from "@/lib/api/types";
import { FEATURE_LABELS, STAGE_LABELS, type Feature } from "@/lib/indexing";
import { repoPath } from "@/lib/routes";

import { Constellation } from "./constellation";
import { LogViewer } from "./log-viewer";
import { refreshIndexState, startReindex } from "./reindex";
import { useJobEvents } from "./use-job-events";

/** The repository the view is about (for links and actions inside it). */
const IndexContext = createContext<{ org: string; repo: string } | null>(null);
const useIndexContext = () => useContext(IndexContext);

interface StageEntry {
  status?: string;
  done?: number;
  total?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  details?: Record<string, unknown>;
  error?: { message?: string } | null;
}

export function stageEntry(job: IndexJob, stage: Stage): StageEntry {
  return (job.stages as Record<string, StageEntry>)[stage] ?? {};
}

function seconds(entry: StageEntry): number | null {
  if (!entry.started_at || !entry.finished_at) return null;
  return Math.max(0, (Date.parse(entry.finished_at) - Date.parse(entry.started_at)) / 1000);
}

const duration = (value: number) =>
  value < 1 ? "<1s" : value < 60 ? `${value.toFixed(0)}s` : formatEta(value);

const STATUS_ICON: Record<string, React.ReactNode> = {
  done: <Check aria-hidden className="size-4 text-ok" />,
  failed: <OctagonX aria-hidden className="size-4 text-danger" />,
  skipped: <Minus aria-hidden className="size-4 text-fg-faint" />,
  cancelled: <Minus aria-hidden className="size-4 text-fg-muted" />,
};

const STATUS_WORDS: Record<string, string> = {
  done: "done",
  failed: "failed",
  skipped: "not in this run",
  cancelled: "cancelled",
  running: "running",
};

/** Every stage of the run: status, done/total, how long it took or how long it should take. */
export function StageChecklist({ job, stages }: { job: IndexJob; stages: Stage[] }) {
  return (
    <ol aria-label="Indexing stages" className="flex flex-col gap-1">
      {stages.map((stage) => {
        const entry = stageEntry(job, stage);
        const status = entry.status ?? "pending";
        const running = status === "running";
        const took = seconds(entry);
        const share =
          entry.total && entry.done !== undefined ? Math.min(1, entry.done / entry.total) : null;
        return (
          <li
            key={stage}
            aria-current={running ? "step" : undefined}
            className={cn(
              "flex flex-col gap-1 rounded-md px-2 py-1.5",
              running && "bg-surface-2",
              status === "skipped" && "text-fg-faint",
            )}
          >
            <div className="flex items-center gap-2.5 text-sm">
              <span className="flex size-4 items-center justify-center">
                {running ? (
                  <LiveDot label="Running" />
                ) : (
                  (STATUS_ICON[status] ?? (
                    <CircleDashed aria-hidden className="size-4 text-fg-faint" />
                  ))
                )}
              </span>
              <span className="flex-1">{STAGE_LABELS[stage]}</span>
              <span className="text-xs text-fg-muted tabular-nums">
                {running && entry.total
                  ? `${String(entry.done ?? 0)}/${String(entry.total)}`
                  : took !== null && status === "done"
                    ? duration(took)
                    : null}
                {running && job.eta_s ? ` · ~${formatEta(job.eta_s)}` : null}
              </span>
              {/* The live dot already says "Running". */}
              {!running && <span className="sr-only">{STATUS_WORDS[status] ?? "waiting"}</span>}
            </div>
            {running && share !== null && (
              <div aria-hidden className="ml-6.5 h-1 overflow-hidden rounded-full bg-surface-3">
                <div
                  className="h-full rounded-full bg-signal transition-[width] duration-(--dur-base)"
                  style={{ width: `${String(share * 100)}%` }}
                />
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/** A number that counts up to its new value (or jumps, with reduced motion). */
export function LiveCounter({ label, value }: { label: string; value: number | null }) {
  const reduce = useReducedMotion();
  const [shown, setShown] = useState(value ?? 0);
  const from = useRef(value ?? 0);
  useEffect(() => {
    if (value === null || reduce) return;
    const controls = animate(from.current, value, {
      duration: 0.6,
      ease: "easeOut",
      onUpdate: (latest) => {
        setShown(Math.round(latest));
      },
    });
    from.current = value;
    return () => {
      controls.stop();
    };
  }, [value, reduce]);
  // With reduced motion the number simply changes.
  const display = reduce ? (value ?? 0) : shown;
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd className="text-2xl font-semibold tabular-nums">
        {value === null ? "—" : display.toLocaleString("en")}
      </dd>
    </div>
  );
}

/** "Code browser available now →" for each feature this run unlocked. */
export function UnlockPills({ features }: { features: Feature[] }) {
  const context = useIndexContext();
  if (features.length === 0) return null;
  const className =
    "inline-flex items-center gap-1.5 rounded-full border border-signal/40 bg-signal/10 px-3 py-1 text-sm text-fg motion-safe:animate-in motion-safe:fade-in motion-safe:zoom-in-95";
  return (
    <ul aria-label="Now available" className="flex flex-wrap gap-2">
      {features.map((feature) => {
        const tab = tabForFeature(feature);
        const label = `${FEATURE_LABELS[feature]} available now`;
        return (
          <li key={feature}>
            {context && tab ? (
              <Link href={repoPath(context.org, context.repo, tab.section)} className={className}>
                <Sparkles aria-hidden className="size-3.5 text-signal" />
                {label}
                <ArrowRight aria-hidden className="size-3.5" />
              </Link>
            ) : (
              <span className={className}>
                <Sparkles aria-hidden className="size-3.5 text-signal" />
                {label}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** What discovery found, while the rest runs: "react · @tanstack/react-query → fastapi …". */
export function WhileYouWait({
  details,
  running,
}: {
  details: Record<string, unknown> | undefined;
  running: boolean;
}) {
  const frameworks = (details?.frameworks as string[] | undefined) ?? [];
  const services = (details?.services as string[] | undefined) ?? [];
  if (frameworks.length === 0 && services.length === 0) return null;
  return (
    <section
      aria-labelledby="while-you-wait"
      className="flex flex-col gap-2 rounded-lg border border-line p-4"
    >
      <h3 id="while-you-wait" className="text-sm font-medium">
        {running ? "While you wait" : "What we found"}
      </h3>
      {frameworks.length > 0 && (
        <p className="text-sm text-fg-muted">
          Built with{" "}
          {frameworks.map((name, index) => (
            <span key={name}>
              {index > 0 && ", "}
              <span className="font-medium text-fg">{name}</span>
            </span>
          ))}
          .
        </p>
      )}
      {services.length > 0 && (
        <p className="text-sm text-fg-muted">
          Runs as {services.length} services: {services.join(", ")}.
        </p>
      )}
    </section>
  );
}

/** Retry (a new run) and, while it runs, Cancel (admins, with a confirmation). */
function JobActions({ job }: { job: IndexJob }) {
  const context = useIndexContext();
  const queryClient = useQueryClient();
  const { role } = useOrgRole();
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  if (!context || !roleAtLeast(role, "admin")) return null;
  const { org, repo } = context;
  const refresh = () => refreshIndexState(queryClient, org, repo, job.id);
  const run = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true);
    try {
      await action();
      toast.success(success);
      await refresh();
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  };
  const active = job.status === "queued" || job.status === "running";
  if (active) {
    return (
      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogTrigger render={<Button variant="outline" size="sm" disabled={busy} />}>
          Cancel indexing
        </AlertDialogTrigger>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Cancel this index run?</AlertDialogTitle>
            <AlertDialogDescription>
              It stops at its next step. What was indexed before stays available.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep indexing</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => {
                setConfirming(false);
                void run(
                  () =>
                    api.POST("/api/v1/orgs/{org}/repos/{repo}/index-jobs/{job}/cancel", {
                      params: { path: { org, repo, job: job.id } },
                    }),
                  "Indexing cancelled",
                );
              }}
            >
              Cancel indexing
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    );
  }
  return (
    <Button
      variant={job.status === "failed" ? "default" : "outline"}
      size="sm"
      disabled={busy}
      onClick={() => void run(() => startReindex(org, repo), "Indexing started")}
    >
      <RotateCw aria-hidden data-icon="inline-start" />
      {job.status === "failed" ? "Retry" : "Index again"}
    </Button>
  );
}

/** A failed run: which stage, why, and what to do. */
function JobFailure({ job, onLogs }: { job: IndexJob; onLogs: () => void }) {
  const error = (job.error ?? {}) as { stage?: Stage; message?: string; code?: string };
  return (
    <section
      role="alert"
      className="flex flex-col gap-3 rounded-lg border border-danger/30 bg-danger/6 p-4"
    >
      <div className="flex items-center gap-2 text-danger">
        <OctagonX aria-hidden className="size-4" />
        <h3 className="text-sm font-semibold">
          {error.stage ? `${STAGE_LABELS[error.stage]} failed` : "Indexing failed"}
        </h3>
      </div>
      <p className="text-sm text-fg-muted">{error.message ?? "Something went wrong."}</p>
      <div className="flex flex-wrap items-center gap-2">
        <JobActions job={job} />
        <Button variant="ghost" size="sm" onClick={onLogs}>
          View logs
        </Button>
        {error.code && <code className="font-mono text-xs text-fg-faint">{error.code}</code>}
      </div>
    </section>
  );
}

function announcement(job: IndexJob): string {
  switch (job.status) {
    case "queued":
      return "Indexing is queued.";
    case "running":
      return job.stage ? `Indexing: ${STAGE_LABELS[job.stage]}.` : "Indexing.";
    case "succeeded":
      return "Indexing finished.";
    case "failed":
      return "Indexing failed.";
    case "cancelled":
      return "Indexing was cancelled.";
  }
}

/** Files, lines, languages and frameworks: final once discovery is done, the file count live
 * while it runs. */
function counters(job: IndexJob) {
  const discover = stageEntry(job, "discover");
  const details = discover.details ?? {};
  const files =
    typeof details.files === "number"
      ? details.files
      : discover.status === "running" && typeof discover.done === "number"
        ? discover.done
        : null;
  const languages = details.languages as Record<string, number> | undefined;
  return {
    files,
    loc: typeof details.loc === "number" ? details.loc : null,
    languages: languages ? Object.keys(languages).length : null,
    frameworks: Array.isArray(details.frameworks) ? details.frameworks.length : null,
    details,
  };
}

/**
 * The live view of one index run: stages with progress and ETA, the constellation forming,
 * counters, what it unlocked, what discovery found, the log, and the failure (with Retry) or
 * Cancel.
 */
export function IndexView({
  org,
  repo,
  jobId,
  known,
}: {
  org: string;
  repo: string;
  jobId: string;
  /** The run as a list of runs already has it: shown at once while it is read again. */
  known?: IndexJob | undefined;
}) {
  const job = $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/index-jobs/{job}",
    { params: { path: { org, repo, job: jobId } } },
    { placeholderData: () => (known?.id === jobId ? known : undefined) },
  );
  const stages = $api.useQuery("get", "/api/v1/indexing/stages", {}, { staleTime: Infinity });
  const { logs, live } = useJobEvents(org, repo, job.data ? jobId : null);
  const [logsOpen, setLogsOpen] = useState(false);

  if (job.isPending || stages.isPending) {
    return (
      <div
        role="status"
        aria-label="Loading the index run"
        className="grid gap-4 md:grid-cols-[18rem_1fr]"
      >
        <Skeleton className="h-72" />
        <Skeleton className="h-72" />
      </div>
    );
  }
  if (job.isError || stages.isError) {
    return (
      <p role="alert" className="text-sm text-danger">
        Couldn&apos;t load this index run.
      </p>
    );
  }
  const data = job.data;
  const { files, loc, languages, frameworks, details } = counters(data);
  const running = data.status === "queued" || data.status === "running";
  return (
    <IndexContext value={{ org, repo }}>
      {/* What screen readers hear as the run moves: each stage, and how it ended (not every
          percent). */}
      <p role="status" aria-live="polite" className="sr-only">
        {announcement(data)}
      </p>
      <div className="grid gap-6 md:grid-cols-[18rem_1fr]">
        <aside className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium">
              {running ? "Indexing" : data.status === "succeeded" ? "Indexed" : "Index run"}
              <span className="ml-2 text-fg-muted tabular-nums">
                {Math.round(data.progress * 100)}%
              </span>
            </h2>
            {live && <LiveDot label="Live" />}
          </div>
          <StageChecklist job={data} stages={stages.data.stages.map((s) => s.stage)} />
        </aside>
        <div className="flex flex-col gap-5">
          {data.status === "failed" && (
            <JobFailure
              job={data}
              onLogs={() => {
                setLogsOpen(true);
              }}
            />
          )}
          <UnlockPills features={data.features} />
          <Constellation org={org} repo={repo} job={jobId} live={running && live} />
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <LiveCounter label="Files" value={files} />
            <LiveCounter label="Lines" value={loc} />
            <LiveCounter label="Languages" value={languages} />
            <LiveCounter label="Frameworks" value={frameworks} />
          </dl>
          <WhileYouWait details={details} running={running} />
          <div className="flex flex-wrap items-center gap-2">
            {data.status !== "failed" && <JobActions job={data} />}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setLogsOpen(true);
              }}
            >
              View logs ({logs.length})
            </Button>
            {data.finished_at && (
              <span className="text-xs text-fg-muted">
                Finished <RelativeTime date={data.finished_at} />
              </span>
            )}
          </div>
        </div>
      </div>
      <LogViewer open={logsOpen} onOpenChange={setLogsOpen} lines={logs} />
    </IndexContext>
  );
}
