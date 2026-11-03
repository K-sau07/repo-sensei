import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { delay, http, HttpResponse } from "msw";
import { expect, userEvent, waitFor } from "storybook/test";

import { NotificationsView } from "@/components/settings/notifications-view";
import { db, OCTO, resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";

const meta = {
  title: "Settings/Notifications",
  component: NotificationsView,
  parameters: { msw: handlers },
  beforeEach: () => {
    resetMockDb(); // the story changes a preference
  },
} satisfies Meta<typeof NotificationsView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Per kind, in the inbox and by email (indexing done isn't emailed unless asked); a switch
 * saves as it is flipped. */
export const Preferences: Story = {
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("octo@example.com")).toBeVisible();
    const indexed = canvas.getByRole("switch", { name: "Indexing done: by email" });
    await expect(indexed).not.toBeChecked();
    await expect(canvas.getByRole("switch", { name: "Approvals needed: by email" })).toBeChecked();
    await userEvent.click(indexed);
    await waitFor(() => expect(indexed).toBeChecked());
    await waitFor(() => expect(db.notificationPrefs?.[OCTO]?.index_done?.email).toBe(true));
    const inbox = canvas.getByRole("switch", { name: "Pull requests opened: in the inbox" });
    await userEvent.click(inbox);
    await waitFor(() => expect(db.notificationPrefs?.[OCTO]?.pr_opened?.in_app).toBe(false));
  },
};

let patches = 0;

/** Two flips at once, the first refused: only it is turned back (the second keeps its). */
export const OneFlipRefused: Story = {
  parameters: {
    msw: [
      http.patch("*/api/v1/me/notifications", async () => {
        patches += 1;
        if (patches > 1) return; // the mock API's own handler saves it
        await delay(300); // answered after the second flip
        return HttpResponse.json(
          { error: { code: "unavailable", message: "Couldn't save that one." } },
          { status: 503 },
        );
      }),
      ...handlers,
    ],
  },
  beforeEach: () => {
    patches = 0;
  },
  play: async ({ canvas }) => {
    const indexed = await canvas.findByRole("switch", { name: "Indexing done: by email" });
    const opened = canvas.getByRole("switch", { name: "Pull requests opened: in the inbox" });
    await userEvent.click(indexed);
    await userEvent.click(opened);
    await expect(indexed).toBeChecked();
    await expect(opened).not.toBeChecked();
    await waitFor(() => expect(indexed).not.toBeChecked()); // refused: turned back
    await expect(opened).not.toBeChecked(); // saved: kept
    await waitFor(() => expect(db.notificationPrefs?.[OCTO]?.pr_opened?.in_app).toBe(false));
    await expect(db.notificationPrefs?.[OCTO]?.index_done?.email).toBe(false);
  },
};
