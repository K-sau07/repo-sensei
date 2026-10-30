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

  test("an answer it can't stand behind says so, and offers a narrower question", async ({
    page,
    context,
  }) => {
    await context.addInitScript(() => {
      localStorage.setItem("sensei.mock.ask", "abstain");
    });
    await page.goto("/acme/api/ask");
    const box = page.getByRole("textbox", { name: "Ask about this repository" });
    await box.fill("Where are payments refunded?");
    await box.press("ControlOrMeta+Enter");

    const card = page.getByTestId("abstain-card");
    await expect(card).toContainText("I couldn't verify this");
    await expect(card).toContainText("POST /items/");
    await expect(page.getByTestId("confidence")).toHaveText("Confidence: low");
    await page.getByTestId("confidence").click();
    await expect(page.getByTestId("confidence-breakdown")).toContainText("1 step not traced");
    await page.keyboard.press("Escape");

    await card.getByRole("button", { name: "Ask: Where are items deleted?" }).click();
    await expect(page.getByText("Where are items deleted?", { exact: true })).toBeVisible();
    await expect(page.getByTestId("answer")).toHaveCount(2);
  });

  test("feedback, renaming and finding a conversation", async ({ page }) => {
    await page.goto("/acme/api/ask");
    const box = page.getByRole("textbox", { name: "Ask about this repository" });
    await box.fill("How is an item created?");
    await box.press("ControlOrMeta+Enter");
    await expect(page.getByTestId("announcer")).toHaveText("Answer ready, 2 citations");

    const helpful = page.getByRole("button", { name: "Helpful" });
    await helpful.click();
    await expect(helpful).toHaveAttribute("aria-pressed", "true");
    await page.getByRole("button", { name: "Something's wrong" }).click();
    const form = page.getByTestId("feedback-form");
    await form.getByRole("button", { name: "Wrong file" }).click();
    await form.getByRole("textbox", { name: "Anything to add" }).fill("It's in crud.py");
    await form.getByRole("button", { name: "Send" }).click();
    await expect(page.getByRole("button", { name: "Something's wrong" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(helpful).toHaveAttribute("aria-pressed", "false");

    // Read back after a reload: what was said is the conversation's.
    await page.reload();
    await expect(page.getByRole("button", { name: "Something's wrong" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    const history = page.getByRole("navigation", { name: "Conversations" });
    await history.getByRole("button", { name: "Rename How is an item created?" }).click();
    const title = history.getByRole("textbox", { name: "Conversation title" });
    await title.fill("Creating items");
    await title.press("Enter");
    await expect(history.getByRole("link", { name: "Creating items" })).toBeVisible();

    await history.getByRole("textbox", { name: "Find a conversation" }).fill("deleting");
    await expect(history).toContainText("No conversation is titled like that.");
    await history.getByRole("textbox", { name: "Find a conversation" }).fill("creat");
    await expect(history.getByRole("link", { name: "Creating items" })).toBeVisible();
  });

  test("a shared link opens read-only, its citations hidden from someone signed out", async ({
    page,
    context,
  }) => {
    await page.goto("/acme/api/ask");
    const box = page.getByRole("textbox", { name: "Ask about this repository" });
    await box.fill("How is an item created?");
    await box.press("ControlOrMeta+Enter");
    await expect(page.getByTestId("announcer")).toHaveText("Answer ready, 2 citations");

    await page.getByRole("button", { name: "Share" }).click();
    const dialog = page.getByTestId("share-dialog");
    await dialog.getByRole("button", { name: "Create link" }).click();
    const link = await dialog.getByRole("textbox", { name: "Share link" }).inputValue();
    expect(link).toMatch(/\/s\/[0-9a-f]{32}$/);

    // The owner sees where each citation points.
    await page.goto(link);
    const shared = page.getByTestId("shared");
    await expect(shared).toContainText("How is an item created?");
    await expect(shared.getByTestId("citation-chip").first()).toContainText("items.py:62–72");

    // Signed out: the words, each citation only its number.
    await page.evaluate(() => {
      localStorage.setItem("sensei.mock.scenario", "signed-out");
    });
    await context.clearCookies();
    await page.goto(link);
    await expect(page.getByTestId("redacted-note")).toContainText("The code this cites is hidden");
    await expect(shared).toContainText("An item is created by the");
    await expect(shared.getByTestId("citation-chip")).toHaveCount(2);
    await expect(shared).not.toContainText("items.py");
    await expect(page.getByRole("button", { name: "Helpful" })).toHaveCount(0);

    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);
  });
});
