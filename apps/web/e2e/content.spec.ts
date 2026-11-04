import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

const PAGES = [
  ["/docs", "Docs"],
  ["/docs/getting-started", "Getting started"],
  ["/docs/mcp", "MCP in your editor"],
  ["/docs/security-model", "Security model"],
  ["/pricing", "Pricing"],
  ["/changelog", "Changelog"],
  ["/blog", "Blog"],
  ["/blog/how-an-answer-is-checked", "How an answer is checked"],
  ["/security", "Security"],
  ["/privacy", "Privacy"],
  ["/terms", "Terms"],
] as const;

test.describe("docs, blog and the site's other pages", () => {
  test.use({ signedIn: false });

  for (const [path, title] of PAGES) {
    test(`${path} opens for anyone, accessibly`, async ({ page }) => {
      const response = await page.goto(path);
      expect(response?.status()).toBe(200);
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(title);
      const { violations } = await new AxeBuilder({ page }).analyze();
      expect(violations.map((v) => v.id)).toEqual([]);
    });
  }

  test("a page that isn't a doc or a post is a 404", async ({ page }) => {
    expect((await page.goto("/docs/nothing-here"))?.status()).toBe(404);
    expect((await page.goto("/blog/nothing-here"))?.status()).toBe(404);
  });

  test("⌘K searches the docs by what a page covers", async ({ page }) => {
    await page.goto("/docs");
    const dialog = page.getByRole("dialog");
    // The shortcut is the page's once it has hydrated: pressed until it opens.
    await expect(async () => {
      await page.keyboard.press("ControlOrMeta+KeyK");
      await expect(dialog).toBeVisible({ timeout: 1_000 });
    }).toPass();
    await dialog.getByRole("combobox").fill("cursor");
    await dialog.getByRole("option", { name: /MCP in your editor/ }).click();
    await expect(page).toHaveURL("/docs/mcp");
    // The setups are this site's (its origin, localhost in development).
    await expect(page.getByText(/"url": "http:\/\/localhost:3000\/mcp"/).first()).toBeVisible();
    await expect(page.getByRole("link", { name: "MCP in your editor" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  test("the header and footer reach every public page", async ({ page }) => {
    await page.goto("/");
    const header = page.getByRole("navigation", { name: "Site" });
    await header.getByRole("link", { name: "Pricing" }).click();
    await expect(page).toHaveURL("/pricing");
    const footer = page.getByRole("navigation", { name: "Footer" });
    for (const name of ["Blog", "Changelog", "Security", "Privacy", "Terms"]) {
      await expect(footer.getByRole("link", { name })).toBeVisible();
    }
    await footer.getByRole("link", { name: "Changelog" }).click();
    await expect(page.getByRole("heading", { name: "Foundations" })).toBeVisible();
  });
});

test("signed in, the app's ⌘K finds the docs too", async ({ page }) => {
  await page.goto("/acme");
  await expect(page.locator("html")).toHaveAttribute("data-shell-ready", "true");
  await page.keyboard.press("ControlOrMeta+KeyK");
  await page.keyboard.type("security model");
  await page
    .getByRole("option", { name: /Security model/ })
    .first()
    .click();
  await expect(page).toHaveURL("/docs/security-model");
});
