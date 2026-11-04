import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.describe("the landing page", () => {
  test.describe("signed out", () => {
    test.use({ signedIn: false });

    test("a visitor asks the demo repository a suggested question and reads its sources", async ({
      page,
    }) => {
      const response = await page.goto("/");
      expect(response?.status()).toBe(200);
      await expect(page).toHaveTitle("repo-sensei: understand any codebase");
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(
        "Understand any codebase, with answers you can verify.",
      );
      const { violations } = await new AxeBuilder({ page }).analyze();
      expect(violations.map((v) => v.id)).toEqual([]);

      const demo = page.getByRole("region", {
        name: "Ask about fastapi/full-stack-fastapi-template",
      });
      await demo.getByRole("button", { name: "How does the API routes work?" }).click();
      const chip = demo.getByTestId("citation-chip").first();
      await expect(chip).toHaveAttribute("href", /^https:\/\/github\.com\/fastapi\//);
    });

    test("a published repository's URL opens its public page", async ({ page }) => {
      await page.goto("/");
      await page
        .getByLabel("A public GitHub repository's URL")
        .fill("github.com/fastapi/full-stack-fastapi-template.git");
      await page.getByRole("button", { name: "Understand it" }).click();
      await expect(page).toHaveURL("/explore/fastapi/full-stack-fastapi-template");
    });

    test("anything else asks to sign in first, keeping the repository", async ({ page }) => {
      await page.goto("/");
      const input = page.getByLabel("A public GitHub repository's URL");
      await input.fill("https://gitlab.com/o/n");
      await page.getByRole("button", { name: "Understand it" }).click();
      await expect(
        page.getByRole("alert").filter({ hasText: "isn't a GitHub repository's URL" }),
      ).toBeVisible();

      await input.fill("https://github.com/pallets/flask/tree/main");
      await page.getByRole("button", { name: "Understand it" }).click();
      await expect(page).toHaveURL(
        `/login?next=${encodeURIComponent("/new?url=https%3A%2F%2Fgithub.com%2Fpallets%2Fflask")}`,
      );
    });
  });

  test("signed in, the repository lands in the org's URL tab, filled in", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("A public GitHub repository's URL").fill("github.com/pallets/flask");
    await page.getByRole("button", { name: "Understand it" }).click();
    await expect(page).toHaveURL(
      `/acme/repos/new?tab=url&url=${encodeURIComponent("https://github.com/pallets/flask")}`,
    );
    await expect(page.getByRole("textbox", { name: /URL/ })).toHaveValue(
      "https://github.com/pallets/flask",
    );
  });

  test("someone with no organization yet gets the repository back once they make one", async ({
    page,
  }) => {
    const flask = "https://github.com/pallets/flask";
    await page.goto(`/onboarding?url=${encodeURIComponent(flask)}`);
    await page.getByLabel("Organization name").fill("Delta");
    await expect(page.getByText("Available.")).toBeVisible();
    await page.getByRole("button", { name: "Create organization" }).click();
    await expect(page).toHaveURL(
      `/onboarding?step=3&org=delta&tab=url&url=${encodeURIComponent(flask)}`,
    );
    await expect(page.getByRole("textbox", { name: /URL/ })).toHaveValue(flask);
  });
});
