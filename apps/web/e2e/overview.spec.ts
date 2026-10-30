import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.describe("overview", () => {
  test("an indexed repository says what it does, how it is laid out and what to ask", async ({
    page,
  }) => {
    await page.goto("/acme/api");

    // The last index first, as it ran: its stages and the constellation.
    const last = page.getByRole("region", { name: "Last index" });
    await expect(last.getByRole("list", { name: "Indexing stages" })).toContainText("Code graph");
    await expect(last.getByRole("figure", { name: /Constellation/ })).toBeVisible();

    // What it does, kept to a few lines until asked for more; each folder's, folded away.
    await expect(page.getByRole("heading", { name: "What this repo does" })).toBeVisible();
    await expect(page.getByText(/A full-stack web application template/)).toBeVisible();
    await page.getByRole("button", { name: "Show more" }).click();
    await expect(page.getByRole("button", { name: "Show less" })).toBeVisible();
    const folders = page.getByRole("button", { name: /What each folder holds/ });
    await expect(
      page.getByText("The FastAPI service: routes, models, CRUD and auth."),
    ).toBeHidden();
    await folders.click();
    await expect(
      page.getByText("The FastAPI service: routes, models, CRUD and auth."),
    ).toBeVisible();

    // The modules on a map (those with code or a link) and all of them in a table that opens
    // them.
    const table = page.getByRole("table", { name: "Modules" });
    await expect(table.getByRole("row")).toHaveCount(13);
    await expect(page.getByTestId("architecture-map").locator(".react-flow__node")).toHaveCount(10);
    await expect(
      page.getByText("12 modules · 81 files in the rest · 8 links between them"),
    ).toBeVisible();
    await expect(table.getByRole("link", { name: "backend/app/api/routes/" })).toHaveAttribute(
      "href",
      "/acme/api/code/backend/app/api/routes",
    );

    // Health says what it measured, and what it doesn't track yet.
    const health = page.getByRole("region", { name: "Index health" });
    await expect(health).toContainText("Files with symbols");
    await expect(health).toContainText("Not tracked yet");

    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);

    // A suggested question is asked on the Ask tab.
    await page
      .getByRole("link", { name: "What happens when POST /api/v1/items/ is called?" })
      .click();
    await expect(page).toHaveURL(/\/acme\/api\/ask(\/[0-9a-f-]{36})?$/);
    await expect(page.getByTestId("answer")).toContainText("An item is created by the");
  });

  test("an index without summaries says so rather than making one up", async ({
    page,
    context,
  }) => {
    await context.addInitScript(() => {
      localStorage.setItem("sensei.mock.overview", "bare");
    });
    await page.goto("/acme/api");
    await expect(page.getByText(/No summary: the summaries stage didn.t run/)).toBeVisible();
    await expect(page.getByRole("heading", { name: "Feature maps" })).toHaveCount(0);
    await expect(page.getByRole("region", { name: "Index health" })).toContainText(
      "Not measured at this index",
    );
  });

  test("⌘L asks about the open file and lines, and the scope can be dropped", async ({ page }) => {
    await page.goto("/acme/api/code/backend/app/api/routes/items.py?L=62-72");
    await expect(page.getByText("def create_item(").first()).toBeVisible();
    await page.keyboard.press("ControlOrMeta+l");
    const bar = page.getByTestId("ask-bar");
    await expect(bar).toBeVisible();
    await expect(bar.getByText("items.py:62–72")).toBeVisible();
    await bar.getByRole("textbox", { name: "Your question" }).fill("What does this check?");
    await bar.getByRole("button", { name: "Ask", exact: true }).click();

    await expect(page.getByTestId("question-scope")).toContainText("items.py:62–72");
    await expect(page.getByTestId("answer")).toContainText("An item is created by the");
    await expect(page).toHaveURL(/\/acme\/api\/ask\/[0-9a-f-]{36}$/);

    // Dropped, the question is about the whole repository.
    await page.goto("/acme/api/code/backend/app/api/routes/items.py?L=62");
    await expect(page.getByText("def create_item(").first()).toBeVisible();
    await page.keyboard.press("ControlOrMeta+l");
    await expect(bar.getByText("items.py:62")).toBeVisible();
    await bar.getByRole("button", { name: /Drop the scope/ }).click();
    await expect(bar.getByText("items.py:62")).toHaveCount(0);
    await bar.getByRole("textbox", { name: "Your question" }).fill("Where is auth?");
    await bar.getByRole("textbox", { name: "Your question" }).press("ControlOrMeta+Enter");
    await expect(page.getByTestId("answer")).toContainText("An item is created by the");
    await expect(page.getByTestId("question-scope")).toHaveCount(0);
  });
});

test.describe("org home", () => {
  test("asks any repository, and lists live agents and recent questions", async ({
    page,
    context,
  }) => {
    await context.addInitScript(() => {
      localStorage.setItem("sensei.mock.runs", "live");
    });
    await page.goto("/acme");
    const ask = page.getByRole("form", { name: "Ask anything" });
    await expect(ask.getByRole("combobox", { name: "Repository to ask" })).toContainText("api");
    await expect(page.getByRole("region", { name: "Running now" })).toContainText(
      "How is an item created?",
    );
    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);

    await ask.getByRole("textbox", { name: "Question" }).fill("Where are items saved?");
    await ask.getByRole("button", { name: "Ask" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/ask/);
    await expect(page.getByTestId("answer")).toContainText("An item is created by the");

    // Back home, the question is the latest one.
    await page.goto("/acme");
    const recent = page.getByRole("region", { name: "Your recent questions" });
    await expect(recent.getByRole("link").first()).toContainText("Where are items saved?");
    await recent.getByRole("link").first().click();
    await expect(page).toHaveURL(/\/acme\/api\/ask\/[0-9a-f-]{36}$/);
  });
});
