import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, screen, userEvent, waitFor, within } from "storybook/test";

import { AdminPage } from "@/components/internal/admin-page";
import { resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";
import { adminJobs, resetAdminMocks } from "@/mocks/handlers/admin";

const LOAD_MS = 10_000;

const meta = {
  title: "Internal/Admin",
  component: AdminPage,
  parameters: {
    nextjs: { appDirectory: true, navigation: { pathname: "/internal/admin" } },
    msw: handlers,
  },
  beforeEach: () => {
    resetMockDb();
    resetAdminMocks();
  },
} satisfies Meta<typeof AdminPage>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Today, failed runs across orgs (one retried), the orgs and the people. */
export const Admin: Story = {
  play: async ({ canvas }) => {
    const spent = await canvas.findByRole("region", { name: "Spent today" }, { timeout: LOAD_MS });
    await expect(spent).toHaveTextContent("$12.40");
    const runs = await canvas.findByRole("table", { name: "Index runs" });
    await expect(within(runs).getAllByTestId("admin-run")).toHaveLength(2);
    await userEvent.click(within(runs).getByRole("button", { name: "Retry acme-gh/api" }));
    // A new run, queued (the address-kept filter is the e2e test's: Storybook's router is a mock).
    await waitFor(() =>
      expect(
        adminJobs()
          .filter((j) => j.status === "queued")
          .map((j) => j.repo_slug),
      ).toEqual(["api"]),
    );
    await expect(await canvas.findByRole("table", { name: "Orgs" })).toHaveTextContent("Acme");
    await expect(canvas.getByRole("table", { name: "People" })).toHaveTextContent("@octo");
  },
};

/** Stopping new spending asks first, then says so until it's allowed again. */
export const ReadOnly: Story = {
  play: async ({ canvas }) => {
    await userEvent.click(
      await canvas.findByRole("button", { name: "Stop new spending" }, { timeout: LOAD_MS }),
    );
    const dialog = await screen.findByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Stop new spending" }));
    await waitFor(() => expect(canvas.getByText("Read-only")).toBeVisible());
    await userEvent.click(canvas.getByRole("button", { name: "Allow new spending" }));
    await waitFor(() => expect(canvas.queryByText("Read-only")).toBeNull());
  },
};
