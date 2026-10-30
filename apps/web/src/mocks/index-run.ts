/**
 * Mock index runs, replaying a real recording (fixtures/index-run.json: the fastapi template's
 * run, as its event stream sent it) three times slower so it can be watched. A run's state at any
 * moment is worked out from the time since it started, so it carries on across reloads, and the
 * job, the repository and the event stream always agree. A run can also be set to fail at a
 * stage, for the failure states.
 *
 * The recording predates the constellation: its one `constellation` event (all 245 points of
 * fixtures/constellation.json, the same commit's layout) goes where discovery sends it, just
 * before the progress report for the batch.
 */
import type { ConstellationPage, IndexJob, Stage } from "@/lib/api/types";

import sky from "./fixtures/constellation.json";
import recording from "./fixtures/index-run.json";

export const SPEED = 3;
const LEAD_MS = 300; // queued, before the first stage starts

export interface RunEvent {
  type: string;
  stage: Stage | null;
  at: number; // ms since the run was queued
  [field: string]: unknown;
}

export interface RunFailure {
  stage: Stage;
  code: string;
  message: string;
}

interface Recorded {
  type: string;
  stage: Stage | null;
  at_ms: number;
  [field: string]: unknown;
}

/**
 * The recording also predates the search pipeline (its parse to links stages were skipped). The
 * mock API serves symbols and keyword search over the recorded commit, so those two stages are
 * played as done, in no time, right after the Merkle stage (where the pipeline runs them), with
 * what they unlock (keyword search and Ask). Embeddings stay skipped: semantic search isn't served.
 */
const SEARCH_STAGES: { stage: Stage; unlocks: IndexJob["features"] }[] = [
  { stage: "symbols", unlocks: ["goto_definition"] },
  { stage: "chunks", unlocks: ["keyword_search", "ask"] },
];

/**
 * It serves the code graph (3.14) and traces (3.15) too, so the graph and links stages are played
 * the same way, just before finalize (where the pipeline runs them), unlocking the trace: the other
 * pages they unlock (the graph, diagrams, fixes, changes) aren't built.
 */
const GRAPH_STAGES: { stage: Stage; unlocks: IndexJob["features"] }[] = [
  { stage: "graph", unlocks: [] },
  { stage: "links", unlocks: ["trace"] },
];

const played = (stages: typeof SEARCH_STAGES, atMs: number, progress: unknown): Recorded[] =>
  stages.flatMap(({ stage, unlocks }) => [
    { type: "stage_started", stage, progress, at_ms: atMs },
    { type: "stage_done", stage, progress, at_ms: atMs },
    ...(unlocks.length > 0 ? [{ type: "unlock", stage, unlocks, at_ms: atMs }] : []),
  ]);

function withPlayedStages(events: Recorded[]): Recorded[] {
  return events.flatMap((event) => {
    const { at_ms: atMs, progress } = event;
    if (event.type === "stage_done" && event.stage === "merkle") {
      return [event, ...played(SEARCH_STAGES, atMs, progress)];
    }
    if (event.type === "stage_started" && event.stage === "finalize") {
      return [...played(GRAPH_STAGES, atMs, progress), event];
    }
    return [event];
  });
}

const RECORDED = withPlayedStages(recording.events as unknown as Recorded[]);
export const RUN_DETAILS = recording.stages as unknown as Record<string, Record<string, unknown>>;
/** The run's constellation, complete (`GET …/constellation` from the start). */
export const SKY = sky as unknown as ConstellationPage;

/** Every event of a run, in order, failing at ``failure.stage`` if given. */
export function runEvents(failure?: RunFailure): RunEvent[] {
  const events: RunEvent[] = [{ type: "queued", stage: null, at: 0 }];
  for (const { at_ms: atMs, ...event } of RECORDED) {
    const at = LEAD_MS + atMs * SPEED;
    // (event.stage is never undefined, so a match means there is a failure.)
    if (failure?.stage === event.stage && event.type !== "stage_started") {
      events.push(
        { type: "log", stage: failure.stage, level: "error", message: failure.message, at },
        {
          type: "failed",
          stage: failure.stage,
          code: failure.code,
          message: failure.message,
          at: at + 1,
        },
      );
      return events;
    }
    const batch = event.stage === "discover" && event.type === "progress";
    if (batch && event.done === event.total && !events.some((e) => e.type === "constellation")) {
      events.push({
        type: "constellation",
        stage: "discover",
        cursor: SKY.total,
        total: SKY.total,
        generation: SKY.generation,
        at,
      });
    }
    events.push({ ...event, at });
  }
  return events;
}

export const RUN_MS = (runEvents().at(-1)?.at ?? 0) + 50;

const ORDER: Stage[] = [
  "clone",
  "discover",
  "merkle",
  "parse",
  "symbols",
  "chunks",
  "embeddings",
  "summaries",
  "graph",
  "links",
  "finalize",
];

/** The job as the API would return it ``elapsed`` ms after it was queued. */
export function jobAt(
  base: Pick<IndexJob, "id" | "repo_id" | "created_at">,
  elapsed: number,
  failure?: RunFailure,
): IndexJob {
  const stages: Record<string, Record<string, unknown>> = {};
  const job: IndexJob = {
    ...base,
    snapshot_id: null,
    kind: "full",
    status: "queued",
    stage: null,
    stages,
    partial: {},
    progress: 0,
    eta_s: null,
    error: null,
    features: [],
    started_at: null,
    finished_at: null,
  };
  const at = (ms: number) => new Date(Date.parse(base.created_at) + ms).toISOString();
  for (const event of runEvents(failure)) {
    if (event.at > elapsed) break;
    const stage = event.stage;
    const entry = stage ? (stages[stage] ??= {}) : {};
    if (typeof event.progress === "number") job.progress = event.progress;
    switch (event.type) {
      case "stage_started":
        if (stage) {
          for (const earlier of ORDER.slice(0, ORDER.indexOf(stage))) {
            stages[earlier] ??= { status: "skipped" };
          }
        }
        job.status = "running";
        job.stage = stage;
        job.started_at ??= at(event.at);
        Object.assign(entry, { status: "running", done: 0, total: null, started_at: at(event.at) });
        break;
      case "progress":
        Object.assign(entry, { done: event.done, total: event.total });
        job.eta_s = typeof event.eta_s === "number" ? event.eta_s * SPEED : null;
        break;
      case "stage_done":
        Object.assign(entry, {
          status: "done",
          finished_at: at(event.at),
          details: stage ? (RUN_DETAILS[stage]?.details ?? {}) : {},
        });
        break;
      case "unlock":
        job.features = [...job.features, ...(event.unlocks as IndexJob["features"])];
        break;
      case "failed":
        job.status = "failed";
        job.error = { stage, code: event.code, message: event.message };
        Object.assign(entry, { status: "failed", error: job.error, finished_at: at(event.at) });
        job.finished_at = at(event.at);
        job.eta_s = null;
        break;
      case "done":
        job.status = "succeeded";
        job.progress = 1;
        job.eta_s = 0;
        job.finished_at = at(event.at);
        break;
    }
  }
  return job;
}
