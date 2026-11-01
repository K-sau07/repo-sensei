import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

const CREATE_USER = "py backend/app/crud.py/create_user().";

test.describe("impact", () => {
  test("pick what to change: its heat map, and what it reaches, ranked", async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.goto("/acme/api/impact");
    await expect(page.getByRole("heading", { name: "What would a change break?" })).toBeVisible();
    await page.getByLabel("What you would change").fill("create_user");
    await page
      .getByRole("list", { name: "Symbols to change" })
      .getByRole("link", { name: /^create_user\s*function\s*backend\/app\/crud\.py/ })
      .click();
    await expect(page).toHaveURL(/\/acme\/api\/impact\?symbol=py\+backend%2Fapp%2Fcrud\.py/);

    await expect(page.getByTestId("impact-total")).toHaveText("9 affected");
    await expect(page.getByTestId("impact-canvas")).toHaveAttribute("data-nodes", "10");
    await expect(page.getByTestId("impact-canvas").locator("canvas").first()).toBeVisible();
    // The routes and the table it reaches, ranked, each with how sure and how far.
    const routes = page.getByTestId("impact-routes");
    await expect(routes.getByRole("heading")).toHaveText("Routes (2)");
    await expect(routes.getByRole("listitem").first()).toContainText(/\d+%\s*2 hops/);
    await expect(page.getByTestId("impact-tables")).toContainText("user");
    await expect(page.getByTestId("impact-symbols")).toContainText("Other code (6)");

    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);

    // As a PR note.
    await page.getByRole("button", { name: "Copy as PR note" }).click();
    await expect(page.getByText("Copied the impact as a PR note")).toBeVisible();
    const note = await page.evaluate(() => navigator.clipboard.readText());
    expect(note).toContain("### Impact of changing `create_user`");
    expect(note).toContain("**Routes** (2)");

    // Following only the surest edges reaches less.
    await page.getByRole("combobox", { name: "Edges followed" }).click();
    await page.getByRole("option", { name: "90% sure or more" }).click();
    await expect(page.getByTestId("impact-total")).not.toHaveText("9 affected");
  });

  test("a name that is nothing in the graph says so", async ({ page }) => {
    await page.goto("/acme/api/impact?symbol=no_such_thing_here");
    await expect(page.getByText("Nothing in the code graph is that")).toBeVisible();
    await expect(page.getByRole("link", { name: "Pick something else" })).toHaveAttribute(
      "href",
      "/acme/api/impact",
    );
  });

  test("reached from a trace's hop and from ⌘K", async ({ page }) => {
    await page.goto(`/acme/api/impact?symbol=${encodeURIComponent(CREATE_USER)}`);
    await expect(page.getByTestId("impact-total")).toHaveText("9 affected");

    await page.goto("/acme/api");
    await expect(page.getByRole("heading", { level: 1, name: "api" })).toBeVisible();
    await page.keyboard.press("ControlOrMeta+k");
    const command = page.getByRole("combobox", { name: "Command" });
    await expect(command).toBeFocused();
    await command.fill("Impact of");
    await page
      .getByRole("dialog", { name: "Command palette" })
      .getByRole("option", { name: /Impact of…/ })
      .click();
    await expect(page).toHaveURL(/\/acme\/api\/impact$/);
  });
});
