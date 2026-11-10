import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.describe("diagrams", () => {
  test("a question is drawn from the graph, with how much of it is verified", async ({ page }) => {
    await page.goto("/acme/api/diagrams");
    await expect(page.getByTestId("no-diagrams")).toBeVisible();
    await page.getByLabel("New diagram").fill("How does login work?");
    await page.getByRole("button", { name: "Draw" }).click();

    await expect(page).toHaveURL(/\/acme\/api\/diagrams\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("verified-ratio")).toHaveText(/\d+% statically verified/);
    const canvas = page.getByTestId("diagram-canvas");
    await expect(canvas.getByTestId("blueprint-node").first()).toBeVisible();
    await expect(canvas.locator(".react-flow__edge.inferred")).toHaveCount(1);
    // Hidden: only what the graph proves.
    await page.getByRole("switch", { name: "Hide unverified edges" }).click();
    await expect(canvas.locator(".react-flow__edge.inferred")).toHaveCount(0);
    await expect(canvas.locator(".react-flow__edge.verified").first()).toBeVisible();
    // The outline says it as text.
    await page.getByRole("button", { name: "Outline" }).click();
    await expect(page.getByTestId("diagram-outline")).toContainText("Login →");

    // A node opens its code beside the canvas.
    await canvas.getByTestId("blueprint-node").filter({ hasText: "authenticate" }).click();
    const panel = page.getByTestId("node-panel");
    await expect(panel).toContainText("authenticate");
    await expect(panel).toContainText("crud.py");
    await expect(panel.getByRole("textbox", { name: "Ask about authenticate" })).toBeVisible();

    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);

    // Listed, with its verification.
    await page.getByRole("link", { name: "Diagrams" }).first().click();
    await expect(page.getByRole("link", { name: /How does login work\?/ })).toContainText(
      "statically verified",
    );
  });

  test("a trace opens as a diagram", async ({ page }) => {
    await page.goto(
      `/acme/api/trace?from=${encodeURIComponent("tsx frontend/src/routes/login.tsx/Login().")}`,
    );
    await page.getByRole("link", { name: "Open as diagram" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/diagrams\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("verified-ratio")).toHaveText("100% statically verified");
    await expect(
      page.getByTestId("diagram-canvas").getByTestId("blueprint-node").first(),
    ).toBeVisible();
  });

  test("nothing in the graph: it says so", async ({ page }) => {
    await page.goto("/acme/api/diagrams");
    await page.getByLabel("New diagram").fill("show me nothing at all");
    await page.getByRole("button", { name: "Draw" }).click();
    await expect(
      page.getByRole("alert").filter({ hasText: "Nothing in the code graph" }),
    ).toBeVisible();
  });

  test("a cited file changed since: stale, and drawn again with what changed", async ({
    page,
    context,
  }) => {
    await context.addInitScript(() => {
      localStorage.setItem("sensei.mock.push", "1");
    });
    await page.goto("/acme/api/diagrams");
    await page.getByLabel("New diagram").fill("How does login work?");
    await page.getByRole("button", { name: "Draw" }).click();
    const stale = page.getByTestId("diagram-stale");
    await expect(stale).toContainText("1 cited file changed since");
    await stale.getByRole("button", { name: "Regenerate" }).click();
    await expect(page).toHaveURL(/\/diagrams\/[0-9a-f-]{36}\?since=/);
    await expect(page.getByTestId("diagram-changes")).toContainText("Drawn again");
  });

  test("the whole app's architecture: its parts by tier, what each holds, every line verified", async ({
    page,
  }) => {
    await page.goto("/acme/api/diagrams");
    await page.getByRole("button", { name: "Map the architecture" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/diagrams\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("verified-ratio")).toHaveText("100% statically verified");
    await expect(
      page.getByRole("group", { name: "View as" }).getByRole("button", { name: "Architecture" }),
    ).toHaveAttribute("aria-pressed", "true");
    const canvas = page.getByTestId("diagram-canvas");
    await expect(canvas.getByTestId("entity-node")).toHaveCount(7);
    for (const tier of ["Frontend", "API", "Services", "Data"]) {
      await expect(canvas.getByTestId("layer-band").filter({ hasText: tier })).toBeVisible();
    }
    await expect(canvas.getByRole("list", { name: "backend/app's members" })).toContainText("user");
    // A part opens as its folder, with what it holds.
    await canvas.getByTestId("entity-node").filter({ hasText: "backend/app/api/routes" }).click();
    const panel = page.getByTestId("node-panel");
    await expect(panel.getByRole("link", { name: "Open the folder" })).toHaveAttribute(
      "href",
      /\/acme\/api\/code\/backend\/app\/api\/routes\?ref=/,
    );
    await expect(panel).toContainText("POST /api/v1/login/access-token");
    await expect(panel.getByRole("link", { name: "Trace from here" })).toHaveCount(0);
  });

  test("the overview opens the architecture as a diagram", async ({ page }) => {
    await page.goto("/acme/api");
    await page.getByRole("link", { name: "Open as an architecture diagram" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/diagrams\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("entity-node").first()).toBeVisible();
  });
});
