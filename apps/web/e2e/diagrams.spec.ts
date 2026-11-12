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

  test("the whole app's architecture: a system diagram, its parts named, its lines the graph's", async ({
    page,
  }) => {
    await page.goto("/acme/api/diagrams");
    await page.getByRole("button", { name: "Map the architecture" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/diagrams\/[0-9a-f-]{36}$/);
    // Every line the graph's but the user's, which says why it is inferred.
    await expect(page.getByTestId("verified-ratio")).toHaveText("89% statically verified");
    await expect(page.getByTestId("diagram-summary")).toContainText("full-stack items app");
    await expect(
      page.getByRole("group", { name: "View as" }).getByRole("button", { name: "Architecture" }),
    ).toHaveAttribute("aria-pressed", "true");
    const canvas = page.getByTestId("diagram-canvas");
    await expect(canvas.getByTestId("system-node")).toHaveCount(9);
    for (const row of ["Users", "Frontend", "API", "Services", "Data", "Outside the app"]) {
      await expect(canvas.getByTestId("system-row").filter({ hasText: row })).toBeVisible();
    }
    await expect(
      canvas.locator('[data-store="true"]').filter({ hasText: "PostgreSQL" }),
    ).toBeVisible();
    await expect(canvas.locator(".react-flow__edge.inferred")).toHaveCount(1);
    // A part opens as the folders it is made of.
    await canvas.getByTestId("system-node").filter({ hasText: "REST API" }).click();
    const panel = page.getByTestId("node-panel");
    await expect(panel.getByRole("link", { name: "backend/app/api/routes" })).toHaveAttribute(
      "href",
      /\/acme\/api\/code\/backend\/app\/api\/routes\?ref=/,
    );
    await expect(panel).toContainText("Endpoints for login");
    await expect(panel.getByRole("link", { name: "Trace from here" })).toHaveCount(0);

    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);
  });

  test("the schema as an ER diagram: tables, keys, and the relation between their rows", async ({
    page,
  }) => {
    await page.goto("/acme/api/diagrams");
    await page.getByRole("button", { name: "Map the schema" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/diagrams\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("verified-ratio")).toHaveText("100% statically verified");
    await expect(page.getByRole("group", { name: "View as" })).toHaveCount(0); // only as itself
    const canvas = page.getByTestId("diagram-canvas");
    await expect(canvas.getByTestId("table-node")).toHaveCount(2);
    const item = canvas.getByRole("list", { name: "item's columns" });
    await expect(item).toContainText("owner_id");
    await expect(item).toContainText(/a foreign key to\W+user\.id/);
    await expect(canvas.getByRole("list", { name: "user's columns" })).toContainText(
      "Primary key id",
    );
    await expect(canvas.locator(".react-flow__edge.relation")).toHaveCount(1);
    // A table opens its model's code.
    await canvas.getByTestId("table-node").filter({ hasText: "user" }).first().click();
    await expect(page.getByTestId("node-panel")).toContainText("models.py");

    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);
  });

  test("a question drawn as an ER diagram: the tables its code reaches", async ({ page }) => {
    await page.goto("/acme/api/diagrams");
    await page.getByLabel("New diagram").fill("items");
    await page.getByRole("combobox", { name: "Kind" }).click();
    await page.getByRole("option", { name: "ER diagram" }).click();
    await page.getByRole("button", { name: "Draw", exact: true }).click();
    await expect(page).toHaveURL(/\/acme\/api\/diagrams\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("diagram-canvas").getByTestId("table-node")).toHaveCount(2);
  });

  test("the overview opens the architecture as a diagram", async ({ page }) => {
    await page.goto("/acme/api");
    await page.getByRole("link", { name: "Open as an architecture diagram" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/diagrams\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("system-node").first()).toBeVisible();
  });
});
