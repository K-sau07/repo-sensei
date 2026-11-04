import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

const PAGES = [
  ["/product", "Product"],
  ["/product/understand", "Understand"],
  ["/product/trace", "Trace"],
  ["/product/diagrams", "Diagrams"],
  ["/product/fix", "Fix"],
  ["/product/mcp", "MCP"],
] as const;

test.describe("the product pages", () => {
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

  test("the site's header leads from the landing page to each one", async ({ page }) => {
    await page.goto("/");
    await page
      .getByRole("navigation", { name: "Site" })
      .getByRole("link", { name: "Product" })
      .click();
    await expect(page).toHaveURL("/product");
    await page.getByRole("link", { name: /^Trace/ }).click();
    await expect(page).toHaveURL("/product/trace");
  });

  test("the trace example says where a chosen step is in the code", async ({ page }) => {
    await page.goto("/product/trace");
    await page
      .getByRole("button", { name: /useAuth/ })
      .first()
      .click();
    await expect(page.getByText(/^useAuth: frontend\/src\/hooks\/useAuth\.ts/)).toBeVisible();
  });

  test("the diagram example is all verified, and a box says which lines it is", async ({
    page,
  }) => {
    await page.goto("/product/diagrams");
    await expect(page.getByText("100% statically verified")).toBeVisible();
    await page.locator(".react-flow__node").filter({ hasText: "get_user_by_email" }).click();
    await expect(page.getByText(/^get_user_by_email: backend\/app\/crud\.py, lines/)).toBeVisible();
  });

  test("the MCP page gives each editor its setup for this site", async ({ page, baseURL }) => {
    await page.goto("/product/mcp");
    await expect(page.getByText(".cursor/mcp.json")).toBeVisible();
    // The site's own origin (SENSEI_WEB_ORIGIN; localhost in development).
    await expect(page.getByText(/"url": "http:\/\/localhost:3000\/mcp"/).first()).toBeVisible();
    expect(baseURL).toBeTruthy();
  });
});
