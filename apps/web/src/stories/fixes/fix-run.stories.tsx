import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, within } from "storybook/test";

import { FixPage } from "@/components/fixes/fix-page";
import { handlers } from "@/mocks/handlers";

const id = (n: number) => `0199f000-0000-7000-8000-00000000f${String(n).padStart(3, "0")}`;
const LOAD_MS = 10_000;

const at = (fixId: string) => ({
  appDirectory: true,
  navigation: {
    pathname: `/acme/api/fixes/${fixId}`,
    segments: [
      ["org", "acme"],
      ["repo", "api"],
      ["fixId", fixId],
    ],
  },
});

const meta = {
  title: "Fixes/Run page",
  component: FixPage,
  parameters: { msw: handlers },
} satisfies Meta<typeof FixPage>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Waiting for approval: the card in its order, the plan's actions, the stages so far. */
export const AwaitingApproval: Story = {
  parameters: { nextjs: at(id(1)) },
  play: async ({ canvas }) => {
    const card = await canvas.findByTestId("approval-card", {}, { timeout: LOAD_MS });
    await expect(within(card).getByTestId("root-cause")).toBeVisible();
    await expect(within(card).getByTestId("repro-evidence")).toBeVisible();
    await expect(within(card).getByTestId("risk-panel")).toBeVisible();
    await expect(await within(card).findByRole("button", { name: /^Approve plan/ })).toBeEnabled();
    const stages = canvas.getByTestId("run-stages");
    await expect(within(stages).getByText("Approve").closest("li")).toHaveAttribute(
      "data-state",
      "current",
    );
  },
};

/** Not reproduced: the attempts, and a retry with more context; nothing to approve. */
export const NotReproduced: Story = {
  parameters: { nextjs: at(id(3)) },
  play: async ({ canvas }) => {
    const ended = await canvas.findByTestId("ended", {}, { timeout: LOAD_MS });
    await expect(within(ended).getByTestId("attempts")).toBeInTheDocument(); // closed, to open
    await expect(canvas.queryByTestId("approval-card")).toBeNull();
  },
};

/** Blocked by an anti-gaming rule: which, and where. */
export const Blocked: Story = {
  parameters: { nextjs: at(id(6)) },
  play: async ({ canvas }) => {
    const hits = await canvas.findByTestId("rule-hits", {}, { timeout: LOAD_MS });
    await expect(hits).toHaveTextContent("An error is caught instead of fixed");
  },
};
