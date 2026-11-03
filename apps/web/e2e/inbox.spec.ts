import { expect, test } from "./fixtures";

test.describe("the inbox", () => {
  test("the badge, triage from the keyboard, and an approval opened at its card", async ({
    page,
  }) => {
    await page.goto("/acme");
    const badge = page.getByRole("navigation", { name: "Main" }).getByTestId("inbox-badge");
    await expect(badge).toHaveText("3 unread");
    await page.keyboard.press("g");
    await page.keyboard.press("i");
    await expect(page).toHaveURL(/\/acme\/inbox$/);
    const inbox = page.getByTestId("inbox");
    await expect(inbox.getByTestId("inbox-item")).toHaveCount(3);
    // j, then e: done, and the badge follows.
    await page.keyboard.press("j");
    await page.keyboard.press("e");
    await expect(inbox.getByTestId("inbox-unread")).toHaveText("2 unread");
    await expect(badge).toHaveText("2 unread");
    // a: the plan's approval card.
    await expect(inbox.getByRole("link", { name: /Items page crashes/ })).toBeFocused();
    await page.keyboard.press("a");
    await expect(page).toHaveURL(/\/acme\/api\/fixes\/0199f000-0000-7000-8000-00000000f001$/);
    await expect(page.getByTestId("approval-card")).toBeVisible();
    await expect(badge).toHaveText("1 unread");
  });

  test("everything, a group at a time, kept in the URL", async ({ page }) => {
    await page.goto("/acme/inbox?show=all&group=finished");
    const inbox = page.getByTestId("inbox");
    await expect(inbox.getByTestId("inbox-item")).toHaveCount(2);
    await inbox.getByRole("button", { name: "Failures" }).click();
    await expect(page).toHaveURL(/group=failures/);
    await expect(inbox.getByTestId("inbox-item")).toHaveCount(2);
    // Read all of what's shown: the plans waiting elsewhere stay unread.
    await inbox.getByRole("button", { name: "Mark all read" }).click();
    await expect(inbox.getByTestId("inbox-unread")).toHaveText("2 unread");
    await inbox.getByRole("button", { name: "Unread" }).click();
    await expect(inbox.getByText("You're all caught up")).toBeVisible();
    await inbox.getByRole("button", { name: "Approvals" }).click();
    await expect(inbox.getByTestId("inbox-item")).toHaveCount(2);
  });

  test("preferences: per kind, saved as they're flipped", async ({ page }) => {
    await page.goto("/settings/notifications");
    const email = page.getByRole("switch", { name: "Indexing done: by email" });
    await expect(email).not.toBeChecked();
    await email.click();
    await expect(email).toBeChecked();
    await page.reload();
    await expect(page.getByRole("switch", { name: "Indexing done: by email" })).toBeChecked();
  });
});
