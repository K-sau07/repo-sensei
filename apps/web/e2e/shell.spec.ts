import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

async function openShell(page: Page, path = "/acme") {
  await page.goto(path);
  await expect(page.getByRole("button", { name: "Acme (switch organization)" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-shell-ready", "true");
}

async function expectNoAxeViolations(page: Page) {
  const results = await new AxeBuilder({ page }).analyze();
  expect(
    results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`),
  ).toEqual([]);
}

test("the shell has no accessibility violations, in dark and light", async ({ page }) => {
  await openShell(page);
  await expectNoAxeViolations(page);

  await page.evaluate(() => {
    localStorage.setItem("theme", "light");
  });
  await openShell(page);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expectNoAxeViolations(page);
});

test("the skip link jumps to the content", async ({ page }) => {
  await openShell(page);
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to content" });
  await expect(skip).toBeFocused();
  await expect(skip).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main")).toBeFocused();
});

test("⌘K opens instantly and gives focus back when it closes", async ({ page }) => {
  await openShell(page);
  const inbox = page.getByRole("link", { name: "Inbox" }).first();
  await inbox.focus();

  await page.keyboard.press("ControlOrMeta+k");
  const palette = page.getByRole("dialog", { name: "Command palette" });
  await expect(palette).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Command" })).toBeFocused();
  // Used constantly, so it never animates in.
  expect(await palette.evaluate((el) => getComputedStyle(el).animationName)).toBe("none");
  await expectNoAxeViolations(page);

  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();
  await expect(inbox).toBeFocused();
});

test("⌘K runs commands", async ({ page }) => {
  await openShell(page);
  await page.keyboard.press("ControlOrMeta+k");
  await page.keyboard.type("inbox");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/acme\/inbox$/);
  await expect(page.getByRole("link", { name: "Inbox" }).first()).toHaveAttribute(
    "aria-current",
    "page",
  );
});

test("go-to sequences navigate", async ({ page }) => {
  await openShell(page);
  await page.locator("#main").click();
  await page.keyboard.press("g");
  await page.keyboard.press("y");
  await expect(page).toHaveURL(/\/acme\/activity$/);
});

test("? shows every shortcut", async ({ page }) => {
  await openShell(page);
  await page.locator("#main").click();
  await page.keyboard.press("Shift+Slash");
  const sheet = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByText("Open the command palette")).toBeVisible();
  await expect(sheet.getByText("Toggle the sidebar")).toBeVisible();
});

test("[ collapses the sidebar; it reloads collapsed without shifting the layout", async ({
  page,
}) => {
  await openShell(page);
  await page.locator("#main").click();
  await page.keyboard.press("BracketLeft");
  await expect(page.locator("html")).toHaveAttribute("data-sidebar", "collapsed");
  // ⌘[ is the browser's Back, not ours.
  await page.keyboard.press("ControlOrMeta+BracketLeft");
  await expect(page.locator("html")).toHaveAttribute("data-sidebar", "collapsed");

  await page.addInitScript(() => {
    const shifts: number[] = [];
    (window as unknown as { __shifts: number[] }).__shifts = shifts;
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries() as (PerformanceEntry & {
        value: number;
        hadRecentInput: boolean;
      })[]) {
        if (!entry.hadRecentInput) shifts.push(entry.value);
      }
    }).observe({ type: "layout-shift", buffered: true });
  });
  await openShell(page);
  await page.waitForTimeout(1000);
  await expect(page.locator("html")).toHaveAttribute("data-sidebar", "collapsed");
  const cls = await page.evaluate(() =>
    (window as unknown as { __shifts: number[] }).__shifts.reduce((a, b) => a + b, 0),
  );
  expect(cls).toBeLessThan(0.01);
});

test("the sidebar resizes from the keyboard and remembers its width", async ({ page }) => {
  await openShell(page);
  const handle = page.getByRole("separator", { name: "Resize sidebar" });
  await handle.focus();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  await expect(handle).toHaveAttribute("aria-valuenow", "272");
  await page.keyboard.press("End");
  await expect(handle).toHaveAttribute("aria-valuenow", "360");

  await openShell(page);
  const sidebar = page.getByRole("complementary", { name: "Sidebar" });
  expect((await sidebar.boundingBox())?.width).toBe(360);
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("navigation moves to a sheet and a bottom bar", async ({ page }) => {
    await page.goto("/acme");
    await expect(page.getByRole("complementary", { name: "Sidebar" })).toBeHidden();
    const quick = page.getByRole("navigation", { name: "Quick" });
    await expect(quick.getByRole("link", { name: "Inbox" })).toBeVisible();

    await page.getByRole("button", { name: "Open navigation" }).click();
    const sheet = page.getByRole("dialog", { name: "Navigation" });
    await expect(sheet).toBeVisible();
    await sheet.getByRole("link", { name: "Activity" }).click();
    await expect(page).toHaveURL(/\/acme\/activity$/);
    await expect(sheet).toBeHidden();
    await expectNoAxeViolations(page);
  });
});
