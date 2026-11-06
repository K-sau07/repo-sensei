import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { expect, test, useMockScenario } from "./fixtures";

async function noAxeViolations(page: Page) {
  // Check the settled page: colors mid fade-in (dialogs, toasts) aren't what people read.
  await page.evaluate(() =>
    Promise.all(
      document
        .getAnimations()
        .filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => undefined)),
    ),
  );
  const { violations } = await new AxeBuilder({ page }).analyze();
  expect(violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`)).toEqual([]);
}

test.describe("API keys", () => {
  test("a key is shown once, then listed by its prefix, and revoked", async ({ page }) => {
    await page.goto("/acme/settings/api-keys");
    const rows = page.getByRole("row");
    // An owner sees everyone's keys; revoked and expired ones on request.
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(1)).toContainText("laptop");
    await expect(rows.nth(2)).toContainText("ci");
    await expect(rows.nth(2)).toContainText("@hubot");
    await page.getByRole("button", { name: "Show 1 revoked or expired key" }).click();
    await expect(rows.nth(3)).toContainText("old ci");
    await expect(rows.nth(3)).toContainText("Revoked");
    await page.getByRole("button", { name: "Hide revoked and expired keys" }).click();
    await noAxeViolations(page);

    await page.getByRole("button", { name: "Create key" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("button", { name: "Create key" })).toBeDisabled();
    await dialog.getByLabel("Name").fill("deploy bot");
    await dialog.getByRole("checkbox", { name: /Ask questions/ }).click();
    await dialog.getByRole("button", { name: "Create key" }).click();

    const key = dialog.getByTestId("new-key");
    await expect(key).toHaveText(/^rs_[0-9A-Za-z]{8}_[0-9A-Za-z]{43}$/);
    await expect(dialog.getByRole("alert")).toContainText("the only time it's shown");
    await expect(dialog.getByRole("button", { name: "Copy API key" })).toBeVisible();
    await noAxeViolations(page);
    const prefix = ((await key.textContent()) ?? "").split("_")[1] ?? "";
    // A click outside doesn't lose it; the button does close it.
    await page.mouse.click(5, 5);
    await expect(key).toBeVisible();
    await dialog.getByRole("button", { name: "I've copied it" }).click();
    await expect(dialog).toHaveCount(0);

    const created = rows.filter({ hasText: "deploy bot" });
    await expect(created).toContainText(`rs_${prefix}_…`);
    await expect(created).toContainText("read:repo");
    await expect(created).toContainText("ask");
    await expect(page.getByText(/rs_[0-9A-Za-z]{8}_[0-9A-Za-z]{43}/)).toHaveCount(0);

    await created.getByRole("button", { name: "Revoke deploy bot" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Revoke key" }).click();
    await expect(created).toHaveCount(0);
    await page.getByRole("button", { name: "Show 2 revoked or expired keys" }).click();
    await expect(created).toContainText("Revoked");
    await expect(created.getByRole("button", { name: /Revoke/ })).toHaveCount(0);
  });

  test("a member sees only their own keys", async ({ context, page }) => {
    await useMockScenario(context, "as-member");
    await page.goto("/acme/settings/api-keys");
    await expect(page.getByRole("row")).toHaveCount(2);
    await expect(page.getByRole("row").nth(1)).toContainText("ci");
    await expect(page.getByText("@octo")).toHaveCount(0);
  });
});

test.describe("connecting an editor", () => {
  test("each editor's setup, remote or local, with the right file and root key", async ({
    page,
    baseURL,
  }) => {
    await page.goto("/acme/settings/mcp");
    const mcp = `${baseURL ?? ""}/mcp`;
    await expect(page.getByTestId("mcp-url")).toHaveText(mcp);
    await expect(
      page.getByText(`claude mcp add --transport http repo-sensei ${mcp}`),
    ).toBeVisible();
    await expect(page.getByText("Or put this in .mcp.json:")).toBeVisible();
    await noAxeViolations(page);

    await page.getByRole("tab", { name: "VS Code" }).click();
    const vscode = page.getByRole("tabpanel", { name: "VS Code" });
    await expect(vscode.getByText("Or put this in .vscode/mcp.json:")).toBeVisible();
    await expect(vscode.locator("pre").last()).toContainText('"servers"');
    const local = vscode.getByRole("tab", { name: "API key, run locally" });
    await local.click();
    await expect(local).toHaveAttribute("aria-selected", "true");
    await expect(vscode.locator("pre")).toContainText('"${input:sensei-api-key}"');
    await expect(vscode.locator("pre")).toContainText('"password": true');
    await expect(vscode.getByRole("link", { name: "API key" })).toHaveAttribute(
      "href",
      "/acme/settings/api-keys",
    );

    await page.getByRole("tab", { name: "Cursor" }).click();
    const cursor = page.getByRole("tabpanel", { name: "Cursor" });
    await expect(cursor.getByText("Put this in .cursor/mcp.json:")).toBeVisible();
    await expect(cursor.locator("pre")).toContainText('"mcpServers"');
  });

  test("test connection says whether editors can connect", async ({ page, context }) => {
    await page.goto("/acme/settings/mcp");
    await page.getByRole("button", { name: "Test connection" }).click();
    const result = page.getByRole("status").filter({ hasText: "editors" });
    await expect(result).toContainText("Ready for your editor.");
    for (const check of ["From your browser", "Sign-in challenge", "Sign-in document", "Tools"]) {
      await expect(result).toContainText(check);
    }
    await expect(result.getByLabel("Passed")).toHaveCount(4);
    await expect(result).toContainText("sensei_trace");
    await noAxeViolations(page);

    await context.addInitScript(() => {
      localStorage.setItem("sensei.mock.mcp", "broken");
    });
    await page.reload();
    await page.getByRole("button", { name: "Test connection" }).click();
    await expect(result).toContainText("editors can't connect yet");
    await expect(result.getByLabel("Failed")).toHaveCount(1);
    await expect(result).toContainText("TimeoutError");
  });
});

test.describe("Slack", () => {
  test("an admin connects the org's Slack, sees only the webhook's end, and disconnects it", async ({
    page,
  }) => {
    await page.goto("/acme/settings/integrations");
    await page
      .getByLabel("Incoming webhook URL")
      .fill("https://hooks.slack.com/services/T0001/B0002/abcDEF1234");
    await page.getByLabel("Channel name (to show here)").fill("#eng-alerts");
    await page.getByRole("button", { name: "Connect Slack" }).click();
    await expect(page.getByTestId("slack-connected")).toContainText("#eng-alerts");
    await expect(page.getByTestId("slack-connected")).toContainText("…1234");
    await noAxeViolations(page);
    await page.reload();
    await expect(page.getByTestId("slack-connected")).toContainText("…1234");
    await page.getByRole("button", { name: "Disconnect Slack" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Disconnect" }).click();
    await expect(page.getByRole("button", { name: "Connect Slack" })).toBeVisible();
  });

  test("members and viewers are pointed to their own notification settings", async ({
    page,
    context,
  }) => {
    await useMockScenario(context, "as-viewer");
    await page.goto("/acme/settings/integrations");
    await expect(page.getByTestId("slack-admins-only")).toBeVisible();
    await page.getByRole("link", { name: "your notification settings" }).click();
    await expect(page).toHaveURL(/\/settings\/notifications$/);
  });
});

test.describe("models", () => {
  test("a step is raised, kept after a reload, and put back", async ({ page }) => {
    await page.goto("/acme/settings/models");
    const fixes = page.getByRole("region", { name: "Fixes and changes" });
    await expect(fixes).toContainText("Recommended: Sonnet 5.5 at medium effort");
    await noAxeViolations(page);
    await fixes.getByRole("combobox", { name: "Fixes and changes: effort" }).click();
    // Only what keeps it accurate: a fix can't be set below its medium effort.
    await expect(page.getByRole("option")).toHaveText(["Recommended", "At least high"]);
    await page.getByRole("option", { name: "At least high" }).click();
    await expect(page.getByText("Fixes and changes updated")).toBeVisible();
    await page.reload();
    const effort = page
      .getByRole("region", { name: "Fixes and changes" })
      .getByRole("combobox", { name: "Fixes and changes: effort" });
    await expect(effort).toContainText("At least high");
    await effort.click();
    await page.getByRole("option", { name: "Recommended" }).click();
    await expect(effort).toContainText("Recommended");
  });
});

test.describe("insights", () => {
  test("the month's questions, answers, repositories, modules and weeks", async ({ page }) => {
    await page.goto("/acme/insights");
    await expect(page.getByRole("heading", { level: 1, name: "Insights" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Answered from the code" })).toContainText("%");
    await expect(page.getByRole("img", { name: /Questions each day/ })).toBeVisible();
    await page.getByText("The modules, as a table").click();
    await expect(page.getByRole("table", { name: "Most cited modules" })).toContainText(
      "backend/app",
    );
    await noAxeViolations(page);
  });
});

test.describe("internal evals", () => {
  test("staff see runs by regressions and a run against its baseline", async ({ page }) => {
    await page.goto("/internal?sort=regressions");
    const rows = page.getByTestId("eval-run");
    await expect(rows.first()).toContainText("3 regressions");
    await noAxeViolations(page);
    await rows.first().getByRole("link").click();
    await expect(page).toHaveURL(/\/internal\/evals\/[0-9a-f-]{36}$/);
    await expect(page.getByRole("table", { name: "Metrics against the baseline" })).toBeVisible();
    const item = page.getByTestId("eval-item").first();
    await item.locator("summary").click();
    await expect(item.getByText("Gold it missed")).toBeVisible();
    await noAxeViolations(page);
  });

  test("someone who isn't staff is told the page isn't there", async ({ context, page }) => {
    await useMockScenario(context, "as-member");
    await page.goto("/internal");
    await expect(page.getByText(/not found|couldn.t find/i).first()).toBeVisible();
    await expect(page.getByRole("table", { name: "Eval runs" })).toHaveCount(0);
  });
});
