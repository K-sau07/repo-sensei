import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.describe("org and account lifecycle", () => {
  test("an owner deletes the org, then restores it from their profile", async ({ page }) => {
    await page.goto("/acme/settings/danger");
    await expect(page.getByRole("heading", { name: "Hand on ownership" })).toBeVisible();
    const { violations } = await new AxeBuilder({ page }).analyze();
    expect(violations.map((v) => v.id)).toEqual([]);
    await page.getByRole("button", { name: "Delete Acme" }).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByLabel("The organization's name in URLs").fill("acme");
    await dialog.getByRole("button", { name: "Delete" }).click();

    await expect(page).toHaveURL("/settings/profile");
    await page.getByRole("button", { name: "Restore Acme" }).click();
    await expect(page.getByRole("button", { name: "Restore Acme" })).toBeHidden();
    await page.goto("/acme/settings/general");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  });

  test("the only owner of an org is told to hand it on before deleting the account", async ({
    page,
  }) => {
    await page.goto("/settings/profile");
    await page.getByRole("button", { name: "Delete my account" }).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByLabel("Your GitHub login").fill("octo");
    await dialog.getByRole("button", { name: "Delete my account" }).click();
    const said = page.getByRole("alert").filter({ hasText: "only owner" });
    await expect(said).toContainText("You're the only owner of acme");
    await said.getByRole("link", { name: "acme" }).click();
    await expect(page).toHaveURL("/acme/settings/danger");
  });
});
