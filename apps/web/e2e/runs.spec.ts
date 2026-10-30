import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.describe("runs", () => {
  test("an answer's run opens as a timeline, and travels back to where a file was read", async ({
    page,
  }) => {
    await page.goto("/acme/api/ask");
    const box = page.getByRole("textbox", { name: "Ask about this repository" });
    await box.fill("How is an item created?");
    await box.press("ControlOrMeta+Enter");
    await expect(page.getByTestId("announcer")).toHaveText("Answer ready, 2 citations");
    await page.getByRole("link", { name: "View run" }).click();

    await expect(page).toHaveURL(/\/acme\/api\/runs\/[0-9a-f-]{36}$/);
    await expect(page.getByRole("heading", { name: "How is an item created?" })).toBeVisible();
    const waterfall = page.getByTestId("waterfall");
    await expect(waterfall.getByRole("button")).toHaveCount(40);
    const rail = page.getByTestId("stage-rail");
    await expect(rail).toContainText("Reading files ×2");

    await page.getByRole("button", { name: "Expanded" }).click();
    await expect(rail).not.toContainText("×2");

    // A file: the step that first read it, the steps after it ghosted.
    const file = page.getByTestId("files-read").getByRole("button").first();
    const path = (await file.textContent()) ?? "";
    await file.click();
    await expect(page.getByTestId("step-card")).toContainText(path);
    const slider = page.getByRole("slider", { name: "Replay to step" });
    await expect(slider).not.toHaveAttribute("aria-valuetext", "Step 40 of 40");
    await expect(waterfall.getByRole("button").last()).toHaveClass(/opacity-20/);

    // A bar picks its step.
    await waterfall.getByRole("button").last().click();
    await expect(page.getByTestId("step-card")).toContainText(
      "Checking the answer against the code",
    );

    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);
  });

  test("the runs tab lists them; a run under way grows as it goes", async ({ page, context }) => {
    await context.addInitScript(() => {
      localStorage.setItem("sensei.mock.runs", "live");
    });
    await page.goto("/acme/api/runs");
    await expect(page.getByRole("cell", { name: /How is an item created/ })).toBeVisible();
    await expect(page.getByText("running")).toBeVisible();
    await page.getByRole("link", { name: "How is an item created?" }).click();
    await expect(page.getByTestId("live")).toBeVisible();
    await expect(page.getByTestId("waterfall").getByRole("button")).toHaveCount(40, {
      timeout: 15_000,
    });
  });
});
