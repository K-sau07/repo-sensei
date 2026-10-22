/** Mock index jobs: stored per repository (when each was queued, how it ends), played back with
 * `index-run.ts`. The repository's status and counts follow its latest job. */
import type { IndexJob, Stage } from "@/lib/api/types";

import { db, saveDb, type MockRepo } from "./db";
import { jobAt, RUN_DETAILS, runEvents, RUN_MS, type RunEvent, type RunFailure } from "./index-run";

export interface MockJob {
  id: string;
  repo_id: string;
  created_at: string;
  queued_ms: number;
  failure?: RunFailure;
  cancelled_after_ms?: number;
}

export function jobsOf(repoId: string): MockJob[] {
  db.jobs[repoId] ??= [];
  return db.jobs[repoId];
}

export function startJob(repoId: string, failure?: RunFailure): MockJob {
  const now = Date.now();
  const job: MockJob = {
    id: crypto.randomUUID(),
    repo_id: repoId,
    created_at: new Date(now).toISOString(),
    queued_ms: now,
    ...(failure ? { failure } : {}),
  };
  jobsOf(repoId).push(job);
  saveDb();
  return job;
}

function elapsed(job: MockJob): number {
  const ms = Date.now() - job.queued_ms;
  return job.cancelled_after_ms === undefined ? ms : Math.min(ms, job.cancelled_after_ms);
}

/** The job as the API returns it now. */
export function jobView(job: MockJob): IndexJob {
  const view = jobAt(job, elapsed(job), job.failure);
  if (job.cancelled_after_ms === undefined || !isActive(view)) return view;
  const stages = { ...view.stages };
  if (view.stage) stages[view.stage] = { ...(stages[view.stage] as object), status: "cancelled" };
  return {
    ...view,
    status: "cancelled",
    stages,
    eta_s: null,
    finished_at: new Date(job.queued_ms + job.cancelled_after_ms).toISOString(),
  };
}

export const isActive = (job: IndexJob) => job.status === "queued" || job.status === "running";

const cancelListeners = new Map<string, Set<() => void>>();

/** Call ``listener`` when the job is cancelled (an open event stream sends `cancelled` then, as
 * the real API's does); returns the unsubscribe. */
export function onCancel(job: MockJob, listener: () => void): () => void {
  const listeners = cancelListeners.get(job.id) ?? new Set();
  cancelListeners.set(job.id, listeners);
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function cancelJob(job: MockJob): void {
  job.cancelled_after_ms = Date.now() - job.queued_ms;
  saveDb();
  for (const listener of cancelListeners.get(job.id) ?? []) listener();
  cancelListeners.delete(job.id);
}

/** The events the job's stream has had so far (the cancellation included). */
export function eventsOf(job: MockJob): (RunEvent & { id: string })[] {
  const upTo = elapsed(job);
  const events = runEvents(job.failure)
    .filter((event) => event.at <= upTo)
    .map((event, index) => ({ ...event, id: `${String(job.queued_ms)}-${String(index)}` }));
  const view = jobView(job);
  if (view.status === "cancelled") {
    events.push({
      type: "cancelled",
      stage: view.stage,
      at: upTo,
      id: `${String(job.queued_ms)}-${String(events.length)}`,
    });
  }
  return events;
}

/** Every event of the job from now on: (delay in ms, event), for a live stream. */
export function upcomingEvents(
  job: MockJob,
): { delay: number; event: RunEvent & { id: string } }[] {
  if (job.cancelled_after_ms !== undefined) return [];
  const now = elapsed(job);
  return runEvents(job.failure)
    .map((event, index) => ({
      event: { ...event, id: `${String(job.queued_ms)}-${String(index)}` },
    }))
    .filter(({ event }) => event.at > now)
    .map(({ event }) => ({ delay: event.at - now, event }));
}

const discovered = (RUN_DETAILS.discover?.details ?? {}) as { files?: number; loc?: number };

/** The commit every mock run indexes (the recorded run's). */
export const RUN_COMMIT = "b1125d8839482cb7bbae603ddc6d0081bf87532e";

/** A repository's first run once its discovery is done: what no `ref` reads before there is an
 * index (`treeReady` once the Merkle stage has run too). */
export function firstRunSnapshot(repo: MockRepo): { commit: string; treeReady: boolean } | null {
  if (repo.last_indexed_sha) return null;
  const latest = jobsOf(repo.id).at(-1);
  if (!latest) return null;
  const job = jobView(latest);
  const stages = job.stages as Record<string, { status?: string } | undefined>;
  if (!isActive(job) || stages.discover?.status !== "done") return null;
  return { commit: RUN_COMMIT, treeReady: stages.merkle?.status === "done" };
}

/** Bring the repository in line with its latest job. */
export function syncRepo(repo: MockRepo): MockRepo {
  const latest = jobsOf(repo.id).at(-1);
  if (!latest) return repo;
  const job = jobView(latest);
  if (isActive(job)) {
    repo.active_job = {
      id: job.id,
      status: job.status,
      stage: job.stage,
      progress: job.progress,
      eta_s: job.eta_s,
    };
    if (job.status === "running" && repo.status !== "access_lost") repo.status = "indexing";
    return repo;
  }
  const changed = repo.active_job !== null;
  repo.active_job = null;
  if (job.status === "succeeded") {
    repo.status = "ready";
    if (changed || !repo.last_indexed_at) {
      repo.last_indexed_sha = RUN_COMMIT;
      repo.last_indexed_at = job.finished_at;
      repo.file_count = discovered.files ?? 245;
      repo.loc = discovered.loc ?? 18_567;
    }
  } else if (repo.status !== "access_lost") {
    repo.status = repo.last_indexed_sha ? "ready" : job.status === "failed" ? "error" : "pending";
  }
  if (changed) saveDb();
  return repo;
}

export const RUN_LENGTH_MS = RUN_MS;
export type { RunFailure, Stage };
