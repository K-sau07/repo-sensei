import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.describe("freshness", () => {
  test("a branch picked is the page's ref, and the tabs keep it", async ({ page }) => {
    await page.goto("/acme/api/code");
    const switcher = page.getByTestId("ref-switcher");
    await expect(switcher).toHaveText(/main/);
    await switcher.click();

    // The default branch, a tracked branch, and a pull request still being indexed (not pickable).
    await expect(page.getByRole("option", { name: /^main/ })).toContainText("Indexed at b1125d8");
    await expect(page.getByRole("option", { name: /pull\/7/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await page.getByRole("option", { name: /release/ }).click();

    await expect(page).toHaveURL(/\/acme\/api\/code\?ref=release$/);
    await expect(switcher).toHaveText(/release/);
    // The tree is read at the branch.
    await expect(page.getByRole("treeitem", { name: /backend/ }).first()).toBeVisible();

    // Another tab keeps it; the default branch takes it out of the URL again.
    await page.getByRole("link", { name: "Ask" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/ask\?ref=release$/);
    await switcher.click();
    await page.getByRole("option", { name: /^main/ }).click();
    await expect(page).toHaveURL(/\/acme\/api\/ask$/);
  });

  test("a push's index lands: the banner says so, and an answer citing what changed is stale", async ({
    page,
    context,
  }) => {
    await context.addInitScript(() => {
      localStorage.setItem("sensei.mock.push", "1");
    });
    await page.goto("/acme/api/ask");
    const box = page.getByRole("textbox", { name: "Ask about this repository" });
    await box.fill("How is an item created?");
    await box.press("ControlOrMeta+Enter");

    const answer = page.getByTestId("answer");
    await expect(answer.getByTestId("citation-chip")).toHaveCount(2);

    // The new index, on the branch the page is read at, while it was open.
    const banner = page.getByTestId("new-commits");
    await expect(banner).toContainText("New commits indexed on main · 1 file changed");
    await expect(banner).toContainText("backend/app/models.py");

    // The answer cites models.py, which the push changed: its chip and the answer say so.
    await expect(answer.getByTestId("answer-stale")).toContainText("1 cited file changed since");
    await expect(answer.locator('[data-stale="changed"]')).toHaveCount(1);

    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);

    // Refresh view reads it all again and the banner goes; Re-run asks the question again.
    await banner.getByRole("button", { name: "Refresh view" }).click();
    await expect(banner).toBeHidden();
    await answer.getByRole("button", { name: "Re-run on latest" }).click();
    await expect(page.getByTestId("question")).toHaveCount(2);
    await expect(page.getByTestId("question").last()).toHaveText("How is an item created?");
  });
});
