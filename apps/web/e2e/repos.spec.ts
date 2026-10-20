import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

async function noAxeViolations(page: Page) {
  await page.evaluate(() =>
    Promise.all(
      document
        .getAnimations()
        .filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => undefined)),
    ),
  );
  const { violations } = await new AxeBuilder({ page }).analyze();
  expect(violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);
}

test.describe("repositories", () => {
  test("the list shows status and size, filters by URL and moves with j/k", async ({ page }) => {
    await page.goto("/acme/repos");
    const row = page.getByRole("link", { name: /acme-gh\/api/ });
    await expect(row).toBeVisible();
    await expect(row).toContainText("Ready");
    await expect(row).toContainText("18.6K lines");
    await noAxeViolations(page);

    await page.keyboard.press("j");
    await expect(row).toBeFocused();

    await page.getByRole("combobox", { name: "Filter by status" }).click();
    await page.getByRole("option", { name: "Needs attention" }).click();
    await expect(page).toHaveURL(/status=attention/);
    await expect(page.getByText("Nothing matches these filters")).toBeVisible();
    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect(row).toBeVisible();
  });

  test("connect a public repository by URL: estimate, connect, land on its page", async ({
    page,
  }) => {
    await page.goto("/acme/repos/new");
    await page.getByRole("tab", { name: "Public URL" }).click();
    await expect(page).toHaveURL(/tab=url/);
    const url = page.getByLabel("Public repository URL");

    await url.fill("https://github.com/acme-gh/secret");
    await expect(page.getByText(/isn't a public GitHub repository/)).toBeVisible();

    await url.fill("https://github.com/langflow-ai/langflow");
    const estimate = page.getByRole("region", { name: "Estimate for langflow-ai/langflow" });
    await expect(estimate).toBeVisible();
    await expect(estimate).toContainText("First index");
    await noAxeViolations(page);
    await page.getByRole("button", { name: "Connect and index" }).click();

    await expect(page).toHaveURL(/\/acme\/langflow$/);
    await expect(page.getByRole("heading", { name: "langflow" })).toBeVisible();
    // The mock run takes a few seconds; the page follows it to the end.
    await expect(page.getByText("Ready", { exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("link", { name: /langflow/ }).first()).toBeVisible(); // sidebar

    // Connecting it again points at the one that exists.
    await page.goto("/acme/repos/new?tab=url");
    await page.getByLabel("Public repository URL").fill("github.com/langflow-ai/langflow");
    await page.getByRole("button", { name: "Connect and index" }).click();
    await expect(page.getByText(/is already connected/)).toBeVisible();
    await expect(page.getByRole("link", { name: "Open it" })).toHaveAttribute(
      "href",
      "/acme/langflow",
    );
  });

  test("connect from the GitHub installation", async ({ page }) => {
    await page.goto("/acme/repos/new");
    await expect(page.getByText("acme-gh/api")).toBeVisible();
    await expect(page.getByText("Connected", { exact: true })).toBeVisible();
    await page.getByLabel("Search repositories").fill("web");
    await expect(page.getByText("acme-gh/infra")).toBeHidden();
    await page.getByRole("button", { name: "Connect acme-gh/web" }).click();
    await expect(page).toHaveURL(/\/acme\/web$/);
  });

  test("onboarding without the GitHub App: a public repository by URL", async ({ page }) => {
    await page.goto("/onboarding");
    await page.getByLabel("Organization name").fill("Delta");
    await expect(page.getByText("Available.")).toBeVisible();
    await page.getByRole("button", { name: "Create organization" }).click();
    await page.getByRole("link", { name: "Skip, use a public URL" }).click();
    await expect(page.getByRole("heading", { name: "Connect a public repository" })).toBeVisible();
    await page
      .getByLabel("Public repository URL")
      .fill("https://github.com/fastapi/full-stack-fastapi-template");
    await expect(
      page.getByRole("region", { name: /Estimate for fastapi\/full-stack-fastapi-template/ }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Connect and index" }).click();
    await expect(page).toHaveURL(/\/delta\/full-stack-fastapi-template$/);
  });
});
