import { describe, expect, it } from "vitest";

import type { IndexJob } from "@/lib/api/types";
import { jobAt, runEvents } from "@/mocks/index-run";

import { filterLines, formatLine } from "./log-viewer";
import { applyEvent, withDetails, type JobEvent, type LogLine } from "./use-job-events";

const BASE = { id: "job-1", repo_id: "repo-1", created_at: "2026-10-20T12:00:00Z" };

function queued(): IndexJob {
  return jobAt(BASE, 0);
}

const event = (fields: Partial<JobEvent> & Pick<JobEvent, "type">): JobEvent => ({
  id: "1-0",
  stage: null,
  ts: "2026-10-20T12:00:01Z",
  ...fields,
});

describe("applying the event stream", () => {
  it("walks a job through its stages to the end", () => {
    let job = queued();
    job = applyEvent(job, event({ type: "stage_started", stage: "clone", progress: 0 }));
    expect([job.status, job.stage]).toEqual(["running", "clone"]);
    job = applyEvent(
      job,
      event({ type: "progress", stage: "clone", done: 2, total: 5, eta_s: 7, progress: 0.02 }),
    );
    expect(job.stages.clone).toMatchObject({ status: "running", done: 2, total: 5 });
    expect(job.eta_s).toBe(7);
    job = applyEvent(job, event({ type: "stage_done", stage: "clone" }));
    expect(job.stages.clone).toMatchObject({ status: "done" });
    job = applyEvent(job, event({ type: "unlock", stage: "discover", unlocks: ["code_browser"] }));
    job = applyEvent(job, event({ type: "unlock", stage: "discover", unlocks: ["code_browser"] }));
    expect(job.features).toEqual(["code_browser"]); // never twice
    job = applyEvent(job, event({ type: "done", stage: "finalize" }));
    expect([job.status, job.progress, job.eta_s]).toEqual(["succeeded", 1, 0]);
  });

  it("records a failure on the stage and the job", () => {
    let job = applyEvent(queued(), event({ type: "stage_started", stage: "clone" }));
    job = applyEvent(
      job,
      event({ type: "failed", stage: "clone", code: "repo_too_large", message: "Too big." }),
    );
    expect(job.status).toBe("failed");
    expect(job.error).toMatchObject({
      stage: "clone",
      code: "repo_too_large",
      message: "Too big.",
    });
    expect(job.stages.clone).toMatchObject({ status: "failed" });
  });

  it("cancels the running stage", () => {
    let job = applyEvent(queued(), event({ type: "stage_started", stage: "discover" }));
    job = applyEvent(job, event({ type: "cancelled", stage: "discover" }));
    expect([job.status, (job.stages.discover as { status: string }).status]).toEqual([
      "cancelled",
      "cancelled",
    ]);
  });

  it("leaves a finished run as it is while its history replays", () => {
    const finished = jobAt(BASE, Number.MAX_SAFE_INTEGER);
    for (const recorded of runEvents()) {
      const after = applyEvent(
        finished,
        event({ ...(recorded as Partial<JobEvent>), type: recorded.type as JobEvent["type"] }),
      );
      expect(after).toBe(finished);
    }
  });

  it("doesn't restart a stage the job has already done, or move progress back", () => {
    let job = applyEvent(queued(), event({ type: "stage_started", stage: "clone", progress: 0 }));
    job = applyEvent(job, event({ type: "stage_done", stage: "clone", progress: 0.1 }));
    job = applyEvent(job, event({ type: "stage_started", stage: "discover", progress: 0.1 }));
    // The history from the start, replayed over the job fetched mid-run:
    const replayed = [
      event({ type: "stage_started", stage: "clone", progress: 0 }),
      event({ type: "progress", stage: "clone", done: 1, total: 5, progress: 0.02 }),
    ].reduce(applyEvent, job);
    expect(replayed.stages.clone).toMatchObject({ status: "done" });
    expect([replayed.stage, replayed.progress]).toEqual(["discover", 0.1]);
  });

  it("adds stage details from a refetch without undoing what the stream has since", () => {
    let job = applyEvent(queued(), event({ type: "stage_started", stage: "discover" }));
    job = applyEvent(job, event({ type: "stage_done", stage: "discover" }));
    // The refetch went out at stage_done; the next stage started before it came back.
    const fetched = structuredClone(job);
    (fetched.stages as Record<string, Record<string, unknown>>).discover = {
      status: "done",
      details: { files: 245 },
    };
    job = applyEvent(job, event({ type: "stage_started", stage: "merkle" }));
    const merged = withDetails(job, fetched);
    expect([merged.status, merged.stage]).toEqual(["running", "merkle"]);
    expect(merged.stages.merkle).toMatchObject({ status: "running" });
    expect(merged.stages.discover).toMatchObject({ status: "done", details: { files: 245 } });
  });

  it("matches what the recorded stream ends with", () => {
    // Folding every recorded event gives the same job the mock API reports at the end.
    let job = queued();
    for (const recorded of runEvents()) {
      job = applyEvent(
        job,
        event({
          ...(recorded as Partial<JobEvent>),
          type: recorded.type as JobEvent["type"],
          stage: recorded.stage,
        }),
      );
    }
    const final = jobAt(BASE, Number.MAX_SAFE_INTEGER);
    expect(job.status).toBe(final.status);
    expect(job.features).toEqual(final.features);
    expect(Object.keys(job.stages).sort()).toEqual(
      Object.entries(final.stages)
        .filter(([, entry]) => (entry as { status: string }).status !== "skipped")
        .map(([stage]) => stage)
        .sort(),
    );
  });
});

describe("the log viewer", () => {
  const lines: LogLine[] = [
    {
      id: "1",
      ts: "2026-10-20T12:00:01Z",
      level: "info",
      stage: "clone",
      message: "Checked out abc.",
    },
    {
      id: "2",
      ts: "2026-10-20T12:00:02Z",
      level: "warn",
      stage: "clone",
      message: "1 submodule not fetched.",
    },
    { id: "3", ts: "2026-10-20T12:00:03Z", level: "error", stage: null, message: "Clone failed." },
  ];

  it("filters by level and text", () => {
    expect(filterLines(lines, ["info", "warn", "error"], "").map((l) => l.id)).toEqual([
      "1",
      "2",
      "3",
    ]);
    expect(filterLines(lines, ["warn", "error"], "").map((l) => l.id)).toEqual(["2", "3"]);
    expect(filterLines(lines, ["info", "warn", "error"], "SUBMODULE").map((l) => l.id)).toEqual([
      "2",
    ]);
  });

  it("copies lines in a readable form", () => {
    const failed = lines.at(-1);
    if (!failed) throw new Error("no lines");
    expect(formatLine(failed)).toBe("2026-10-20T12:00:03Z ERROR [job] Clone failed.");
  });
});
