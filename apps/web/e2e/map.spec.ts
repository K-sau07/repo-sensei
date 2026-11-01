import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

const LOGIN = "tsx frontend/src/routes/login.tsx/Login().";

test.describe("codebase galaxy", () => {
  test("by meaning first, its clusters named; by folder at a toggle; a search lights stars", async ({
    page,
  }) => {
    await page.goto("/acme/api/map");
    const canvas = page.getByTestId("galaxy-canvas");
    await expect(canvas).toHaveAttribute("data-layout", "semantic");
    await expect(canvas).toHaveAttribute("data-stars", "43");
    await expect(canvas.locator("canvas").first()).toBeVisible();
    const clusters = page.getByTestId("galaxy-clusters").getByRole("button");
    await expect(clusters).toHaveCount(2);
    // Named in the scene too: a name flies there.
    await canvas.getByRole("button", { name: /session/ }).click();
    await clusters.first().click();

    const audit = await new AxeBuilder({ page }).include("main").exclude("canvas").analyze();
    expect(audit.violations).toEqual([]);

    await page.getByRole("button", { name: "By folder" }).click();
    await expect(canvas).toHaveAttribute("data-layout", "structural");
    await expect(canvas).toHaveAttribute("data-stars", "245");
    await expect(page).toHaveURL(/layout=structural/);

    await page.getByLabel("Find stars").fill("routes");
    await page.getByRole("button", { name: "Find", exact: true }).click();
    await expect(page.getByTestId("galaxy-found")).toHaveText(/[1-9]\d* stars lit/);
  });

  test("while the layout by meaning is made, by folder, then by meaning", async ({
    page,
    context,
  }) => {
    await context.addInitScript(() => {
      localStorage.setItem("sensei.mock.galaxy", "computing");
    });
    await page.goto("/acme/api/map");
    await expect(page.getByTestId("galaxy-note")).toContainText(
      "Working out the layout by meaning",
    );
    const canvas = page.getByTestId("galaxy-canvas");
    await expect(canvas).toHaveAttribute("data-layout", "structural");
    // The toggle says what is drawn.
    const byFolder = page.getByRole("button", { name: "By folder" });
    const byMeaning = page.getByRole("button", { name: "By meaning" });
    await expect(byFolder).toHaveAttribute("aria-pressed", "true");
    await expect(canvas).toHaveAttribute("data-layout", "semantic", { timeout: 15_000 });
    await expect(byMeaning).toHaveAttribute("aria-pressed", "true");
  });

  test("without summaries it says so, by folder", async ({ page, context }) => {
    await context.addInitScript(() => {
      localStorage.setItem("sensei.mock.galaxy", "none");
    });
    await page.goto("/acme/api/map");
    await expect(page.getByTestId("galaxy-note")).toContainText("needs this commit's summaries");
    await expect(page.getByTestId("galaxy-canvas")).toHaveAttribute("data-layout", "structural");
    await expect(page.getByRole("button", { name: "By folder" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(page.getByRole("button", { name: "By meaning" })).toBeDisabled();
  });

  test("a trace's stars are joined", async ({ page }) => {
    await page.goto(`/acme/api/map?layout=structural&trace=${encodeURIComponent(LOGIN)}`);
    await expect(page.getByTestId("galaxy-canvas")).not.toHaveAttribute("data-trace", /^[01]$/);
  });

  test("100,000 stars", async ({ page, context }) => {
    test.setTimeout(90_000);
    await context.addInitScript(() => {
      localStorage.setItem("sensei.mock.galaxy", "big");
    });
    await page.goto("/acme/api/map");
    const canvas = page.getByTestId("galaxy-canvas");
    await expect(canvas).toHaveAttribute("data-stars", "100000", { timeout: 60_000 });
    await expect(canvas.locator("canvas").first()).toBeVisible();
  });
});
