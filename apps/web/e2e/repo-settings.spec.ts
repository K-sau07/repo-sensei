import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.describe("repository settings", () => {
  test("an admin tracks a branch, keeps it after a reload, then disconnects", async ({ page }) => {
    await page.goto("/acme/api/settings");
    await expect(page.getByRole("heading", { name: "Settings", level: 1 })).toBeVisible();
    await page.getByLabel("A branch to track").fill("staging");
    await page.getByRole("button", { name: "Track", exact: true }).click();
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByRole("button", { name: "Save" })).toBeDisabled();
    await page.reload();
    await expect(page.getByRole("list", { name: "Tracked branches" })).toContainText("staging");
    const { violations } = await new AxeBuilder({ page }).analyze();
    expect(violations.map((v) => v.id)).toEqual([]);

    await page.getByRole("button", { name: "Disconnect acme-gh/api" }).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByLabel("The repository's name").fill("acme-gh/api");
    await dialog.getByRole("button", { name: "Disconnect" }).click();
    await expect(page).toHaveURL("/acme/repos");
  });
});
