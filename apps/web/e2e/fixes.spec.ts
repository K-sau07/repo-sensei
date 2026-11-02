import { expect, test, useMockScenario } from "./fixtures";

const TRACE = `Traceback (most recent call last):
  File "/app/backend/app/api/routes/items.py", line 34, in read_items
    count = crud.count_items(session=session, owner_id=current_user.id)
  File "/app/backend/app/crud.py", line 61, in count_items
    return total // per_page
ZeroDivisionError: integer division or modulo by zero`;

test.describe("fixes", () => {
  test("the list is filtered by status, source and risk, kept in the address", async ({ page }) => {
    await page.goto("/acme/api/fixes");
    await expect(page.getByTestId("fix-row")).toHaveCount(4);
    await page.getByRole("combobox", { name: "Status" }).click();
    await page.getByRole("option", { name: "Awaiting approval" }).click();
    await expect(page).toHaveURL(/status=awaiting_approval/);
    await expect(page.getByTestId("fix-row")).toHaveCount(1);
    await page.getByRole("combobox", { name: "Status" }).click();
    await page.getByRole("option", { name: "Any status" }).click();
    await page.getByRole("combobox", { name: "Risk" }).click();
    await page.getByRole("option", { name: "High risk" }).click();
    await expect(page.getByTestId("fix-row")).toHaveCount(1);
    await expect(page.getByTestId("fix-row")).toContainText("Invoice totals");
    await page.goto("/acme/api/fixes?source=sentry");
    await expect(page.getByTestId("fix-row")).toHaveCount(1);
    await expect(page.getByTestId("fix-row")).toContainText("Needs triage");
  });

  test("a pasted trace is previewed as it is typed, then starts a fix", async ({ page }) => {
    await page.goto("/acme/api/fixes");
    await page.getByRole("link", { name: "New fix" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/fixes\/new$/);
    await page.getByLabel("Stack trace or what goes wrong").fill(TRACE);
    const preview = page.getByTestId("trace-preview");
    await expect(preview).toContainText("ZeroDivisionError: integer division or modulo by zero");
    await expect(preview).toContainText("2 frames, 2 in this repository.");
    const crud = preview.getByTestId("frame-chip").filter({ hasText: "backend/app/crud.py:61" });
    await expect(crud).toHaveAttribute("href", "/acme/api/code/backend/app/crud.py?L=61");
    await page.getByRole("button", { name: "Start investigation" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/fixes\/[0-9a-f-]{36}$/);
    await expect(
      page.getByRole("heading", {
        level: 1,
        name: "ZeroDivisionError: integer division or modulo by zero",
      }),
    ).toBeVisible();
    await expect(page.getByTestId("fix-page").getByTestId("fix-status")).toHaveText("Queued");
    await page.getByTestId("fix-page").getByRole("link", { name: "Fixes" }).click();
    await expect(page.getByTestId("fix-row")).toHaveCount(5);
  });

  test("a GitHub issue starts a fix, and one GitHub doesn't have says so", async ({ page }) => {
    await page.goto("/acme/api/fixes/new?tab=issue");
    const issue = page.getByLabel("Issue number or URL");
    await issue.fill("pull request 12");
    await expect(page.getByRole("button", { name: "Start investigation" })).toBeDisabled();
    await issue.fill("#404");
    await page.getByRole("button", { name: "Start investigation" }).click();
    await expect(page.getByText("GitHub has no issue #404.")).toHaveAttribute("role", "alert");
    await issue.fill("https://github.com/acme/api/issues/12");
    await page.getByRole("button", { name: "Start investigation" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/fixes\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("fix-page").getByRole("heading", { level: 1 })).toHaveText(
      "Issue #12: the items count crashes",
    );
  });

  test("a Sentry issue waiting for triage is started from the Sentry tab", async ({ page }) => {
    await page.goto("/acme/api/fixes/new?tab=sentry");
    await expect(page.getByTestId("sentry-status")).toContainText("Connected to acme");
    const waiting = page.getByTestId("sentry-issue");
    await expect(waiting).toHaveCount(1);
    await waiting.getByRole("button", { name: /^Start investigating/ }).click();
    await expect(page).toHaveURL(/\/acme\/api\/fixes\/0199f000-0000-7000-8000-00000000f002$/);
    await expect(page.getByTestId("fix-page").getByTestId("fix-status")).toHaveText("Queued");
  });

  test("viewers follow fixes but can't start one", async ({ page, context }) => {
    await useMockScenario(context, "as-viewer");
    await page.goto("/acme/api/fixes");
    await expect(page.getByTestId("fix-row")).toHaveCount(4);
    await expect(page.getByTestId("viewer-note")).toBeVisible();
    await expect(page.getByRole("link", { name: "New fix" })).toHaveCount(0);
    await page.goto("/acme/api/fixes/new");
    await expect(page.getByRole("heading", { name: "You need the member role" })).toBeVisible();
  });
});
