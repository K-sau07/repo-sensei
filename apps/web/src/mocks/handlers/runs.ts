/**
 * The mock run endpoints (4.12): the repository's runs, a run's detail, and its steps as a
 * stream (all of them and its end, for a finished run).
 *
 * Situations worth seeing, from the browser:
 *   localStorage.setItem("sensei.mock.runs", "live")
 * "live": the run is under way, its steps arriving one by one (every 150 ms).
 */
import { delay, http, HttpResponse } from "msw";

import { apiError } from "@/mocks/handlers/errors";
import { MOCK_RUN_ID, mockRun, mockRunSteps } from "@/mocks/runs";

const RUNS_SCENARIO_KEY = "sensei.mock.runs";
const LIVE_STEP_MS = 150;
const LIVE_FIRST = 6; // the steps a live run has when the page opens

function live(): boolean {
  return typeof localStorage !== "undefined" && localStorage.getItem(RUNS_SCENARIO_KEY) === "live";
}

function detail() {
  const steps = mockRunSteps();
  return live() ? mockRun(steps.slice(0, LIVE_FIRST), "running") : mockRun(steps);
}

export const runsHandlers = [
  http.get("/api/v1/orgs/:org/repos/:repo/runs", () => {
    const { steps: _, ...run } = detail();
    return HttpResponse.json({ runs: [run], next_cursor: null });
  }),
  http.get("/api/v1/orgs/:org/agent-runs/:id", ({ params }) => {
    if (params.id !== MOCK_RUN_ID) return apiError(404, "run_not_found", "Run not found.");
    return HttpResponse.json(detail());
  }),
  http.get("/api/v1/orgs/:org/agent-runs/:id/events", ({ params }) => {
    if (params.id !== MOCK_RUN_ID) return apiError(404, "run_not_found", "Run not found.");
    const steps = mockRunSteps();
    const going = live();
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      async start(controller) {
        for (const step of steps) {
          const data = { type: "step", ...step };
          controller.enqueue(encoder.encode(`event: step\ndata: ${JSON.stringify(data)}\n\n`));
          if (going && step.idx >= LIVE_FIRST) await delay(LIVE_STEP_MS);
        }
        const done = { type: "finished", status: "succeeded" };
        controller.enqueue(encoder.encode(`event: finished\ndata: ${JSON.stringify(done)}\n\n`));
        controller.close();
      },
    });
    return new HttpResponse(stream, { headers: { "content-type": "text/event-stream" } });
  }),
];
