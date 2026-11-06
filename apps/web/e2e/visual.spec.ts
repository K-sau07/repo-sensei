/**
 * Visual baselines (10.8.3): a few pages whose mock content doesn't move with the date, at a
 * phone's, a tablet's and a laptop's width. Screenshots differ by platform, so these run, and
 * their baselines are made, only in the visual workflow (the Playwright image, Node 22): a page
 * without a baseline gets one there, uploaded as the run's `visual-baselines` artifact to commit
 * here. To make one again, delete its PNG. Not part of `pnpm e2e` (`E2E_VISUAL=1` adds them).
 */
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

const WIDTHS = [
  ["phone", 390, 844],
  ["tablet", 834, 1112],
  ["laptop", 1440, 900],
] as const;

const PAGES = [
  ["ask", "/acme/api/ask", "Ask about this repository"],
  ["models", "/acme/settings/models", "Models"],
  ["general", "/acme/settings/general", "General"],
  ["pricing", "/pricing", "Pricing"],
] as const;

// What moves: canvases (the landing's globe).
const masks = (page: Page) => [page.locator("canvas")];

for (const [size, width, height] of WIDTHS) {
  test.describe(size, () => {
    test.use({ viewport: { width, height }, colorScheme: "dark", reducedMotion: "reduce" });

    for (const [name, path, heading] of PAGES) {
      test(`${name} @visual`, async ({ page }) => {
        await page.goto(path);
        await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible();
        await page.evaluate(() => document.fonts.ready);
        // The dev server's badge isn't the page's (a mask can't reach into its shadow root).
        await page.addStyleTag({ content: "nextjs-portal { display: none !important; }" });
        await expect(page).toHaveScreenshot(`${name}-${size}.png`, {
          fullPage: true,
          animations: "disabled",
          caret: "hide",
          mask: masks(page),
          maxDiffPixelRatio: 0.01,
        });
      });
    }
  });
}
