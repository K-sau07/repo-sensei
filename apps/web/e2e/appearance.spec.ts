import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.describe("appearance and shortcuts", () => {
  test("a chosen accent, density and transparency apply at once and survive a reload", async ({
    page,
  }) => {
    await page.goto("/settings/appearance");
    await expect(page.getByRole("heading", { name: "Accent" })).toBeVisible();
    const { violations } = await new AxeBuilder({ page }).analyze();
    expect(violations.map((v) => v.id)).toEqual([]);

    await page.getByRole("radio", { name: "Green" }).check();
    await page.getByRole("radio", { name: /Compact/ }).check();
    await page.getByRole("switch", { name: "Reduce transparency" }).check();
    const html = page.locator("html");
    await expect(html).toHaveAttribute("data-density", "compact");
    await expect(html).toHaveAttribute("data-transparency", "reduce");
    await expect(page.locator("style#sensei-appearance")).toBeAttached();

    await page.reload();
    await expect(html).toHaveAttribute("data-density", "compact");
    await expect(page.locator("style#sensei-appearance")).toBeAttached();
    await expect(page.getByRole("radio", { name: "Green" })).toBeChecked();

    await page.getByRole("button", { name: "Back to the defaults" }).click();
    await expect(html).toHaveAttribute("data-density", "comfortable");
    await expect(page.locator("style#sensei-appearance")).not.toBeAttached();
  });

  test("the shortcuts page finds a shortcut by what it does", async ({ page }) => {
    await page.goto("/settings/shortcuts");
    await page.getByLabel("Find a shortcut").fill("palette");
    await expect(page.getByText(/command palette/i).first()).toBeVisible();
    const { violations } = await new AxeBuilder({ page }).analyze();
    expect(violations.map((v) => v.id)).toEqual([]);
  });
});
