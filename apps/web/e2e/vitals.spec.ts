import { expect, test } from "./fixtures";

/**
 * Web vitals (10.11): what a page measured is sent when it's hidden, to its org's insights, with
 * its route as a pattern: no repository's or file's name goes with it.
 */
test("a hidden page sends its vitals with its route as a pattern", async ({ page }) => {
  await page.goto("/acme/api/code/backend/app/main.py");
  await expect(page.locator("main")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.locator("main").click({ position: { x: 5, y: 5 } }); // an input, for INP and LCP

  const sent = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith("/api/v1/orgs/acme/vitals"),
  );
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const body = (await sent).postDataJSON() as {
    vitals: { name: string; value: number; route: string }[];
  };
  expect(body.vitals.length).toBeGreaterThan(0);
  for (const vital of body.vitals) {
    expect(["LCP", "INP", "CLS"]).toContain(vital.name);
    expect(vital.route).toBe("/[org]/[repo]/code/[...path]");
  }
});

test("a file path with escaped characters is still only a pattern", async ({ page }) => {
  // Next gives the page its params still percent-encoded; none of the path may go out as words.
  await page.goto("/acme/api/code/src/app/%5Borg%5D/main%20file.tsx");
  await expect(page.locator("main")).toBeVisible();
  await page.waitForLoadState("networkidle");
  await page.locator("main").click({ position: { x: 5, y: 5 } });
  const sent = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith("/api/v1/orgs/acme/vitals"),
  );
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const body = (await sent).postDataJSON() as { vitals: { route: string }[] };
  for (const vital of body.vitals) expect(vital.route).toBe("/[org]/[repo]/code/[...path]");
});
