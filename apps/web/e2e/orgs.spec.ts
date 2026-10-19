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

/** Change who the mock API thinks is signed in (it persists across reloads). */
async function actAs(page: Page, scenario: "signed-in" | "as-friend" | "as-member") {
  await page.evaluate((value) => {
    if (value === "signed-in") localStorage.removeItem("sensei.mock.scenario");
    else localStorage.setItem("sensei.mock.scenario", value);
  }, scenario);
}

test.describe("onboarding", () => {
  test("names an org, checks the URL and creates it", async ({ page }) => {
    await page.goto("/onboarding");
    await expect(page.getByRole("list", { name: "Onboarding steps" })).toBeVisible();
    await noAxeViolations(page);

    const name = page.getByLabel("Organization name");
    const url = page.getByLabel("URL");
    const create = page.getByRole("button", { name: "Create organization" });

    await name.fill("Acme");
    await expect(url).toHaveValue("acme");
    await expect(page.getByText("That name is already taken.")).toBeVisible();
    await expect(create).toBeDisabled();

    await url.fill("api");
    await expect(page.getByText("That name is reserved.")).toBeVisible();

    await name.fill("Beta Labs");
    await expect(url).toHaveValue("api"); // edited by hand, so it no longer follows the name
    await url.fill("beta-labs");
    await expect(page.getByText("Available.")).toBeVisible();
    await create.click();

    await expect(page).toHaveURL(/\/beta-labs$/);
    await expect(
      page.getByRole("button", { name: "Beta Labs (switch organization)" }),
    ).toBeVisible();
  });

  test("⌘⇧O switches orgs and the last one opened is remembered", async ({ page }) => {
    await page.goto("/onboarding");
    await page.getByLabel("Organization name").fill("Gamma");
    await expect(page.getByText("Available.")).toBeVisible();
    await page.getByRole("button", { name: "Create organization" }).click();
    await expect(page).toHaveURL(/\/gamma$/);
    await expect(page.locator("html")).toHaveAttribute("data-shell-ready", "true");

    await page.keyboard.press("ControlOrMeta+Shift+KeyO");
    await page.getByRole("menuitem", { name: /Acme/ }).click();
    await expect(page).toHaveURL(/\/acme$/);

    // Sign-in later returns to the org opened last.
    await expect
      .poll(async () => {
        await page.goto("/auth/callback");
        await page.waitForURL(/\/(acme|gamma)$/);
        return new URL(page.url()).pathname;
      })
      .toBe("/acme");
  });
});

test("invite, accept as the invitee, change their role, then remove them", async ({ page }) => {
  await page.goto("/acme/settings/members");
  await expect(page.getByRole("heading", { name: "Members" })).toBeVisible();
  await noAxeViolations(page);

  await page.getByRole("button", { name: "Invite people" }).click();
  await page.getByLabel("Email or GitHub username").fill("friend@example.com");
  await page.getByRole("button", { name: "Send invitation" }).click();
  const link = await page.getByTestId("invite-link").textContent();
  expect(link).toMatch(/\/invite\/mock-/);
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByText("friend@example.com", { exact: true })).toBeVisible(); // pending

  await actAs(page, "as-friend");
  await page.goto(link ?? "");
  await expect(page.getByRole("heading", { name: "Join Acme" })).toBeVisible();
  await noAxeViolations(page);
  await page.getByRole("button", { name: "Join Acme" }).click();
  await expect(page).toHaveURL(/\/acme$/);

  await actAs(page, "signed-in");
  await page.goto("/acme/settings/members");
  const row = page.getByRole("row").filter({ hasText: "@friend" });
  await row.getByRole("combobox", { name: "Role of A Friend" }).click();
  await page.getByRole("option", { name: "Admin" }).click();
  await expect(page.getByText("A Friend is now admin")).toBeVisible();

  // Removing asks first; cancelling keeps them.
  await row.getByRole("button", { name: "Remove A Friend" }).click();
  const confirm = page.getByRole("alertdialog", { name: "Remove A Friend?" });
  await expect(confirm).toBeVisible();
  await noAxeViolations(page);
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Remove A Friend" }).click();
  await confirm.getByRole("button", { name: "Remove" }).click();
  await expect(page.getByText("Removed A Friend")).toBeVisible();
  await expect(confirm).toHaveCount(0);
  await expect(page.getByRole("row").filter({ hasText: "@friend" })).toHaveCount(0);
});

test("the only owner is told how to leave instead of being offered a failing button", async ({
  page,
}) => {
  await page.goto("/acme/settings/members");
  const me = page.getByRole("row").filter({ hasText: "You" });
  await expect(me.getByText("Make someone else an owner to leave")).toBeVisible();
  await expect(me.getByRole("button", { name: "Leave" })).toHaveCount(0);
});

test("renaming the org's URL moves you there without a not-found flash", async ({ page }) => {
  await page.goto("/acme/settings/general");
  const notFound: string[] = [];
  // Record any moment the gate replaces the page while the slug changes.
  await page.exposeFunction("sawNotFound", () => notFound.push("seen"));
  await page.evaluate(() => {
    new MutationObserver(() => {
      if (document.body.innerText.includes("Organization not found")) {
        void (window as unknown as { sawNotFound: () => Promise<void> }).sawNotFound();
      }
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  });
  const url = page.getByLabel("URL");
  await url.fill("acme-renamed");
  await expect(page.getByText("Available.")).toBeVisible();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page).toHaveURL(/\/acme-renamed\/settings\/general$/);
  await expect(page.getByLabel("URL")).toHaveValue("acme-renamed");
  // The switcher and links follow the new slug.
  await expect(page.getByRole("link", { name: "General" })).toHaveAttribute(
    "href",
    "/acme-renamed/settings/general",
  );
  expect(notFound).toEqual([]);
});

test("members who aren't admins see why they can't manage the org", async ({ context, page }) => {
  await useMockScenario(context, "as-member");
  await page.goto("/acme/settings/members");
  await expect(page.getByText("Only admins and owners can invite or change roles.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Invite people" })).toHaveCount(0);
  await expect(page.getByRole("combobox")).toHaveCount(0);

  await page.goto("/acme/settings/general");
  await expect(page.getByLabel("Name")).toBeDisabled();
  await expect(page.getByText("Only admins and owners can change these.")).toBeVisible();
});

test.describe("invitation links", () => {
  test("closed and invalid links explain themselves", async ({ page }) => {
    await page.goto("/invite/seed-expired-token");
    await expect(page.getByRole("heading", { name: "This invitation has expired" })).toBeVisible();
    await page.goto("/invite/seed-accepted-token");
    await expect(
      page.getByRole("heading", { name: "This invitation was already used" }),
    ).toBeVisible();
    await page.goto("/invite/not-a-real-token");
    await expect(
      page.getByRole("heading", { name: "This invitation link isn’t valid" }),
    ).toBeVisible();
  });

  test("the wrong account is told so", async ({ page }) => {
    await page.goto("/invite/seed-pending-token"); // sent to someone@example.com
    await page.getByRole("button", { name: "Join Acme" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "different account" })).toContainText(
      "@octo",
    );
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  });

  test.describe("signed out", () => {
    test.use({ signedIn: false });

    test("offers sign-in and comes back to the invitation", async ({ context, page }) => {
      await useMockScenario(context, "signed-out");
      await page.goto("/invite/seed-pending-token");
      await expect(page.getByRole("heading", { name: "Join Acme" })).toBeVisible();
      await expect(page.getByRole("link", { name: "Sign in to accept" })).toHaveAttribute(
        "href",
        "/login?next=%2Finvite%2Fseed-pending-token",
      );
    });
  });
});

test("an org you can't see is simply not found", async ({ page }) => {
  await page.goto("/someone-elses-org");
  await expect(page.getByRole("heading", { name: "Organization not found" })).toBeVisible();
  await noAxeViolations(page);
});
