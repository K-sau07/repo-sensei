import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

/** Connect a public repository by URL and land on its page, where its first run starts. */
async function connect(page: Page, url: string) {
  await page.goto("/acme/repos/new?tab=url");
  await page.getByLabel("Public repository URL").fill(url);
  await expect(page.getByRole("region", { name: /Estimate for/ })).toBeVisible();
  await page.getByRole("button", { name: "Connect and index" }).click();
}

test.describe("indexing", () => {
  test("watch a run live: stages, counters and unlocks, then the summary", async ({ page }) => {
    await connect(page, "https://github.com/fastapi/full-stack-fastapi-template");
    await expect(page).toHaveURL(/\/acme\/full-stack-fastapi-template$/);

    const stages = page.getByRole("list", { name: "Indexing stages" });
    await expect(stages).toBeVisible();
    await expect(stages.locator("[aria-current=step]")).toBeVisible();
    await expect(page.getByText("Live", { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: /Code browser available now/ })).toBeVisible({
      timeout: 10_000,
    });

    // At the end the overview shows what the run found.
    const summary = page.getByRole("region", { name: /^Indexed/ });
    await expect(summary).toBeVisible({ timeout: 15_000 });
    await expect(summary).toContainText("245");
    await expect(summary).toContainText("18,567");
  });

  test("a locked tab unlocks while you watch it, without reloading", async ({ page }) => {
    await connect(page, "https://github.com/fastapi/full-stack-fastapi-template");
    await expect(page).toHaveURL(/\/acme\/full-stack-fastapi-template$/);
    await page.getByRole("link", { name: /^Code/ }).click();
    await expect(page.getByRole("region", { name: "Code is not ready yet" })).toBeVisible();
    // Discovery finishes on the run's own schedule; the tab follows the org stream.
    await expect(page.getByText("Code is on its way")).toBeVisible({ timeout: 15_000 });
  });

  test("a failed run says why, and Retry starts another", async ({ page }) => {
    await connect(page, "https://github.com/big-corp/monorepo");
    const alert = page.getByRole("alert").filter({ hasText: "over 2.0 GB" });
    await expect(alert).toBeVisible({ timeout: 15_000 });
    await expect(alert).toContainText("repo_too_large");

    await alert.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByText("Indexing started")).toBeVisible();
    await page.goto("/acme/monorepo/index");
    await expect(page.getByRole("table").getByRole("row")).toHaveCount(3); // header + 2 runs
  });

  test("cancel a run, after confirming", async ({ page }) => {
    await page.goto("/acme/api/index");
    await page.getByRole("button", { name: "Index again" }).click();
    await page.getByRole("button", { name: "Cancel indexing" }).click();
    const dialog = page.getByRole("alertdialog", { name: "Cancel this index run?" });
    await dialog.getByRole("button", { name: "Cancel indexing" }).click();
    await expect(page.getByText("Indexing cancelled")).toBeVisible();
    await expect(page.getByRole("table")).toContainText("Cancelled");
    await expect(page.getByRole("button", { name: "Index again" })).toBeVisible();
    // The repository keeps its last index: the overview shows it, not the cancelled run.
    await page.getByRole("link", { name: "Overview" }).click();
    await expect(page.getByRole("region", { name: /^Indexed/ })).toBeVisible();
  });

  test("index history: every run, pick one, read and filter its log", async ({ page }) => {
    await page.goto("/acme/api/index");
    // A finished run opens as it is: no Cancel while its history replays into the log.
    await expect(page.getByRole("heading", { name: /^Indexed/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "Cancel indexing" })).toBeHidden();

    await page.getByRole("button", { name: "Index again" }).click();
    await expect(page.getByRole("table").getByRole("row")).toHaveCount(3);
    await expect(page.getByRole("heading", { name: /^Indexed/ })).toBeVisible({ timeout: 15_000 });

    // The older run, by its row; the URL keeps the pick.
    await page.getByRole("button", { name: /run 0199f000-0000-7000-8000-0000000000f1/ }).click();
    await expect(page).toHaveURL(/job=0199f000-0000-7000-8000-0000000000f1/);

    await page.getByRole("button", { name: /View logs \(3\)/ }).click();
    const drawer = page.getByRole("dialog", { name: "Index log" });
    await expect(drawer.getByText("3 lines")).toBeVisible();
    await drawer.getByLabel("Search the log").fill("245 files");
    await expect(drawer.getByText("2 lines")).toBeVisible();
    await drawer.getByRole("button", { name: "info" }).click();
    await expect(drawer.getByText("No lines match.")).toBeVisible();
  });
});
