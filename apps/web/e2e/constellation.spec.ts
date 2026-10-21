import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

const SUMMARY = /Constellation of the repository: 245 files in \d+ directories/;

test.describe("constellation", () => {
  test("the last index's constellation, in 3D, with its directories as a list", async ({
    page,
  }) => {
    await page.goto("/acme/api");
    const figure = page.getByRole("figure", { name: /Constellation/ });
    await expect(figure.getByRole("img", { name: SUMMARY })).toBeVisible();
    // A browser with WebGL2 gets the 3D view (rendered in the browser only).
    await expect(figure.getByTestId("constellation-webgl").locator("canvas")).toBeVisible();

    await figure.getByRole("button", { name: "Show as a list" }).click();
    const table = figure.getByRole("table", { name: "Directories in the constellation" });
    await expect(table.getByRole("link", { name: "backend/app/api/routes/" })).toHaveAttribute(
      "href",
      "/acme/api/code/backend/app/api/routes",
    );
    const { violations } = await new AxeBuilder({ page }).analyze();
    expect(violations).toEqual([]);
  });

  test("it forms while a run discovers the files", async ({ page }) => {
    await page.goto("/acme/repos/new?tab=url");
    await page
      .getByLabel("Public repository URL")
      .fill("https://github.com/fastapi/full-stack-fastapi-template");
    await expect(page.getByRole("region", { name: /Estimate for/ })).toBeVisible();
    await page.getByRole("button", { name: "Connect and index" }).click();
    await expect(page).toHaveURL(/\/acme\/full-stack-fastapi-template$/);

    const figure = page.getByRole("figure", { name: /Constellation/ });
    await expect(figure.getByText("Stars appear as discovery finds the files.")).toBeVisible();
    // While the run is live the view turns, with a control to stop it.
    await figure.getByRole("button", { name: "Pause motion" }).click();
    await expect(figure.getByRole("button", { name: "Resume motion" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // Discovery's points arrive; when the run is over the overview shows the last index's.
    await expect(
      page.getByRole("figure", { name: /Constellation/ }).getByRole("img", { name: SUMMARY }),
    ).toBeVisible({ timeout: 15_000 });
  });

  test("with reduced motion it is drawn still, without the motion control", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/acme/api");
    const figure = page.getByRole("figure", { name: /Constellation/ });
    await expect(figure.getByRole("img", { name: SUMMARY })).toBeVisible();
    await expect(figure.getByTestId("constellation-webgl")).toBeVisible();
    await expect(figure.getByRole("button", { name: /motion/ })).toHaveCount(0);
  });

  test("without WebGL2 it falls back to a static SVG scatter", async ({ page }) => {
    await page.addInitScript(() => {
      // A browser without WebGL2: every other kind of canvas context as usual.
      const proto = HTMLCanvasElement.prototype;
      const original = Reflect.get(proto, "getContext") as (...args: unknown[]) => unknown;
      Reflect.set(proto, "getContext", function (this: HTMLCanvasElement, ...args: unknown[]) {
        return args[0] === "webgl2" ? null : Reflect.apply(original, this, args);
      });
    });
    await page.goto("/acme/api");
    const figure = page.getByRole("figure", { name: /Constellation/ });
    const svg = figure.getByTestId("constellation-svg");
    await expect(svg).toBeVisible();
    await expect(svg).toHaveAccessibleName(SUMMARY);
    await expect(figure.getByTestId("constellation-webgl")).toHaveCount(0);
    // Every layer is drawn (each as a few paths, not a node per star).
    for (const layer of ["ui", "api", "svc", "db", "other"]) {
      await expect(svg.locator(`path[data-layer=${layer}]`).first()).toBeAttached();
    }
  });
});
