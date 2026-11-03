import { expect, test, useMockScenario } from "./fixtures";

const PLANNED = { timeout: 15_000 }; // the mock plans in about a second, read every 1.5 s

test.describe("changes", () => {
  test("describe, plan, edit, approve: a draft pull request with its tests", async ({ page }) => {
    await page.goto("/acme/api/changes");
    await page.getByRole("link", { name: "New change" }).click();
    await page.getByRole("button", { name: /^Add rate limiting to the API/ }).click();
    await page.getByRole("button", { name: "API", exact: true }).click();
    await page.getByRole("button", { name: "Plan this change" }).click();
    await expect(page).toHaveURL(/\/acme\/api\/changes\/[0-9a-f-]{36}$/);
    const view = page.getByTestId("change-page");
    await expect(view.getByTestId("request")).toContainText(
      "Scope: keep the change to these layers: API.",
    );
    // Planned: its files by layer, the test it adds, the pattern it follows.
    const plan = view.getByTestId("plan-canvas");
    await expect(plan).toBeVisible(PLANNED);
    await expect(plan.getByText("Plan (revision 1)")).toBeVisible();
    await expect(plan.getByTestId("planned-file")).toHaveCount(4);
    await expect(plan.getByTestId("planned-test")).toHaveCount(1);
    // Edited in place: a file out, a test in; an edited plan is planned again, not approved.
    await plan
      .getByRole("button", { name: "Remove frontend/src/client/core/request.ts from the plan" })
      .click();
    await plan.getByLabel("Add a test file").fill("backend/app/tests/api/routes/test_window.py");
    await plan.getByLabel("What it checks").fill("The window resets after a minute.");
    await plan.getByTestId("add-test").getByRole("button", { name: "Add" }).click();
    await expect(view.getByRole("button", { name: "Approve plan" })).toBeDisabled();
    await view.getByLabel("A note for the planner (optional)").fill("No client change for now.");
    await view.getByRole("button", { name: "Plan again with my edits" }).click();
    await expect(plan.getByText("Plan (revision 2)")).toBeVisible(PLANNED);
    await expect(plan.getByTestId("planned-file")).toHaveCount(3);
    await expect(plan.getByTestId("planned-test")).toHaveCount(2);
    await expect(view.getByTestId("plan-requests")).toContainText("the last from @octo");
    // Approved as it stands: built, verified, a draft pull request.
    await view.getByRole("button", { name: "Approve plan" }).click();
    await expect(view.getByRole("link", { name: /Draft pull request/ })).toBeVisible(PLANNED);
    await expect(view.getByTestId("fix-status")).toHaveText("PR opened");
    await expect(view.getByTestId("readiness")).toContainText("Merge readiness 4/5");
    await expect(view.getByTestId("built")).toContainText("2 new test file(s)");
    await expect(view.getByRole("button", { name: "Approve plan" })).toHaveCount(0);
  });

  test("a plan is rejected with why, and takes no more decisions", async ({ page }) => {
    await page.goto("/acme/api/changes/0199f000-0000-7000-8000-00000000c001");
    const view = page.getByTestId("change-page");
    await expect(view.getByTestId("plan-canvas")).toBeVisible(PLANNED);
    await view.getByLabel("A note for the planner (optional)").fill("Use the gateway's limiter.");
    await view.getByRole("button", { name: "Reject" }).click();
    await expect(view.getByTestId("fix-status")).toHaveText("Rejected");
    await expect(view.getByTestId("stop-reason")).toHaveText(
      "Rejected by @octo: Use the gateway's limiter.",
    );
    await expect(view.getByRole("button", { name: "Approve plan" })).toHaveCount(0);
  });

  test("viewers follow changes but can't request or decide one", async ({ page, context }) => {
    await useMockScenario(context, "as-viewer");
    await page.goto("/acme/api/changes");
    await expect(page.getByTestId("change-row")).toHaveCount(3);
    await expect(page.getByTestId("viewer-note")).toBeVisible();
    await page.goto("/acme/api/changes/0199f000-0000-7000-8000-00000000c001");
    await expect(page.getByTestId("plan-canvas")).toBeVisible(PLANNED);
    await expect(page.getByRole("button", { name: "Approve plan" })).toHaveCount(0);
    await page.goto("/acme/api/changes/new");
    await expect(page.getByRole("heading", { name: "You need the member role" })).toBeVisible();
  });
});
