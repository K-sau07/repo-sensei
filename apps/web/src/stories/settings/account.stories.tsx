import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { ProfileView } from "@/components/settings/profile-view";
import { db, HUBOT, OCTO, resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";

const meta = {
  title: "Settings/Account",
  component: ProfileView,
  parameters: {
    nextjs: { appDirectory: true, navigation: { pathname: "/settings/profile" } },
    msw: handlers,
  },
  beforeEach: () => {
    resetMockDb(); // the stories restore orgs and delete the account
  },
} satisfies Meta<typeof ProfileView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** An org deleted a day ago: restorable until its purge, and back with one click. */
export const Restore: Story = {
  beforeEach: () => {
    const acme = db.orgs.find((org) => org.slug === "acme");
    if (!acme) throw new Error("the mock seed has Acme");
    db.deletedOrgs = {
      acme: { org: acme, members: db.members.acme ?? [], deleted_at: "2026-10-17T12:00:00Z" },
    };
    db.orgs = db.orgs.filter((org) => org.slug !== "acme");
  },
  play: async ({ canvas }) => {
    const restore = await canvas.findByRole("button", { name: "Restore Acme" });
    await expect(canvas.getByText(/purged on Oct 24, 2026/)).toBeVisible();
    await userEvent.click(restore);
    await waitFor(async () => {
      await expect(db.orgs.some((org) => org.slug === "acme")).toBe(true);
    });
    await waitFor(async () => {
      await expect(canvas.queryByRole("button", { name: "Restore Acme" })).toBeNull();
    });
  },
};

/** The only owner of an org can't delete their account: the org is named, to hand it on. */
export const SoleOwner: Story = {
  play: async ({ canvas, canvasElement }) => {
    await userEvent.click(await canvas.findByRole("button", { name: "Delete my account" }));
    const dialog = within(canvasElement.ownerDocument.body).getByRole("alertdialog");
    await userEvent.type(within(dialog).getByLabelText("Your GitHub login"), "octo");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete my account" }));
    const said = await canvas.findByRole("alert");
    await expect(said).toHaveTextContent("You're the only owner of acme");
    await expect(within(said).getByRole("link", { name: "acme" })).toHaveAttribute(
      "href",
      "/acme/settings/danger",
    );
    await expect(db.users.find((u) => u.id === OCTO)?.deleted).toBeUndefined();
  },
};

/** With another owner, the account goes. */
export const Deleted: Story = {
  beforeEach: () => {
    const hubot = db.members.acme?.find((m) => m.user_id === HUBOT);
    if (hubot) hubot.role = "owner";
  },
  play: async ({ canvas, canvasElement }) => {
    await userEvent.click(await canvas.findByRole("button", { name: "Delete my account" }));
    const dialog = within(canvasElement.ownerDocument.body).getByRole("alertdialog");
    const go = within(dialog).getByRole("button", { name: "Delete my account" });
    await expect(go).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Your GitHub login"), "OCTO");
    await userEvent.click(go);
    await waitFor(async () => {
      await expect(db.users.find((u) => u.id === OCTO)?.deleted).toBe(true);
    });
    await expect(db.members.acme?.some((m) => m.user_id === OCTO)).toBe(false);
  },
};
