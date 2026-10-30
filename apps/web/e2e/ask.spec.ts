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
    await expect(page.getByTestId("question")).toHaveText("How is an item created?");
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

test.describe("ask: what the tools found", () => {
  test("cards show the lookups, promote to the side panel, and suggest what to ask next", async ({
    page,
  }) => {
    await page.goto("/acme/api/ask");
    const box = page.getByRole("textbox", { name: "Ask about this repository" });
    await box.fill("How is an item created?");
    await box.press("ControlOrMeta+Enter");
    const answer = page.getByTestId("answer");
    await expect(answer).toContainText("An item is created by the");

    const cards = page.getByTestId("tool-cards");
    await expect(cards.getByTestId("tool-card")).toHaveCount(4); // three lookups and endpoints
    const path = cards.getByRole("list", { name: "Traced path" });
    await expect(path.getByRole("listitem")).toHaveCount(5);
    await expect(cards.getByRole("table", { name: "Endpoints" })).toContainText("/api/v1/items/");

    await cards
      .getByRole("button", { name: "Promote Trace from AddItem to the side panel" })
      .click();
    const panel = page.getByRole("complementary", { name: "Artifact" });
    await expect(panel).toContainText("Trace from AddItem");
    await expect(panel.getByRole("link", { name: "Open in Trace" })).toHaveAttribute(
      "href",
      "/acme/api/trace?from=AddItem",
    );
    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);

    await panel.getByRole("button", { name: "Close the artifact" }).click();
    const rail = page.getByTestId("evidence-rail");
    await expect(rail).toBeVisible();
    // The rail's Graph tab: what the run walked; its searches run again on the search page.
    await rail.getByRole("tab", { name: "Graph (2)" }).click();
    await expect(rail.getByRole("list", { name: "Traced path" })).toBeVisible();
    await rail.getByRole("tab", { name: /^Searches/ }).click();
    await expect(rail.getByRole("link", { name: "code_search 'create item'" })).toHaveAttribute(
      "href",
      "/acme/api/search?q=create+item",
    );

    const next = page.getByRole("navigation", { name: "Ask next" });
    await next.getByRole("button", { name: "Where is `Item` tested?" }).click();
    await expect(page.getByTestId("answer")).toHaveCount(2);
    await expect(page.getByText("Where is `Item` tested?").first()).toBeVisible();
  });
});

test.describe("ask: scope and mentions", () => {
  test("@ mentions a symbol, Scope picks layers and a folder, and the question keeps them", async ({
    page,
  }) => {
    await page.goto("/acme/api/ask");
    const box = page.getByRole("textbox", { name: "Ask about this repository" });
    await box.fill("what calls @create_it");
    const mentions = page.getByRole("listbox", { name: "Mention a file or symbol" });
    await expect(mentions.getByRole("option").first()).toBeVisible();
    await expect(box).toHaveAttribute("aria-activedescendant", /.+/);
    await box.press("Enter"); // picks the first suggestion, it doesn't send
    await expect(box).toHaveValue(/^what calls `[\w./-]+` $/);
    await expect(mentions).toHaveCount(0);

    await page.getByRole("button", { name: "Scope the question" }).click();
    const picker = page.getByRole("dialog", { name: "Scope" });
    await picker.getByRole("button", { name: "API" }).click();
    await picker.getByRole("button", { name: "Data" }).click();
    await picker.getByLabel("A file or folder").fill("backend/app/");
    await picker.getByRole("button", { name: "Folder backend/app/" }).click();
    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);
    await page.keyboard.press("Escape");
    const chips = page.getByLabel("Scope of the question").getByTestId("scope-chip");
    await expect(chips).toHaveText(["app/", "API", "Data"]);

    await box.press("ControlOrMeta+Enter");
    await expect(page.getByTestId("question-scope")).toHaveText(/app\/ · API · Data/);
    await expect(page.getByTestId("answer")).toContainText("An item is created by the");
    // The scope stays for the next question, until a chip is removed.
    await page.getByRole("button", { name: "Remove the scope Data" }).click();
    await expect(chips).toHaveText(["app/", "API"]);
  });
});

test.describe("ask: a new conversation", () => {
  test("New conversation starts an empty thread, and its question a conversation of its own", async ({
    page,
  }) => {
    await page.goto("/acme/api/ask");
    const box = page.getByRole("textbox", { name: "Ask about this repository" });
    await box.fill("How is an item created?");
    await box.press("ControlOrMeta+Enter");
    await expect(page.getByTestId("answer")).toContainText("An item is created by the");
    await expect(page).toHaveURL(/\/acme\/api\/ask\/[0-9a-f-]{36}$/);
    const first = page.url();

    await page.getByRole("link", { name: "New conversation" }).first().click();
    await expect(page).toHaveURL(/\/acme\/api\/ask$/);
    await expect(page.getByTestId("answer")).toHaveCount(0);
    await box.fill("Where are passwords hashed?");
    await box.press("ControlOrMeta+Enter");
    await expect(page.getByTestId("answer")).toHaveCount(1);
    await expect(page).toHaveURL(/\/acme\/api\/ask\/[0-9a-f-]{36}$/);
    expect(page.url()).not.toBe(first);
  });
});
