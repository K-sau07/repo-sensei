import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { delay, http, HttpResponse } from "msw";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { RunView } from "@/components/runs/run-view";
import { RunsList } from "@/components/runs/runs-list";
import { handlers } from "@/mocks/handlers";
import { MOCK_RUN_ID, mockRun, mockRunSteps } from "@/mocks/runs";

const navigation = (runId: string | null) => ({
  appDirectory: true,
  navigation: {
    pathname: runId ? `/acme/api/runs/${runId}` : "/acme/api/runs",
    segments: [["org", "acme"], ["repo", "api"], ...(runId ? [["runId", runId]] : [])],
  },
});

function Timeline() {
  return (
    <div className="flex h-[760px] flex-col overflow-hidden rounded-xl border border-line">
      <RunView />
    </div>
  );
}

const meta = {
  title: "Runs/Run timeline",
  component: Timeline,
  parameters: { nextjs: navigation(MOCK_RUN_ID), msw: handlers },
} satisfies Meta<typeof Timeline>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A recorded run of 40 steps: the waterfall, its steps aggregated, the first one open. */
export const Recorded: Story = {
  play: async ({ canvas }) => {
    await expect(
      await canvas.findByRole("heading", { name: "How is an item created?" }),
    ).toBeVisible();
    const rail = canvas.getByTestId("stage-rail");
    await expect(within(rail).getByText("Searching before the first turn")).toBeVisible();
    await expect(canvas.getByTestId("step-card")).toHaveTextContent("18 results in 7 files");
    const bars = within(canvas.getByTestId("waterfall")).getAllByRole("button");
    await expect(bars).toHaveLength(40);
  },
};

/** Expanded: every call a row of its own; a turn's calls side by side in the waterfall. */
export const Expanded: Story = {
  play: async ({ canvas }) => {
    const rail = await canvas.findByTestId("stage-rail");
    await expect(within(rail).getAllByText(/Reading files ×2/).length).toBeGreaterThan(0);
    await userEvent.click(canvas.getByRole("button", { name: "Expanded" }));
    await waitFor(async () => {
      await expect(within(rail).queryAllByText(/×\d/)).toHaveLength(0);
    });
    await expect(within(rail).getAllByText(/^Reading files · /).length).toBeGreaterThan(0);
  },
};

/** Time travel: a file picked opens the step that first read it, the later steps ghosted. */
export const TimeTravel: Story = {
  play: async ({ canvas }) => {
    const files = await canvas.findByTestId("files-read");
    const [first] = within(files).getAllByRole("button");
    if (!first) throw new Error("no file read");
    await userEvent.click(first);
    const card = canvas.getByTestId("step-card");
    await expect(card).toHaveTextContent(first.textContent);
    const slider = canvas.getByRole("slider", { name: "Replay to step" });
    await expect(Number(slider.getAttribute("aria-valuenow"))).toBeLessThan(39);
    const last = within(canvas.getByTestId("waterfall")).getAllByRole("button").at(-1);
    await expect(last).toHaveClass("opacity-20");
  },
};

/** A run under way: steps arrive as they are recorded; it ends, and reads its outputs. */
export const Live: Story = {
  parameters: {
    msw: [
      http.get("/api/v1/orgs/:org/agent-runs/:id", () =>
        HttpResponse.json(mockRun(mockRunSteps().slice(0, 6), "running")),
      ),
      http.get("/api/v1/orgs/:org/agent-runs/:id/events", () => {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          async start(controller) {
            for (const step of mockRunSteps().slice(6, 12)) {
              await delay(60);
              const data = JSON.stringify({ type: "step", ...step });
              controller.enqueue(encoder.encode(`event: step\ndata: ${data}\n\n`));
            }
            // Held open: the run goes on.
          },
        });
        return new HttpResponse(stream, { headers: { "content-type": "text/event-stream" } });
      }),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByTestId("live")).toHaveTextContent("Live");
    await waitFor(
      async () => {
        const bars = within(canvas.getByTestId("waterfall")).getAllByRole("button");
        await expect(bars).toHaveLength(12);
      },
      { timeout: 3000 },
    );
  },
};

/** A long run (400 steps): the step list draws the rows in view, not all of them. */
export const LongRun: Story = {
  parameters: {
    msw: [
      http.get("/api/v1/orgs/:org/agent-runs/:id", () =>
        HttpResponse.json(mockRun(mockRunSteps(400))),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await userEvent.click(await canvas.findByRole("button", { name: "Expanded" }));
    const rail = canvas.getByTestId("stage-rail");
    await waitFor(async () => {
      const rows = within(rail).getAllByRole("button").length;
      await expect(rows).toBeGreaterThan(5);
      await expect(rows).toBeLessThan(60);
    });
  },
};

/** The repository's runs. */
export const List: StoryObj = {
  parameters: { nextjs: navigation(null), msw: handlers },
  render: () => <RunsList />,
  play: async ({ canvas }) => {
    const link = await canvas.findByRole("link", { name: "How is an item created?" });
    await expect(link).toHaveAttribute("href", `/acme/api/runs/${MOCK_RUN_ID}`);
    await expect(canvas.getByText("$0.0832")).toBeVisible();
  },
};
