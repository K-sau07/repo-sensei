import { expect, test, useMockScenario } from "./fixtures";

test.describe("signed out", () => {
  test.use({ signedIn: false });

  test("signed-in pages send you to sign in and remember where you were", async ({ page }) => {
    await page.goto("/acme/inbox?tab=mentions");
    await expect(page).toHaveURL(/\/login\?next=%2Facme%2Finbox%3Ftab%3Dmentions$/);
    const button = page.getByRole("link", { name: "Continue with GitHub" });
    await expect(button).toHaveAttribute(
      "href",
      "/api/v1/auth/github/login?next=%2Facme%2Finbox%3Ftab%3Dmentions",
    );
  });

  test("public pages stay public", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/$/);
  });

  test("sign-in errors are explained", async ({ page }) => {
    await page.goto("/login?error=access_denied");
    // (Next's route announcer is also role="alert", so match ours by its text.)
    await expect(
      page.getByRole("alert").filter({ hasText: "You cancelled the sign-in" }),
    ).toBeVisible();
    await page.goto("/login?error=something_new");
    await expect(
      page.getByRole("alert").filter({ hasText: "Sign-in didn't complete" }),
    ).toBeVisible();
  });

  test("an unsafe next is never followed", async ({ page }) => {
    await page.goto("/login?next=//evil.example/steal");
    await expect(page.getByRole("link", { name: "Continue with GitHub" })).toHaveAttribute(
      "href",
      "/api/v1/auth/github/login",
    );
  });
});

test.describe("after GitHub sends you back", () => {
  test("you land where you were going", async ({ page }) => {
    await page.goto("/auth/callback?next=%2Facme%2Factivity");
    await expect(page).toHaveURL(/\/acme\/activity$/);
  });

  test("otherwise you land in your last organization", async ({ page }) => {
    await page.goto("/auth/callback");
    await expect(page).toHaveURL(/\/acme$/);
  });

  test("with no organization yet you start onboarding", async ({ context, page }) => {
    await useMockScenario(context, "no-orgs");
    await page.goto("/auth/callback");
    await expect(page).toHaveURL(/\/onboarding$/);
  });
});

test("an expired session says so and returns you to sign in, keeping your place", async ({
  context,
  page,
}) => {
  await useMockScenario(context, "expired");
  await page.goto("/acme/insights");
  await expect(page.getByText("Your session ended. Sign in again to continue.")).toBeVisible();
  await expect(page).toHaveURL(/\/login\?next=%2Facme%2Finsights$/);
});

test("sessions can be revoked one by one, or all at once", async ({ page }) => {
  await page.goto("/settings/sessions");
  const row = (device: string) => page.getByRole("listitem").filter({ hasText: device });
  await expect(row("Chrome on macOS")).toContainText("This device");

  await page.getByRole("button", { name: "Revoke Safari on iOS" }).click();
  await expect(page.getByText("Signed out of Safari on iOS")).toBeVisible();
  await expect(row("Safari on iOS")).toHaveCount(0);

  await page.getByRole("button", { name: "Sign out everywhere" }).click();
  const confirm = page.getByRole("dialog", { name: "Sign out everywhere?" });
  await expect(confirm).toBeVisible();
  await confirm.getByRole("button", { name: "Sign out everywhere" }).click();
  await expect(page).toHaveURL(/\/login/);
});

test("the profile shows what GitHub told us", async ({ page }) => {
  await page.goto("/settings/profile");
  await expect(page.getByRole("heading", { name: "Octo Cat" })).toBeVisible();
  await expect(page.getByText("@octo")).toBeVisible();
  await expect(page.getByRole("link", { name: "Back to Acme" })).toHaveAttribute("href", "/acme");
});

test("every page carries a strict, per-request script policy", async ({ page }) => {
  const first = await page.goto("/login");
  const second = await page.goto("/login");
  const policy = first?.headers()["content-security-policy"] ?? "";
  expect(policy).toMatch(/script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'/);
  expect(policy).not.toContain("script-src 'self' 'unsafe-inline'");
  // A fresh nonce every time.
  expect(second?.headers()["content-security-policy"]).not.toBe(policy);
  // Nothing on the page was blocked by it.
  const blocked: string[] = [];
  page.on("console", (message) => {
    if (/Content Security Policy/i.test(message.text())) blocked.push(message.text());
  });
  await page.goto("/settings/profile");
  await expect(page.getByRole("heading", { name: "Octo Cat" })).toBeVisible();
  expect(blocked).toEqual([]);
});
