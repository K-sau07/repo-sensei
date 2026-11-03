import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { SecuritySettings } from "@/components/orgs/security-settings";
import { db, OCTO, resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";

const meta = {
  title: "Settings/Security",
  component: SecuritySettings,
  parameters: {
    nextjs: {
      appDirectory: true,
      navigation: { pathname: "/acme/settings/security", segments: [["org", "acme"]] },
    },
    msw: handlers,
  },
  beforeEach: () => {
    resetMockDb();
  },
} satisfies Meta<typeof SecuritySettings>;

export default meta;
type Story = StoryObj<typeof meta>;

const rows = (root: HTMLElement) =>
  within(within(root).getByRole("table", { name: "Audit log" }))
    .getAllByRole("row")
    .slice(1);

/** The log, newest first, in words; filtered by what was done and by whom. */
export const AuditLog: Story = {
  play: async ({ canvas, canvasElement }) => {
    await canvas.findByRole("table", { name: "Audit log" });
    await expect(rows(canvasElement)).toHaveLength(7);
    await expect(rows(canvasElement)[0]).toHaveTextContent("Changed a repository's settings");
    await expect(rows(canvasElement)[1]).toHaveTextContent("member → admin");
    await expect(rows(canvasElement)[3]).toHaveTextContent("repo-sensei");
    const body = within(canvasElement.ownerDocument.body);
    await userEvent.click(canvas.getByRole("combobox", { name: "Who did it" }));
    await userEvent.click(await body.findByRole("option", { name: "@hubot" }));
    await waitFor(async () => {
      await expect(rows(canvasElement)).toHaveLength(1);
    });
    await expect(rows(canvasElement)[0]).toHaveTextContent("Created an API key");
    await userEvent.click(canvas.getByRole("combobox", { name: "What was done" }));
    await userEvent.click(await body.findByRole("option", { name: "Connected a repository" }));
    await expect(await canvas.findByText("Nothing recorded")).toBeVisible();
  },
};

/** Below admin: the log isn't theirs to read. */
export const Member: Story = {
  beforeEach: () => {
    const me = db.members.acme?.find((m) => m.user_id === OCTO);
    if (me) me.role = "member";
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText(/Only the organization's admins/)).toBeVisible();
  },
};
