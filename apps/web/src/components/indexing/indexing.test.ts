import { describe, expect, it } from "vitest";

import type { IndexJob } from "@/lib/api/types";
import { jobAt, runEvents } from "@/mocks/index-run";

import { filterLines, formatLine } from "./log-viewer";
import { applyEvent, type JobEvent, type LogLine } from "./use-job-events";

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
