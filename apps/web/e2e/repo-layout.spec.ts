import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.describe("repository layout", () => {
  test("header, tabs and locks", async ({ page }) => {
    await page.goto("/acme/api");
    await expect(page.getByRole("heading", { level: 1, name: "api" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Branch: main/ })).toBeVisible();
    const tabs = page.getByRole("navigation", { name: "Repository" });
    await expect(tabs.getByRole("link", { name: "Overview" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    // Code is unlocked by the last index; Ask waits for a later phase.
    await expect(tabs.getByRole("link", { name: "Code" })).not.toContainText("locked");
    const ask = tabs.getByRole("link", { name: /Ask/ });
    await expect(ask).toContainText("locked: Arrives in phase 4");
    await ask.hover();
    await expect(page.locator("[data-slot=tooltip-content]")).toContainText("Arrives in phase 4");
    await ask.click();
    await expect(page).toHaveURL(/\/acme\/api\/ask$/);
    await expect(page.getByText("Ask is on its way")).toBeVisible();

    const { violations } = await new AxeBuilder({ page }).analyze();
    expect(violations.map((v) => v.id)).toEqual([]);
  });

  test("g c goes to the code tab and the More menu reaches the rest", async ({ page }) => {
    await page.goto("/acme/api");
    await expect(page.getByRole("heading", { level: 1, name: "api" })).toBeVisible();
    await page.keyboard.press("g");
    await page.keyboard.press("c");
    await expect(page).toHaveURL(/\/acme\/api\/code$/);
    await page.getByRole("button", { name: "More" }).click();
    await page.getByRole("menuitem", { name: "Index" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/index$/);
    await expect(page.getByRole("button", { name: "Index" })).toBeVisible(); // the menu names it
  });

  test("an unknown repository is not found", async ({ page }) => {
    await page.goto("/acme/nope");
    await expect(page.getByText("Repository not found")).toBeVisible();
  });
});
