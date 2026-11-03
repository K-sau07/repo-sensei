import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { OrgDangerZone } from "@/components/orgs/danger-zone";
import { db, HUBOT, OCTO, resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";

const nextjs = {
  appDirectory: true,
  navigation: { pathname: "/acme/settings/danger", segments: [["org", "acme"]] },
};

const meta = {
  title: "Settings/Danger zone",
  component: OrgDangerZone,
  parameters: { nextjs, msw: handlers },
  beforeEach: () => {
    resetMockDb(); // the stories hand the org on and delete it
  },
} satisfies Meta<typeof OrgDangerZone>;

export default meta;
type Story = StoryObj<typeof meta>;

const roleOf = (user: string) => db.members.acme?.find((m) => m.user_id === user)?.role;

/** The owner hands the org to a member, and steps down to admin. */
export const HandOn: Story = {
  play: async ({ canvas, canvasElement }) => {
    const make = await canvas.findByRole("button", { name: "Make owner" });
    await expect(make).toBeDisabled();
    await userEvent.click(canvas.getByRole("combobox", { name: "The new owner" }));
    await userEvent.click(
      await within(canvasElement.ownerDocument.body).findByRole("option", { name: /@hubot/ }),
    );
    await userEvent.click(make);
    await waitFor(async () => {
      await expect(roleOf(HUBOT)).toBe("owner");
      await expect(roleOf(OCTO)).toBe("admin");
    });
  },
};

/** Deleting asks for the org's name in URLs; it is then restorable, not gone. */
export const Delete: Story = {
  play: async ({ canvas, canvasElement }) => {
    await userEvent.click(await canvas.findByRole("button", { name: "Delete Acme" }));
    const dialog = within(canvasElement.ownerDocument.body).getByRole("alertdialog");
    const go = within(dialog).getByRole("button", { name: "Delete" });
    await expect(go).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("The organization's name in URLs"), "acme");
    await userEvent.click(go);
    await waitFor(async () => {
      await expect(db.deletedOrgs?.acme?.org.name).toBe("Acme");
    });
    await expect(db.orgs.some((org) => org.slug === "acme")).toBe(false);
  },
};

/** Only owners see more than a word here. */
export const NotOwner: Story = {
  beforeEach: () => {
    const me = db.members.acme?.find((m) => m.user_id === OCTO);
    if (me) me.role = "admin";
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText(/Only the organization's owners/)).toBeVisible();
    await expect(canvas.queryByRole("button", { name: "Delete Acme" })).toBeNull();
  },
};
