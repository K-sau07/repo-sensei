"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { api } from "@/lib/api/client";
import type { IndexJob, Stage } from "@/lib/api/types";
import { followStream } from "@/lib/event-stream";
import { FEATURE_LABELS, type Feature } from "@/lib/indexing";
import { queryKeys } from "@/lib/query/keys";

export interface JobEvent {
  id: string;
  type:
    | "queued"
    | "stage_started"
    | "progress"
    | "stage_done"
    | "unlock"
    | "log"
    | "constellation"
    | "done"
    | "failed"
    | "cancelled";
  stage: Stage | null;
  ts: string;
  done?: number;
  total?: number;
  eta_s?: number | null;
  progress?: number;
  unlocks?: Feature[];
  level?: "info" | "warn" | "error";
  message?: string;
  code?: string;
  /** constellation: how many points can be read now, of `total`, in this generation. */
  cursor?: number;
  generation?: string;
}

export interface LogLine {
  id: string;
  ts: string;
  level: "info" | "warn" | "error";
  stage: Stage | null;
  message: string;
}

const TYPES: JobEvent["type"][] = [
  "queued",
  "stage_started",
  "progress",
  "stage_done",
  "unlock",
  "log",
  "constellation",
  "done",
  "failed",
  "cancelled",
];
const TERMINAL = new Set<JobEvent["type"]>(["done", "failed", "cancelled"]);
const FRESH_MS = 15_000; // unlocks this recent get a toast; replayed history doesn't

type Stages = Record<string, Record<string, unknown>>;

export const isSettled = (job: IndexJob | undefined) =>
  job !== undefined && job.status !== "queued" && job.status !== "running";

/** The job after ``event`` (a new object; the event stream's view of the state machine).
 *
 * The stream starts with the run's history, which can be older than the job it is applied to:
 * a finished run stays as it is, a stage the job already has done isn't restarted, and progress
 * never goes back. */
export function applyEvent(job: IndexJob, event: JobEvent): IndexJob {
  if (isSettled(job)) return job;
  const stage = event.stage;
  const known = stage ? (job.stages as Stages)[stage] : undefined;
  if (known?.status === "done" && (event.type === "stage_started" || event.type === "progress")) {
    return job;
  }
  const stages: Stages = { ...(job.stages as Stages) };
  const entry = stage ? { ...(stages[stage] ?? {}) } : {};
  const next: IndexJob = { ...job, stages };
  if (typeof event.progress === "number") next.progress = Math.max(job.progress, event.progress);
  switch (event.type) {
    case "stage_started":
      Object.assign(entry, { status: "running", done: 0, total: null, started_at: event.ts });
      next.status = "running";
      next.stage = stage;
      next.started_at ??= event.ts;
      break;
    case "progress":
      Object.assign(entry, { done: event.done, total: event.total });
      next.eta_s = event.eta_s ?? null;
      break;
    case "stage_done":
      Object.assign(entry, { status: "done", finished_at: event.ts });
      break;
    case "unlock":
      next.features = [...new Set([...job.features, ...(event.unlocks ?? [])])];
      break;
    case "failed":
      next.status = "failed";
      next.error = { stage, code: event.code, message: event.message };
      Object.assign(entry, { status: "failed", finished_at: event.ts, error: next.error });
      next.finished_at = event.ts;
      next.eta_s = null;
      break;
    case "cancelled":
      next.status = "cancelled";
      if (entry.status === "running") entry.status = "cancelled";
      next.finished_at = event.ts;
      next.eta_s = null;
      break;
    case "done":
      next.status = "succeeded";
      next.progress = 1;
      next.eta_s = 0;
      next.finished_at = event.ts;
      break;
    default:
      break;
  }
  if (stage && event.type !== "unlock" && event.type !== "log") stages[stage] = entry;
  return next;
}

/** ``job`` with the stage details of ``fetched`` (counts, frameworks: events don't carry them),
 * and otherwise as the stream left it. A refetch can be older than the events since, so it only
 * adds what the stream can't know. */
export function withDetails(job: IndexJob, fetched: IndexJob): IndexJob {
  const stages: Stages = { ...(job.stages as Stages) };
  let changed = false;
  for (const [stage, entry] of Object.entries(fetched.stages as Stages)) {
    const details = entry.details;
    if (details === undefined) continue;
    stages[stage] = { ...(stages[stage] ?? {}), details };
    changed = true;
  }
  return changed ? { ...job, stages } : job;
}

const FLUSH_MS = 100; // log lines and unlocks reach React in batches, not one render per line

interface Progress {
  job: string | null;
  /** The last event handled (to resume after), and every id seen (no event twice). */
  lastId: string | null;
  seen: Set<string>;
  toasted: Set<Feature>;
  ended: boolean;
}

const fresh = (job: string | null): Progress => ({
  job,
  lastId: null,
  seen: new Set(),
  toasted: new Set(),
  ended: false,
});

/**
 * Follow an index job's event stream: the cached job (`queryKeys.indexJob`) moves with every
 * event, logs and fresh unlocks are collected, and the stream closes after the final event.
 *
 * Where the stream got to is kept across the effect running again (the page hidden and shown,
 * a reconnect), so it resumes after the last event instead of replaying the run: no event is
 * applied twice and no unlock toasts twice. The job list (`queryKeys.indexJobs`) is refreshed
 * when a stage finishes or unlocks something, so tab locks follow the run.
 */
export function useJobEvents(org: string, repo: string, jobId: string | null) {
  const queryClient = useQueryClient();
  // Kept with the job they belong to, so switching jobs starts empty without a reset effect.
  const [collected, setCollected] = useState<{
    job: string | null;
    logs: LogLine[];
    unlocked: Feature[];
  }>({ job: null, logs: [], unlocked: [] });
  const [live, setLive] = useState(false);
  const progress = useRef<Progress>(fresh(null));
  const mine = collected.job === jobId ? collected : { logs: [], unlocked: [] };

  useEffect(() => {
    if (!jobId || typeof EventSource === "undefined") return;
    if (progress.current.job !== jobId) progress.current = fresh(jobId);
    const state = progress.current;
    if (state.ended) return; // it ended while this was open before: nothing more will come
    const key = queryKeys.indexJob(org, repo, jobId);
    const listKey = queryKeys.indexJobs(org, repo);
    const skyKey = queryKeys.constellation(org, repo, jobId);

    let lines: LogLine[] = [];
    let unlocks: Feature[] = [];
    let flushTimer: ReturnType<typeof setTimeout> | undefined;
    const flush = () => {
      clearTimeout(flushTimer);
      flushTimer = undefined;
      if (lines.length === 0 && unlocks.length === 0) return;
      const [newLines, newUnlocks] = [lines, unlocks];
      lines = [];
      unlocks = [];
      setCollected((current) => {
        const base = current.job === jobId ? current : { job: jobId, logs: [], unlocked: [] };
        return {
          job: jobId,
          logs: newLines.length ? [...base.logs, ...newLines] : base.logs,
          unlocked: newUnlocks.length
            ? [...new Set([...base.unlocked, ...newUnlocks])]
            : base.unlocked,
        };
      });
    };
    const later = () => {
      flushTimer ??= setTimeout(flush, FLUSH_MS);
    };
    const refreshDetails = () => {
      api
        .GET("/api/v1/orgs/{org}/repos/{repo}/index-jobs/{job}", {
          params: { path: { org, repo, job: jobId } },
        })
        .then(({ data }) => {
          if (data)
            queryClient.setQueryData<IndexJob>(key, (job) => (job ? withDetails(job, data) : data));
        })
        .catch(() => undefined); // the next stage brings them again
    };

    let close: (() => void) | null = null; // set once the stream is open
    const onEvent = (message: MessageEvent<string>) => {
      let event: JobEvent;
      try {
        event = JSON.parse(message.data) as JobEvent;
      } catch {
        return;
      }
      event.id ||= message.lastEventId;
      if (event.id) {
        if (state.seen.has(event.id)) return;
        state.seen.add(event.id);
        state.lastId = event.id;
      }
      // A run that had already finished when the stream opened only replays its history: it
      // feeds the log, while the job and the repository stay as the API returned them.
      const settled = isSettled(queryClient.getQueryData<IndexJob>(key));
      queryClient.setQueryData<IndexJob>(key, (job) => (job ? applyEvent(job, event) : job));
      if (!settled && event.type === "stage_done") refreshDetails();
      if (!settled && (event.type === "stage_done" || event.type === "unlock")) {
        void queryClient.invalidateQueries({ queryKey: listKey });
      }
      // More points to read, or discovery done (the stored constellation answers from then on).
      if (
        !settled &&
        (event.type === "constellation" ||
          (event.type === "stage_done" && event.stage === "discover"))
      ) {
        void queryClient.invalidateQueries({ queryKey: skyKey });
      }
      if (event.type === "log" && event.message) {
        lines.push({
          id: event.id,
          ts: event.ts,
          level: event.level ?? "info",
          stage: event.stage,
          message: event.message,
        });
        later();
      }
      if (event.type === "unlock" && event.unlocks?.length) {
        unlocks.push(...event.unlocks);
        later();
        if (Date.now() - Date.parse(event.ts) < FRESH_MS) {
          for (const feature of event.unlocks) {
            if (state.toasted.has(feature)) continue;
            state.toasted.add(feature);
            toast.success(`${FEATURE_LABELS[feature]} is ready`);
          }
        }
      }
      if (TERMINAL.has(event.type)) {
        state.ended = true;
        close?.();
        setLive(false);
        flush();
        if (settled) return;
        // The final word comes from the API (stage details, the repository's new status).
        void queryClient.invalidateQueries({ queryKey: key });
        void queryClient.invalidateQueries({ queryKey: listKey });
        void queryClient.invalidateQueries({ queryKey: queryKeys.repo(org, repo) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.repos(org) });
        void queryClient.invalidateQueries({ queryKey: skyKey });
        // The last index may be another commit now: its picture is read again from the start.
        void queryClient.resetQueries({ queryKey: queryKeys.constellation(org, repo, null) });
      }
    };

    const stop = followStream(
      `/api/v1/orgs/${encodeURIComponent(org)}/repos/${encodeURIComponent(repo)}/index-jobs/${encodeURIComponent(jobId)}/events`,
      {
        types: TYPES,
        onEvent,
        onLive: setLive,
        lastId: () => state.lastId,
        finished: () => state.ended,
      },
    );
    close = stop;
    return () => {
      stop();
      flush(); // what arrived in the last moment still counts
    };
  }, [org, repo, jobId, queryClient]);

  return { logs: mine.logs, unlocked: mine.unlocked, live };
}
