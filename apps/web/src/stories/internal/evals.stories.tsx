import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, within } from "storybook/test";

import { EvalRunPage } from "@/components/internal/eval-run-page";
import { EvalsList } from "@/components/internal/evals-list";
import { resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";
import { EVAL_RUNS } from "@/mocks/handlers/internal";

const LOAD_MS = 10_000;

const meta = {
  title: "Internal/Evals",
  parameters: {
    nextjs: { appDirectory: true, navigation: { pathname: "/internal" } },
    msw: handlers,
  },
  beforeEach: () => {
    resetMockDb();
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const latest = EVAL_RUNS.at(-1)?.id ?? "";
const bench = EVAL_RUNS.find((r) => r.metrics.bench && r.created_at.includes("-16T"))?.id ?? "";

/** Every stored run, newest first, each against its baseline. */
export const Runs: Story = {
  render: () => <EvalsList />,
  play: async ({ canvas }) => {
    const table = await canvas.findByRole("table", { name: "Eval runs" }, { timeout: LOAD_MS });
    const rows = within(table).getAllByTestId("eval-run");
    await expect(rows).toHaveLength(EVAL_RUNS.length);
    await expect(rows[0]).toHaveTextContent("3 regressions");
    await expect(rows.at(-1)).toHaveTextContent("No baseline");
  },
};

/** A retrieval run that got worse: recall fell and it cost more; the item that fell first. */
export const WorseRun: Story = {
  render: () => <EvalRunPage run={latest} />,
  play: async ({ canvas }) => {
    const metrics = await canvas.findByRole(
      "table",
      { name: "Metrics against the baseline" },
      { timeout: LOAD_MS },
    );
    const cost = within(metrics).getByText("cost_usd").closest("tr");
    await expect(cost).toHaveTextContent("+$0.20");
    await expect(cost).toHaveTextContent("Worse");
    const first = canvas.getAllByTestId("eval-item")[0];
    await expect(first).toHaveTextContent("create-item");
    await expect(first).toHaveTextContent("Worse");
  },
};

/** A CrossLayerBench flow's traced path, each hop matched to the gold. */
export const BenchPath: Story = {
  render: () => <EvalRunPage run={bench} />,
  play: async ({ canvas }) => {
    const path = await canvas.findByRole("list", { name: "The traced path" }, { timeout: LOAD_MS });
    await expect(within(path).getAllByRole("listitem")).toHaveLength(4);
    await expect(path).toHaveTextContent("gold hop 4");
  },
};
