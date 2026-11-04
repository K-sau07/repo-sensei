import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, userEvent, within } from "storybook/test";

import { UsageSettings } from "@/components/orgs/usage-settings";
import { db, OCTO, resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";

const meta = {
  title: "Settings/Usage",
  component: UsageSettings,
  parameters: {
    nextjs: {
      appDirectory: true,
      navigation: { pathname: "/acme/settings/usage", segments: [["org", "acme"]] },
    },
    msw: handlers,
  },
  beforeEach: () => {
    resetMockDb();
  },
} satisfies Meta<typeof UsageSettings>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Today's spend (with what runs under way hold) against the day's cap, the month's against its
 * budget, a month of days (each in a table too), what it went on and by whom. */
export const Usage: Story = {
  play: async ({ canvas }) => {
    const today = await canvas.findByRole("region", { name: "Today's spend" });
    await expect(today).toHaveTextContent("$3.90 of $20.00");
    await expect(today).toHaveTextContent("$0.50 of it set aside for runs under way.");
    await expect(within(today).getByRole("meter")).toHaveAttribute("aria-valuenow", "20");
    const month = canvas.getByRole("region", { name: "This month" });
    await expect(month).toHaveTextContent("$18.25 of $25.00");
    await userEvent.click(canvas.getByText("Each day, as a table"));
    const days = canvas.getByRole("table", { name: "Spend each day" });
    await expect(within(days).getAllByRole("row")).toHaveLength(31);
    const models = canvas.getByRole("table", { name: "Spend by model" });
    await expect(models).toHaveTextContent("claude-sonnet-5-5");
    await expect(canvas.getByRole("table", { name: "Spend by person" })).toHaveTextContent(
      "No one (index runs, public pages)",
    );
  },
};

export const Member: Story = {
  beforeEach: () => {
    const me = db.members.acme?.find((m) => m.user_id === OCTO);
    if (me) me.role = "member";
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText(/Only the organization's admins/)).toBeVisible();
  },
};
