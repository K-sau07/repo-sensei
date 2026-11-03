import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.describe("the public gallery", () => {
  test.use({ signedIn: false });

  test("anyone opens a published repository and asks about it", async ({ page }) => {
    const listed = await page.goto("/explore");
    expect(listed?.status()).toBe(200);
    await expect(page).toHaveTitle("Explore · repo-sensei");
    const card = page.getByRole("link", { name: /full-stack-fastapi-template/ });
    await expect(card).toContainText("Verified at b1125d8");
    await card.click();

    await expect(page).toHaveURL("/explore/fastapi/full-stack-fastapi-template");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(
      "fastapi/full-stack-fastapi-template",
    );
    // Rendered on the server: what a crawler reads is in the page, with its description,
    // its canonical address and what it is (structured data).
    const html = await (
      await page.request.get("/explore/fastapi/full-stack-fastapi-template")
    ).text();
    expect(html).toContain("create_item");
    expect(html).toMatch(
      /<link rel="canonical" href="[^"]*\/explore\/fastapi\/full-stack-fastapi-template"/,
    );
    expect(html).toContain('"@type":"SoftwareSourceCode"');
    expect(html).toContain('property="og:image"');

    await page.getByLabel("Your question").fill("Where are items saved?");
    await page.getByRole("button", { name: "Ask" }).click();
    const thread = page.getByTestId("explore-thread");
    await expect(thread.getByTestId("citation-chip").first()).toHaveAttribute(
      "href",
      /^https:\/\/github\.com\/fastapi\/full-stack-fastapi-template\/blob\//,
    );
    const { violations } = await new AxeBuilder({ page }).analyze();
    expect(violations.map((v) => v.id)).toEqual([]);
  });

  test("a repository that isn't published is not found, and the sitemap lists the ones that are", async ({
    page,
  }) => {
    const missing = await page.goto("/explore/someone/unpublished");
    expect(missing?.status()).toBe(404);
    const sitemap = await (await page.request.get("/sitemap.xml")).text();
    expect(sitemap).toContain("/explore/fastapi/full-stack-fastapi-template</loc>");
    expect(sitemap).toContain("/explore/tiangolo/typer</loc>");
    // The page's social card, where its metadata says it is.
    const html = await (
      await page.request.get("/explore/fastapi/full-stack-fastapi-template")
    ).text();
    const image = /property="og:image" content="([^"]+)"/.exec(html)?.[1];
    expect(image).toBeDefined();
    const { pathname, search } = new URL(image ?? "", "http://localhost");
    const card = await page.request.get(`${pathname}${search}`);
    expect(card.status()).toBe(200);
    expect(card.headers()["content-type"]).toBe("image/png");
    // Never a card in the site's name for a repository that isn't published.
    const spoofed = await page.request.get(
      `${pathname.replace("fastapi/full-stack-fastapi-template", "anyone/anything")}${search}`,
    );
    expect(spoofed.status()).toBe(404);
  });
});
