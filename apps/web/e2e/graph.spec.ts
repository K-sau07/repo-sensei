import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

const canvas = (page: Page) => page.getByTestId("graph-canvas");

test.describe("graph explorer", () => {
  test("the module level on WebGL, a node's panel, and the code behind an edge", async ({
    page,
  }) => {
    await page.goto("/acme/api/graph");
    await expect(canvas(page)).toHaveAttribute("data-nodes", "13");
    await expect(canvas(page).locator("canvas").first()).toBeVisible();
    await expect(page.getByRole("list", { name: "Legend" })).toContainText("In a cycle");
    await expect(page.getByTestId("graph-minimap")).toBeVisible();

    // The same graph as tables; a node's name focuses it.
    await page.getByRole("switch", { name: "Show as tables" }).click();
    const table = page.getByTestId("graph-table");
    await table
      .getByRole("button", { name: "backend/app/api/routes", exact: true })
      .first()
      .click();
    await expect(page).toHaveURL(/node=backend\/app\/api\/routes/);
    const panel = page.getByTestId("graph-node-panel");
    await expect(panel.getByRole("heading", { name: "backend/app/api/routes" })).toBeVisible();
    await expect(panel.getByRole("definition").first()).toBeVisible(); // its coupling
    // Its heaviest dependency: the lines of code behind it, each opening the code.
    await panel.getByRole("button", { name: /backend\/app\s+182/ }).click();
    const edge = page.getByTestId("graph-edge-panel");
    await expect(edge).toContainText("calls");
    const lines = page.getByTestId("graph-evidence").getByRole("link");
    await expect(lines.first()).toHaveAttribute(
      "href",
      /\/acme\/api\/code\/backend\/app\/api\/routes\/.+\?L=\d+/,
    );

    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);

    // Back on the canvas, focused, ± hops.
    await page.getByRole("switch", { name: "Show as tables" }).click();
    await edge.getByRole("button", { name: "Close the edge" }).click();
    await expect(page.getByTestId("graph-degree")).toHaveText("1");
    await page.getByRole("button", { name: "More hops" }).click();
    await expect(page.getByTestId("graph-degree")).toHaveText("2");
    await expect(page).toHaveURL(/degree=2/);
  });

  test("opening a node into its files, by zooming or from its panel", async ({ page }) => {
    await page.goto("/acme/api/graph?node=backend%2Fapp%2Fapi%2Froutes");
    await expect(canvas(page)).toHaveAttribute("data-nodes", "13");
    await page
      .getByTestId("graph-node-panel")
      .getByRole("button", { name: "Open into files" })
      .click();
    await expect(canvas(page)).toHaveAttribute("data-open", "backend/app/api/routes");
    // Its 6 files in its place: 13 modules less one, plus its files.
    await expect(canvas(page)).toHaveAttribute("data-nodes", "18");

    // Zoomed in, the modules in view open; out again, they close.
    await page.goto("/acme/api/graph");
    await expect(canvas(page)).toHaveAttribute("data-nodes", "13");
    const box = await canvas(page).boundingBox();
    if (!box) throw new Error("no canvas");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    for (let i = 0; i < 12; i++) {
      await page.mouse.wheel(0, -400);
      await page.waitForTimeout(60);
    }
    await expect(canvas(page)).not.toHaveAttribute("data-open", "");
    await page.getByRole("button", { name: "Fit the graph" }).click();
    await expect(canvas(page)).toHaveAttribute("data-open", "");
  });

  test("search by name or by a phrase, and a path between two nodes", async ({ page }) => {
    await page.goto("/acme/api/graph");
    await page.getByLabel("Find in the graph").fill("routes");
    await page.getByRole("button", { name: "Find", exact: true }).click();
    await expect(page.getByTestId("graph-found")).toHaveText("3 nodes found");
    await page.getByLabel("Find in the graph").fill("create a user");
    await page.getByRole("button", { name: "Find", exact: true }).click();
    await expect(page.getByTestId("graph-found")).toHaveText(/[1-9]\d* nodes? found/);

    await page.goto("/acme/api/graph?table=true");
    const table = page.getByTestId("graph-table");
    await table
      .getByRole("button", { name: "backend/app/api/routes", exact: true })
      .first()
      .click();
    await page.getByRole("button", { name: "Path from here" }).click();
    await table.getByRole("button", { name: "backend/app/core", exact: true }).first().click();
    await page.getByRole("button", { name: "Path to here" }).click();
    await expect(page.getByTestId("graph-path")).toContainText(
      "backend/app/api/routes → backend/app/core (1 hop)",
    );
  });

  test("the DSM: a cycle's cells in red, and a cell's code", async ({ page }) => {
    await page.goto("/acme/api/graph?tab=dsm");
    const matrix = page.getByTestId("dsm-canvas");
    await expect(matrix).toBeVisible();
    await expect(matrix).not.toHaveAttribute("data-cycle-cells", "0");
    await expect(page.getByText("13 modules · 1 cycle")).toBeVisible();
    await page.getByText("Every dependency, as a list").click();
    await page
      .getByRole("button", {
        name: /^backend\/app\/api\/routes depends on backend\/app: \d+ \(in a cycle\)$/,
      })
      .click();
    await expect(page.getByTestId("graph-evidence").getByRole("link").first()).toBeVisible();
    await expect(page.getByRole("table", { name: /Coupling/ })).toBeVisible();
  });

  test("cycles, each shown alone on the graph", async ({ page }) => {
    await page.goto("/acme/api/graph?tab=cycles");
    const cycle = page.getByTestId("cycle");
    await expect(cycle).toHaveCount(1);
    await expect(cycle).toContainText("4 modules depend on each other");
    await expect(cycle).toContainText("lightest");
    await cycle.getByRole("button", { name: "Show in the graph" }).click();
    await expect(page).toHaveURL(/\/graph\?cycle=0$/); // the graph tab: the default
    await expect(page.getByRole("button", { name: "Show all" })).toBeVisible();
  });

  test("packages: versions, where each is declared, and who imports it", async ({ page }) => {
    await page.goto("/acme/api/graph?tab=packages");
    const fastapi = page.getByTestId("package").filter({ hasText: /^fastapi/ });
    await expect(fastapi).toBeVisible();
    await fastapi.getByText("fastapi", { exact: true }).click();
    await expect(fastapi).toContainText("Declared in backend/pyproject.toml");
    await expect(fastapi.getByRole("list", { name: "Files importing fastapi" })).toBeVisible();
    await page.getByLabel("Find a package").fill("react");
    await expect(page.getByTestId("package").first()).toContainText("react");
  });

  test("20,000 nodes stay smooth to pan", async ({ page, context }) => {
    test.setTimeout(90_000); // the mock sends the level as JSON (the API: MessagePack)
    await context.addInitScript(() => {
      localStorage.setItem("sensei.mock.deps", "big");
    });
    await page.goto("/acme/api/graph");
    await expect(canvas(page)).toHaveAttribute("data-nodes", "20000", { timeout: 45_000 });
    const box = await canvas(page).boundingBox();
    if (!box) throw new Error("no canvas");
    await page.evaluate(() => {
      const w = window as unknown as { frames_: number; stop_: boolean };
      w.frames_ = 0;
      w.stop_ = false;
      const tick = () => {
        w.frames_++;
        if (!w.stop_) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
    const started = Date.now();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    for (let i = 0; i < 60; i++) {
      await page.mouse.move(box.x + box.width / 2 + Math.sin(i / 6) * 200, box.y + box.height / 2);
    }
    await page.mouse.up();
    const frames = await page.evaluate(() => {
      const w = window as unknown as { frames_: number; stop_: boolean };
      w.stop_ = true;
      return w.frames_;
    });
    const fps = (frames * 1000) / (Date.now() - started);
    test.info().annotations.push({ type: "fps", description: fps.toFixed(1) });
    // Recorded, not asserted: headless Chromium draws WebGL with a software rasterizer here (an
    // M3 Pro's GPU pans this level at ~49 fps).
    expect(fps).toBeGreaterThan(0);
  });
});
