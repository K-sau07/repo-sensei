import { expect, test, useMockScenario } from "./fixtures";

const LOW = "/acme/api/fixes/0199f000-0000-7000-8000-00000000f001";
const HIGH = "/acme/api/fixes/0199f000-0000-7000-8000-00000000f005";
const NOT_REPRODUCED = "/acme/api/fixes/0199f000-0000-7000-8000-00000000f003";
const BLOCKED = "/acme/api/fixes/0199f000-0000-7000-8000-00000000f006";
const BUILT = { timeout: 15_000 }; // the mock builds in a few seconds, read every 2 s

test.describe("a fix's run page", () => {
  test("the approval card, then approve: verified, a draft pull request", async ({ page }) => {
    await page.goto(LOW);
    const card = page.getByTestId("approval-card");
    await expect(card.getByTestId("root-cause")).toContainText("count_items divides by per_page");
    await card.getByText(/How it was found/).click();
    await expect(
      card.getByRole("link", { name: "backend/app/api/routes/items.py:30" }).first(),
    ).toHaveAttribute("href", "/acme/api/code/backend/app/api/routes/items.py?L=30-36");
    await expect(card.getByTestId("repro-evidence")).toContainText("ZeroDivisionError");
    await expect(card.getByTestId("fix-plan")).toContainText("Plan (revision 1)");
    await expect(card.getByTestId("merge-readiness")).toHaveText(
      "Merge readiness: once the patch is verified",
    );
    // Approved (⌘↵ asks, as the button does), confirmed.
    await page.keyboard.press("ControlOrMeta+Enter");
    await page.getByRole("alertdialog").getByRole("button", { name: "Approve" }).click();
    await expect(page.getByRole("link", { name: /Draft pull request/ })).toBeVisible(BUILT);
    await expect(page.getByTestId("fix-page").getByTestId("fix-status").first()).toHaveText(
      "PR opened",
    );
    await expect(card.getByTestId("merge-readiness")).toContainText("Merge readiness 4/5");
    await expect(card.getByTestId("diff-file")).toHaveCount(2);
    // The code first, its test after.
    await expect(card.getByTestId("diff-file").first()).toContainText("backend/app/crud.py");
    await expect(card.getByTestId("repro-evidence")).toContainText("1 passed");
    await expect(card.getByTestId("unverified")).toContainText("Accounts created before");
  });

  test("a high-risk fix needs two people", async ({ page, context }) => {
    await page.goto(HIGH);
    const card = page.getByTestId("approval-card");
    await expect(card.getByTestId("approvals-count")).toContainText("0 of 2 people approved");
    await card.getByRole("button", { name: /^Approve plan/ }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Approve" }).click();
    await expect(card.getByTestId("approvals-count")).toContainText("1 of 2 people approved");
    await expect(card.getByRole("button", { name: /^You approved it/ })).toBeDisabled();
    await expect(page.getByTestId("fix-status").first()).toHaveText("Awaiting approval");
    // A second person (a member who can write) approves: it goes on.
    await useMockScenario(context, "as-member");
    await page.reload();
    await card.getByRole("button", { name: /^Approve plan/ }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Approve" }).click();
    await expect(page.getByRole("link", { name: /Draft pull request/ })).toBeVisible(BUILT);
  });

  test("not reproduced: the attempts, no approve, and a retry with more context", async ({
    page,
  }) => {
    await page.goto(NOT_REPRODUCED);
    const ended = page.getByTestId("ended");
    await expect(ended.getByTestId("stop-reason")).toHaveText(
      "No reproducing test: it needs a real browser to expire the cookie.",
    );
    await expect(ended.getByTestId("attempts")).toContainText("1 test run");
    await expect(page.getByRole("button", { name: /^Approve plan/ })).toHaveCount(0);
    await ended.getByLabel(/Retry with more context/).fill("Set the cookie's max-age to 1 second.");
    await ended.getByRole("button", { name: "Retry with this context" }).click();
    await expect(page.getByTestId("fix-status").first()).toHaveText("Queued");
    await expect(page.getByTestId("ended")).toHaveCount(0);
  });

  test("blocked: the rule it hit, and where", async ({ page }) => {
    await page.goto(BLOCKED);
    const hits = page.getByTestId("rule-hits");
    await expect(hits).toContainText("An error is caught instead of fixed");
    await expect(hits).toContainText("backend/app/api/routes/items.py");
  });

  test("a question about the fix is asked in Ask, about its root cause", async ({ page }) => {
    await page.goto(LOW);
    await page.getByLabel("Ask about this fix").fill("Who else calls count_items?");
    await page.getByTestId("side-question").getByRole("button", { name: "Ask" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/ask/);
  });
});
