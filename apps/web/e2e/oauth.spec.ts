import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

const CALLBACK = "http://127.0.0.1:33418/callback";

test.describe("signing in an editor or the CLI", () => {
  test("an editor asks: who, what for, where the answer goes; allowed, it has a code", async ({
    page,
  }) => {
    // The editor's loopback server, standing in.
    await page.route(`${CALLBACK}**`, (route) =>
      route.fulfill({ status: 200, contentType: "text/html", body: "<p>Signed in</p>" }),
    );
    await page.goto("/oauth/consent?request=mock-request");
    await expect(
      page.getByRole("heading", { name: /^Claude Code wants to use repo-sensei as @/ }),
    ).toBeVisible();
    const scopes = page.getByRole("list", { name: "What it can do" });
    await expect(scopes).toContainText("Read your repositories");
    await expect(scopes).toContainText("Ask questions");
    await expect(page.getByTestId("consent-redirect")).toContainText("127.0.0.1:33418");
    await expect(page.getByTestId("consent-redirect")).toContainText("an app on this computer");

    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);

    await page.getByRole("button", { name: "Allow" }).click();
    await page.waitForURL(/127\.0\.0\.1:33418\/callback\?code=mock-code-/);
    const back = new URL(page.url());
    expect(back.searchParams.get("state")).toBe("xyz");
    expect(back.searchParams.get("iss")).not.toBeNull();
  });

  test("denied, the editor hears access_denied", async ({ page }) => {
    await page.route(`${CALLBACK}**`, (route) =>
      route.fulfill({ status: 200, contentType: "text/html", body: "<p>Denied</p>" }),
    );
    await page.goto("/oauth/consent?request=mock-request");
    await page.getByRole("button", { name: "Deny" }).click();
    await page.waitForURL(/error=access_denied/);
  });

  test("an expired or bad request says so and sends nobody anywhere", async ({ page }) => {
    await page.goto("/oauth/consent?request=mock-expired");
    await expect(page.getByTestId("consent-error")).toContainText("Requests last 10 minutes");
    await page.goto(
      "/oauth/consent?error=invalid_request&error_description=The%20redirect_uri%20isn%27t%20one%20of%20the%20client%27s.",
    );
    await expect(page.getByTestId("consent-error")).toContainText("redirect_uri");
    await expect(page.getByRole("button", { name: "Allow" })).toHaveCount(0);
  });

  test("the CLI's code, typed as people type it, then allowed", async ({ page }) => {
    await page.goto("/device");
    const code = page.getByLabel("Code");
    await code.fill("bcdf ghj");
    await expect(page.getByRole("button", { name: "Continue" })).toBeDisabled();
    await code.fill("bcdf ghjx");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByTestId("device-error")).toContainText("isn't valid");

    await code.fill("bcdfghjk");
    await expect(code).toHaveValue("BCDF-GHJK");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByText("repo-sensei CLI", { exact: true })).toBeVisible();
    const audit = await new AxeBuilder({ page }).include("main").analyze();
    expect(audit.violations).toEqual([]);
    await page.getByRole("button", { name: "Allow" }).click();
    await expect(page.getByTestId("device-done")).toHaveText("You're signed in on your device");
  });

  test("arriving with the code in the link goes straight to the question", async ({ page }) => {
    await page.goto("/device?code=BCDF-GHJK");
    await expect(page.getByRole("button", { name: "Allow" })).toBeVisible();
    await page.getByRole("button", { name: "Deny" }).click();
    await expect(page.getByTestId("device-done")).toHaveText("Sign-in denied");
  });
});
