import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.describe("ask", () => {
  test("a question streams its answer, with citations that open their code", async ({ page }) => {
    await page.goto("/acme/api/ask");
    await expect(page.getByRole("heading", { name: "Ask about this repository" })).toBeVisible();

    const box = page.getByRole("textbox", { name: "Ask about this repository" });
    await box.fill("How is an item created?");
    await box.press("ControlOrMeta+Enter");

    // The question, then what the run is doing while it writes.
    await expect(page.getByText("How is an item created?")).toBeVisible();
    await expect(page.getByTestId("status-line")).toContainText(/Searching code|Reading files/);

    // The answer, a chip after the words each citation backs.
    const answer = page.getByTestId("answer");
    await expect(answer).toContainText("An item is created by the");
    const chips = answer.getByTestId("citation-chip");
    await expect(chips).toHaveCount(2);
    await expect(chips.first()).toContainText("items.py:62–72");
    await expect(page.getByTestId("confidence")).toHaveText("Confidence: high");
    await expect(page.getByTestId("announcer")).toHaveText("Answer ready, 2 citations");

    // The new conversation has an address of its own, and the answer didn't restart.
    await expect(page).toHaveURL(/\/acme\/api\/ask\/[0-9a-f-]{36}$/);
    await expect(chips).toHaveCount(2);

    // A chip's peek shows the lines it cites; from there the code opens beside the answer.
    await chips.first().click();
    const peek = page.getByTestId("citation-peek");
    await expect(peek).toContainText("backend/app/api/routes/items.py");
    await expect(peek).toContainText("def create_item(");
    await peek.getByRole("button", { name: "Open beside the answer" }).click();
    await expect(page.getByTestId("cited-code")).toContainText("backend/app/api/routes/items.py");

    // The rail lists the sources by file.
    await page.getByTestId("cited-code").getByRole("button", { name: "Close" }).click();
    const rail = page.getByTestId("evidence-rail");
    await expect(rail).toContainText("backend/app/models.py");

    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);
  });

  test("a draft survives a reload", async ({ page }) => {
    await page.goto("/acme/api/ask");
    const box = page.getByRole("textbox", { name: "Ask about this repository" });
    await box.fill("Where are passwords hashed?");
    await page.reload();
    await expect(page.getByRole("textbox", { name: "Ask about this repository" })).toHaveValue(
      "Where are passwords hashed?",
    );
  });
});
