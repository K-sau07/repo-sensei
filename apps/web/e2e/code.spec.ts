import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page } from "@playwright/test";

import { expect, test } from "./fixtures";

const COMMIT = "b1125d8839482cb7bbae603ddc6d0081bf87532e";

/** The mock's synthetic files (a 10,000-line file, a 3 MB one, 100,000 files in a directory). */
async function useBrowseExtras(context: BrowserContext) {
  await context.addInitScript(() => {
    localStorage.setItem("sensei.mock.browse", "extras");
  });
}

const tree = (page: Page) => page.getByRole("tree", { name: "Repository files" });
// A file's name is followed by its layer, "main.py (API layer)".
const item = (page: Page, name: string) =>
  tree(page).getByRole("treeitem", {
    name: new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}( \\(|$)`),
  });

test.describe("code browser", () => {
  test("opens a file from the tree, by keyboard", async ({ page }) => {
    await page.goto("/acme/api/code");
    // Directories first, then files, as the API sorts them; the root's listing is in the main pane.
    await expect(tree(page).getByRole("treeitem").first()).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    await expect(page.getByRole("table", { name: "Contents of api" })).toBeVisible();

    await tree(page).focus();
    await page.keyboard.type("ba"); // type-ahead
    await expect(tree(page)).toHaveAttribute(
      "aria-activedescendant",
      (await item(page, "backend").getAttribute("id")) ?? "",
    );
    await page.keyboard.press("ArrowRight"); // open backend
    await expect(item(page, "backend")).toHaveAttribute("aria-expanded", "true");
    await expect(item(page, "app")).toBeVisible(); // its listing has arrived
    await page.keyboard.press("ArrowRight"); // into it: app
    await page.keyboard.press("ArrowRight"); // open app
    await expect(item(page, "main.py")).toBeVisible();
    await page.keyboard.type("mai");
    await page.keyboard.press("Enter");

    await expect(page).toHaveURL(/\/acme\/api\/code\/backend\/app\/main\.py$/);
    await expect(page.getByRole("heading", { level: 2, name: "main.py" })).toBeVisible();
    await expect(page.getByText("FRONTEND_DIR = Path")).toBeVisible();
    await expect(page.getByText("36 lines")).toBeVisible();
    await expect(page.getByText("958 B")).toBeVisible();
    await expect(item(page, "main.py")).toHaveAttribute("aria-selected", "true");
    await expect(item(page, "main.py")).toHaveAttribute("aria-level", "3");
    await expect(page.getByRole("link", { name: /Open on GitHub/ })).toHaveAttribute(
      "href",
      `https://github.com/acme-gh/api/blob/${COMMIT}/backend/app/main.py`,
    );

    // ← goes up to the directory; the breadcrumb leads back to it.
    await page.keyboard.press("ArrowLeft");
    await expect(tree(page)).toHaveAttribute(
      "aria-activedescendant",
      (await item(page, "app").getAttribute("id")) ?? "",
    );
    await page.getByRole("navigation", { name: "Path" }).getByRole("link", { name: "app" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/code\/backend\/app$/);
    await expect(page.getByRole("link", { name: "main.py" })).toBeVisible();

    const { violations } = await new AxeBuilder({ page }).analyze();
    expect(violations).toEqual([]);
  });

  test("line ranges: a deep link lights them, the gutter picks them, Back and Forward work", async ({
    page,
  }) => {
    await page.goto("/acme/api/code/backend/app/main.py?L=10-12");
    const viewer = page.getByTestId("code-viewer");
    await expect(viewer.locator(".cm-range-line")).toHaveCount(3);
    await expect(viewer.locator(".cm-lineNumbers .cm-range-gutter")).toHaveCount(3);
    await expect(page.getByText("Lines 10–12")).toBeVisible();

    const lineNumber = (n: number) =>
      viewer.locator(".cm-lineNumbers .cm-gutterElement").getByText(String(n), { exact: true });
    await lineNumber(20).click();
    await expect(page).toHaveURL(/L=20$/);
    await lineNumber(25).click({ modifiers: ["Shift"] });
    await expect(page).toHaveURL(/L=20-25$/);
    await expect(viewer.locator(".cm-range-line")).toHaveCount(6);
    // The permalink pins the commit and the lines.
    await expect(
      page.getByRole("button", { name: "Copy a permalink to lines 20–25" }),
    ).toBeVisible();

    await page.goBack();
    await expect(page).toHaveURL(/L=20$/);
    await expect(viewer.locator(".cm-range-line")).toHaveCount(1);
    await page.goBack();
    await expect(page).toHaveURL(/L=10-12$/);
    await expect(viewer.locator(".cm-range-line")).toHaveCount(3);
    await page.goForward();
    await expect(page).toHaveURL(/L=20$/);

    await page.getByRole("button", { name: "Clear the selected lines" }).click();
    await expect(viewer.locator(".cm-range-line")).toHaveCount(0);
  });

  test("a deep link far down a long file scrolls to the lines", async ({ page, context }) => {
    await useBrowseExtras(context);
    await page.goto("/acme/api/code/mock-extras/ten_thousand_lines.py?L=5001-5003");
    const lit = page.getByTestId("code-viewer").locator(".cm-range-line");
    await expect(lit).toHaveCount(3);
    await expect(lit.first()).toBeInViewport();
    await expect(lit.first()).toContainText("def handler_1251");
  });

  test("search, soft wrap and keyboard search", async ({ page }) => {
    await page.goto("/acme/api/code/backend/app/email-templates/new_account.html");
    const viewer = page.getByTestId("code-viewer");
    await expect(viewer.locator(".cm-content")).toBeVisible();
    // One 7,862-character line: wrapped, it takes many rows.
    const line = viewer.locator(".cm-line").first();
    const unwrapped = (await line.boundingBox())?.height ?? 0;
    await page.getByRole("button", { name: "Wrap long lines" }).click();
    await expect
      .poll(async () => (await line.boundingBox())?.height ?? 0)
      .toBeGreaterThan(unwrapped * 4);

    await page.getByRole("button", { name: "Search in the file" }).click();
    await expect(viewer.getByRole("textbox", { name: "Find" })).toBeFocused();
    await page.keyboard.press("Escape");
    // ⌘F from anywhere on the page searches the file (the browser's find can't see lines
    // CodeMirror hasn't rendered).
    await page.getByRole("heading", { level: 2, name: "new_account.html" }).click();
    await page.keyboard.press("ControlOrMeta+f");
    await expect(viewer.getByRole("textbox", { name: "Find" })).toBeFocused();
  });

  test("binary and large files say why and link to the file", async ({ page, context }) => {
    await useBrowseExtras(context);
    await page.goto("/acme/api/code/img/login.png");
    await expect(page.getByRole("heading", { name: "This file isn't text" })).toBeVisible();
    await expect(page.getByText("50.7 KB")).toHaveCount(2); // the header and the message
    // The API's download link (GitHub's raw file would need access to a private repository).
    await expect(page.getByRole("link", { name: "Download", exact: true })).toHaveAttribute(
      "href",
      /^https:\/\/storage\.mock\.invalid\/artifacts\//,
    );

    await page.goto("/acme/api/code/mock-extras/server.log");
    await expect(
      page.getByRole("heading", { name: "This file is too large to show" }),
    ).toBeVisible();
    await expect(page.getByRole("link", { name: "Download", exact: true })).toHaveAttribute(
      "href",
      /^https:\/\/storage\.mock\.invalid\/artifacts\//,
    );
    const { violations } = await new AxeBuilder({ page }).analyze();
    expect(violations).toEqual([]);
  });

  test("reveals the current file", async ({ page }) => {
    await page.goto("/acme/api/code/backend/app/api/routes/items.py");
    await expect(item(page, "items.py")).toHaveAttribute("aria-selected", "true");
    await page.getByRole("button", { name: "Collapse all folders" }).click();
    await expect(item(page, "items.py")).toHaveCount(0);
    await page.getByRole("button", { name: "Reveal the current file" }).click();
    await expect(item(page, "items.py")).toBeVisible();
    await expect(item(page, "routes")).toHaveAttribute("aria-expanded", "true");
  });

  test("directories, missing paths and the constellation's links", async ({ page }) => {
    // The constellation's directory links land here.
    await page.goto("/acme/api/code/backend/app/api/routes");
    await expect(page.getByRole("heading", { level: 2, name: "routes" })).toBeVisible();
    await expect(page.getByRole("link", { name: "items.py" })).toBeVisible();
    await expect(item(page, "routes")).toHaveAttribute("aria-selected", "true");

    await page.goto("/acme/api/code/backend/nope.py");
    await expect(page.getByText("Nothing at this path")).toBeVisible();
  });

  test("a 10,000-line file paints within 300 ms", async ({ page, context }) => {
    await useBrowseExtras(context);
    // Warm up: the viewer's chunk and the Python grammar load with the first file.
    await page.goto("/acme/api/code/backend/app/main.py");
    await expect(page.getByText("FRONTEND_DIR = Path")).toBeVisible();
    await page.evaluate(() => {
      performance.clearMeasures();
    });
    await item(page, "mock-extras").click();
    await item(page, "ten_thousand_lines.py").click();
    await expect(page.getByText("def handler_0001")).toBeVisible();
    const paint = await page.evaluate(
      () => performance.getEntriesByName("code-viewer:paint", "measure").at(-1)?.duration ?? null,
    );
    console.log(`10,000-line file: first paint ${String(paint?.toFixed(1))} ms after it arrived`);
    expect(paint).not.toBeNull();
    expect(paint ?? Infinity).toBeLessThan(300);
  });

  test("the tree scrolls 100,000 files at 60 fps", async ({ page, context }) => {
    await useBrowseExtras(context);
    await page.goto("/acme/api/code/mock-extras/many-files");
    await expect(item(page, "module_000001.ts")).toBeVisible();
    const frames = await tree(page).evaluate(async (element) => {
      const intervals: number[] = [];
      let last = performance.now();
      for (let frame = 0; frame < 180; frame += 1) {
        element.scrollTop += 120; // ~4 rows a frame: a brisk flick
        await new Promise(requestAnimationFrame);
        const now = performance.now();
        intervals.push(now - last);
        last = now;
      }
      return intervals.sort((a, b) => a - b);
    });
    const median = frames[Math.floor(frames.length / 2)] ?? Infinity;
    const p95 = frames[Math.floor(frames.length * 0.95)] ?? Infinity;
    console.log(`100,000-row tree: median frame ${median.toFixed(1)} ms, p95 ${p95.toFixed(1)} ms`);
    // A 60 Hz frame is 16.7 ms; allow for the timer's jitter.
    expect(median).toBeLessThan(18);
    expect(p95).toBeLessThan(34);
    // Only the rows on screen exist.
    expect(await tree(page).getByRole("treeitem").count()).toBeLessThan(120);
  });
});
