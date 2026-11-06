import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

const PHONE = { width: 390, height: 844 };
const TABLET = { width: 834, height: 1112 };

const overflow = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);

test.describe("on a phone", () => {
  test.use({ viewport: PHONE });

  test("a trace is a list, and nothing is wider than the screen", async ({ page }) => {
    await page.goto("/acme/api/trace?from=frontend%2Fsrc%2Froutes%2Flogin.tsx");
    await expect(page.getByTestId("trace-view")).toBeVisible();
    await expect(page.getByTestId("trace-swimlane")).toHaveCount(0);
    await expect(page.getByRole("group", { name: "View" })).toHaveCount(0);
    await expect(page.getByRole("list").filter({ hasText: "Login" }).first()).toBeVisible();
    expect(await overflow(page)).toBe(0);
  });

  test("a citation's code comes up from the bottom", async ({ page }) => {
    await page.goto("/acme/api/ask");
    const box = page.getByRole("textbox", { name: "Ask about this repository" });
    await box.fill("How is an item created?");
    await box.press("ControlOrMeta+Enter");
    const chip = page.getByTestId("answer").getByTestId("citation-chip").first();
    await chip.click();
    await page.getByRole("button", { name: "Open the cited code" }).click();
    const sheet = page.getByRole("dialog", { name: "Cited code" });
    await expect(sheet).toBeVisible();
    await expect(sheet).toContainText("backend/app/api/routes/items.py");
    await page.keyboard.press("Escape");
    await expect(sheet).toHaveCount(0);
    expect(await overflow(page)).toBe(0);
  });
});

test.describe("on a tablet", () => {
  test.use({ viewport: TABLET });

  test("the file tree is a drawer that closes once a file opens", async ({ page }) => {
    await page.goto("/acme/api/code/backend/app/main.py");
    await expect(page.getByRole("tree")).toHaveCount(0);
    await page.getByRole("button", { name: "Files" }).click();
    const drawer = page.getByRole("dialog", { name: "Files" });
    await drawer.getByRole("treeitem", { name: "README.md" }).last().click();
    await expect(page).toHaveURL(/\/code\/(?:.+\/)?README\.md$/);
    await expect(drawer).toHaveCount(0);
    expect(await overflow(page)).toBe(0);
  });

  test("Ask is on the bottom bar only, not also floating over it", async ({ page }) => {
    await page.goto("/acme/api");
    await expect(page.getByRole("navigation", { name: "Quick" })).toBeVisible();
    await expect(
      page.getByRole("button", { name: /Ask about this repository|Ask ⌘L/ }),
    ).toHaveCount(0);
  });
});

test.describe("on a tablet, the right rail", () => {
  test.use({ viewport: { width: 834, height: 1112 } });

  test("opens as a drawer from the top bar", async ({ page }) => {
    await page.goto("/acme/api/code/backend/app/main.py");
    await page.getByRole("button", { name: "Symbol" }).click();
    const drawer = page.getByRole("dialog", { name: "Symbol" });
    await expect(drawer).toBeVisible();
    await expect(drawer.getByText(/Outline/).first()).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);
  });
});
