"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import type { IndexJob, Stage } from "@/lib/api/types";
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
  "done",
  "failed",
  "cancelled",
];
const TERMINAL = new Set<JobEvent["type"]>(["done", "failed", "cancelled"]);
const FRESH_MS = 15_000; // unlocks this recent get a toast; replayed history doesn't

type Stages = Record<string, Record<string, unknown>>;

/** The job after ``event`` (a new object; the event stream's view of the state machine). */
export function applyEvent(job: IndexJob, event: JobEvent): IndexJob {
  const stages: Stages = { ...(job.stages as Stages) };
  const stage = event.stage;
  const entry = stage ? { ...(stages[stage] ?? {}) } : {};
  const next: IndexJob = { ...job, stages };
  if (typeof event.progress === "number") next.progress = event.progress;
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

/**
 * Follow an index job's event stream: the cached job (`queryKeys.indexJob`) moves with every
 * event, logs and fresh unlocks are collected, and the stream closes after the final event.
 * `EventSource` reconnects by itself and resumes from the last event id (`Last-Event-ID`).
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
  const mine = collected.job === jobId ? collected : { logs: [], unlocked: [] };

  useEffect(() => {
    if (!jobId || typeof EventSource === "undefined") return;
    const key = queryKeys.indexJob(org, repo, jobId);
    let source: EventSource | null = null;
    let closed = false;
    const update = (
      change: (state: { logs: LogLine[]; unlocked: Feature[] }) => {
        logs: LogLine[];
        unlocked: Feature[];
      },
    ) => {
      setCollected((state) => {
        const current = state.job === jobId ? state : { job: jobId, logs: [], unlocked: [] };
        return { job: jobId, ...change(current) };
      });
    };

    const onEvent = (message: MessageEvent<string>) => {
      let event: JobEvent;
      try {
        event = JSON.parse(message.data) as JobEvent;
      } catch {
        return;
      }
      event.id ||= message.lastEventId;
      queryClient.setQueryData<IndexJob>(key, (job) => (job ? applyEvent(job, event) : job));
      // A finished stage has details (counts, frameworks) that events don't carry.
      if (event.type === "stage_done") void queryClient.invalidateQueries({ queryKey: key });
      if (event.type === "log" && event.message) {
        const line: LogLine = {
          id: event.id,
          ts: event.ts,
          level: event.level ?? "info",
          stage: event.stage,
          message: event.message,
        };
        update((state) => ({
          ...state,
          logs: state.logs.some((l) => l.id === line.id) ? state.logs : [...state.logs, line],
        }));
      }
      if (event.type === "unlock" && event.unlocks?.length) {
        const unlocks = event.unlocks;
        update((state) => ({ ...state, unlocked: [...new Set([...state.unlocked, ...unlocks])] }));
        if (Date.now() - Date.parse(event.ts) < FRESH_MS) {
          for (const feature of event.unlocks) toast.success(`${FEATURE_LABELS[feature]} is ready`);
        }
      }
      if (TERMINAL.has(event.type)) {
        source?.close();
        setLive(false);
        // The final word comes from the API (stage details, the repository's new status).
        void queryClient.invalidateQueries({ queryKey: key });
        void queryClient.invalidateQueries({ queryKey: queryKeys.indexJobs(org, repo) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.repo(org, repo) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.repos(org) });
      }
    };

    const open = () => {
      if (closed) return;
      source = new EventSource(
        `/api/v1/orgs/${encodeURIComponent(org)}/repos/${encodeURIComponent(repo)}/index-jobs/${encodeURIComponent(jobId)}/events`,
      );
      source.onopen = () => {
        setLive(true);
      };
      source.onerror = () => {
        setLive(false); // the browser retries on its own
      };
      for (const type of TYPES) source.addEventListener(type, onEvent);
    };
    if (process.env.NEXT_PUBLIC_MOCKS === "1") {
      void import("@/mocks/start").then(({ startMocks }) => startMocks()).then(open);
    } else {
      open();
    }
    return () => {
      closed = true;
      source?.close();
    };
  }, [org, repo, jobId, queryClient]);

  return { logs: mine.logs, unlocked: mine.unlocked, live };
}
