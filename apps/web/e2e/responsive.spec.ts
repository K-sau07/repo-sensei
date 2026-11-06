import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

const PHONE = { width: 390, height: 844 };
const TABLET = { width: 834, height: 1112 };

const overflow = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);

/** What reaches past the screen's right edge outside anything that scrolls or clips it (the
 * page itself scrolls in `main`, which a document-wide check can't see). */
const sticksOut = (page: Page) =>
  page.evaluate(() => {
    const width = window.innerWidth;
    const main = document.querySelector("main");
    const held = (el: Element) => {
      for (let up = el.parentElement; up; up = up.parentElement) {
        if (up === main || up === document.body) continue;
        if (["auto", "scroll", "hidden", "clip"].includes(getComputedStyle(up).overflowX))
          return true;
      }
      return false;
    };
    return [...document.querySelectorAll("body *")]
      .filter((el) => {
        const box = el.getBoundingClientRect();
        return (
          box.width > 0 &&
          box.height > 0 &&
          box.right > width + 1 &&
          !held(el) &&
          getComputedStyle(el).position !== "fixed"
        );
      })
      .slice(0, 3)
      .map((el) => `${el.tagName.toLowerCase()}.${el.getAttribute("class") ?? ""}`.slice(0, 80));
  });

const PHONE_PAGES = [
  "/acme", "/acme/inbox", "/acme/insights", "/acme/api", "/acme/api/ask", "/acme/api/fixes",
  "/acme/api/fixes/0199f000-0000-7000-8000-00000000f001", "/acme/api/index",
  "/acme/settings/general", "/acme/settings/models", "/acme/settings/mcp", "/acme/settings/usage",
  "/settings/profile", "/settings/appearance", "/pricing", "/docs",
]; // prettier-ignore

test.describe("on a phone", () => {
  test.use({ viewport: PHONE });

  for (const path of PHONE_PAGES) {
    test(`${path} fits the screen`, async ({ page }) => {
      await page.goto(path);
      await expect(page.locator("main, body").first()).toBeVisible();
      await page.waitForLoadState("networkidle");
      expect(await sticksOut(page)).toEqual([]);
    });
  }

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
