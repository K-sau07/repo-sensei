import { expect, test, type BrowserContext, type Page } from "@playwright/test";

const STUB = "http://localhost:8099";
const run = Date.now().toString(36);

/** Choose which GitHub account the OAuth stub signs this browser in as. */
async function asGitHubUser(context: BrowserContext, login: string) {
  await context.addCookies([{ name: "stub_login", value: login, url: STUB }]);
}

async function signInWithGitHub(page: Page) {
  await page.getByRole("link", { name: "Continue with GitHub" }).click();
}

test("sign in with GitHub, create an org, invite a teammate who joins", async ({ browser }) => {
  const owner = await browser.newContext();
  await asGitHubUser(owner, `owner-${run}`);
  const page = await owner.newPage();

  // Signed out → login → GitHub (stub) → callback → a new account starts onboarding.
  await page.goto("/settings/profile");
  await expect(page).toHaveURL(/\/login\?next=%2Fsettings%2Fprofile$/);
  await signInWithGitHub(page);
  await expect(page).toHaveURL(/\/settings\/profile$/);
  await expect(page.getByText(`@owner-${run}`)).toBeVisible();

  await page.goto("/onboarding");
  await page.getByLabel("Organization name").fill(`Team ${run}`);
  await expect(page.getByText("Available.")).toBeVisible();
  await page.getByRole("button", { name: "Create organization" }).click();
  const slug = `team-${run}`;
  await expect(page).toHaveURL(new RegExp(`/${slug}$`));

  // Invite by GitHub username; the link is shown once.
  await page.goto(`/${slug}/settings/members`);
  await page.getByRole("button", { name: "Invite people" }).click();
  await page.getByLabel("Email or GitHub username").fill(`@friend-${run}`);
  await page.getByRole("button", { name: "Send invitation" }).click();
  const link = (await page.getByTestId("invite-link").textContent()) ?? "";
  expect(link).toContain("/invite/");

  // The teammate opens it signed out, signs in with GitHub and lands back on the invitation.
  const friend = await browser.newContext();
  await asGitHubUser(friend, `friend-${run}`);
  const friendPage = await friend.newPage();
  await friendPage.goto(new URL(link).pathname);
  await friendPage.getByRole("link", { name: "Sign in to accept" }).click();
  await signInWithGitHub(friendPage);
  await expect(friendPage).toHaveURL(/\/invite\//);
  await friendPage.getByRole("button", { name: `Join Team ${run}` }).click();
  await expect(friendPage).toHaveURL(new RegExp(`/${slug}$`));

  // The owner sees them, promotes them, then removes them.
  await page.reload();
  const row = page.getByRole("row").filter({ hasText: `@friend-${run}` });
  await expect(row).toBeVisible();
  await row.getByRole("combobox").click();
  await page.getByRole("option", { name: "Admin" }).click();
  await expect(page.getByText(/is now admin/)).toBeVisible();
  await row.getByRole("button", { name: /^Remove/ }).click();
  await expect(page.getByRole("row").filter({ hasText: `@friend-${run}` })).toHaveCount(0);

  // The removed teammate can no longer see the org (and isn't told it exists).
  await friendPage.goto(`/${slug}`);
  await expect(friendPage.getByRole("heading", { name: "Organization not found" })).toBeVisible();

  await owner.close();
  await friend.close();
});

test("a sign-in link finished in another browser is refused (login CSRF)", async ({
  browser,
  baseURL,
}) => {
  // The attacker starts a real sign-in and stops at GitHub's redirect back, keeping its URL.
  const attacker = await browser.newContext();
  await asGitHubUser(attacker, `attacker-${run}`);
  const start = await attacker.request.get(`${baseURL}/api/v1/auth/github/login`, {
    maxRedirects: 0,
  });
  const authorize = start.headers().location ?? "";
  expect(authorize).toContain("/login/oauth/authorize");
  const back = await attacker.request.get(authorize, { maxRedirects: 0 });
  const callback = back.headers().location ?? "";
  expect(callback).toMatch(/\/api\/v1\/auth\/github\/callback\?code=.+&state=.+/);

  // The victim's browser opens that link: refused, and no session is created.
  const victim = await browser.newContext();
  const victimPage = await victim.newPage();
  await victimPage.goto(callback);
  await expect(victimPage).toHaveURL(/\/login\?error=state_mismatch$/);
  await expect(victimPage.getByRole("alert").filter({ hasText: "another browser" })).toBeVisible();
  expect((await victim.cookies()).some((cookie) => cookie.name === "session")).toBe(false);

  // The same link still works in the attacker's own browser, which holds the state cookie.
  const own = await attacker.request.get(callback, { maxRedirects: 0 });
  expect(own.headers().location).toContain("/auth/callback");

  await attacker.close();
  await victim.close();
});

test("signing out everywhere ends every session", async ({ browser }) => {
  const context = await browser.newContext();
  await asGitHubUser(context, `leaver-${run}`);
  const page = await context.newPage();
  await page.goto("/login?next=%2Fsettings%2Fsessions");
  await signInWithGitHub(page);
  await expect(page).toHaveURL(/\/settings\/sessions$/);
  await expect(page.getByText("This device")).toBeVisible();

  await page.getByRole("button", { name: "Sign out everywhere" }).click();
  await page
    .getByRole("dialog", { name: "Sign out everywhere?" })
    .getByRole("button", { name: "Sign out everywhere" })
    .click();
  await expect(page).toHaveURL(/\/login/);

  // The old session cookie, if still present, no longer works.
  await page.goto("/settings/sessions");
  await expect(page).toHaveURL(/\/login\?next=/);
  await context.close();
});
