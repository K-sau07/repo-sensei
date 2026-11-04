import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { SlackSettings } from "@/components/orgs/slack-settings";
import { db, resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";

const nextjs = {
  appDirectory: true,
  navigation: { pathname: "/acme/settings/integrations", segments: [["org", "acme"]] },
};
const HOOK = "https://hooks.slack.com/services/T0001/B0002/abcDEF1234";

const meta = {
  title: "Settings/Slack",
  component: SlackSettings,
  parameters: { nextjs, msw: handlers },
  beforeEach: () => {
    resetMockDb(); // the stories connect and disconnect it
  },
} satisfies Meta<typeof SlackSettings>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Not connected: only Slack's own webhooks are taken; what it posts starts at the approvals,
 * pull requests and stops. Connected, it shows only the webhook's end. */
export const Connect: Story = {
  play: async ({ canvas }) => {
    const url = await canvas.findByLabelText("Incoming webhook URL");
    const connect = canvas.getByRole("button", { name: "Connect Slack" });
    await expect(connect).toBeDisabled();
    await userEvent.type(url, "https://evil.example/hook");
    await expect(canvas.getByText(/That isn't a Slack incoming webhook/)).toBeVisible();
    await expect(connect).toBeDisabled();
    await userEvent.clear(url);
    await userEvent.type(url, HOOK);
    await userEvent.type(canvas.getByLabelText("Channel name (to show here)"), "#eng-alerts");
    await expect(
      canvas.getByRole("checkbox", { name: "Plans waiting for approval" }),
    ).toBeChecked();
    await expect(canvas.getByRole("checkbox", { name: "Repositories indexed" })).not.toBeChecked();
    await userEvent.click(canvas.getByRole("checkbox", { name: "Indexing that failed" }));
    await userEvent.click(connect);
    const connected = await canvas.findByTestId("slack-connected");
    await expect(connected).toHaveTextContent("Connected to #eng-alerts");
    await expect(connected).toHaveTextContent("…1234");
    await expect(canvas.queryByText(HOOK)).toBeNull();
    await expect(db.slack?.acme?.kinds).toEqual([
      "approval_needed", "fix_failed", "index_failed", "pr_opened",
    ]); // prettier-ignore
  },
};

/** Connected: what it posts changes without a new webhook; disconnecting asks first. */
export const Disconnect: Story = {
  beforeEach: () => {
    db.slack = { acme: { webhook_url: HOOK, channel: "#eng", kinds: ["approval_needed"] } };
  },
  play: async ({ canvas, canvasElement }) => {
    await expect(await canvas.findByTestId("slack-connected")).toHaveTextContent("#eng");
    await userEvent.click(canvas.getByRole("checkbox", { name: "Pull requests opened" }));
    await userEvent.click(canvas.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(db.slack?.acme?.kinds).toEqual(["approval_needed", "pr_opened"]));
    await expect(db.slack?.acme?.webhook_url).toBe(HOOK);
    // Saved, the form starts again from what was saved: nothing left to save (and Disconnect,
    // held while saving, is back).
    await waitFor(async () => {
      await expect(canvas.getByRole("button", { name: "Save" })).toBeDisabled();
      await expect(canvas.getByRole("button", { name: "Disconnect Slack" })).toBeEnabled();
    });
    await userEvent.click(canvas.getByRole("button", { name: "Disconnect Slack" }));
    const dialog = await within(canvasElement.ownerDocument.body).findByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    await expect(await canvas.findByRole("button", { name: "Connect Slack" })).toBeVisible();
    await expect(db.slack?.acme).toBeUndefined();
  },
};
