import { expect, test } from "./fixtures";

/**
 * What a screen reader is given (10.9.1) on the three flows read without seeing them: an answer
 * (announced once written, not token by token), a trace (an ordered list of named hops), and an
 * approval card (a region with its findings as headings, and the decision as named buttons).
 */

test("an answer is announced once it's written, with its citations", async ({ page }) => {
  await page.goto("/acme/api/ask");
  const box = page.getByRole("textbox", { name: "Ask about this repository" });
  await box.fill("How is an item created?");
  await box.press("ControlOrMeta+Enter");
  const announcer = page.getByTestId("announcer");
  await expect(announcer).toHaveAttribute("aria-live", "polite");
  await expect(announcer).toHaveText(/^Answer ready, \d+ citations?$/);
});

test("a trace reads as its paths, each an ordered list of named hops", async ({ page }) => {
  await page.goto("/acme/api/trace?from=frontend%2Fsrc%2Froutes%2Flogin.tsx");
  await expect(
    page.getByRole("status").filter({ hasText: /^Trace ready: \d+ paths?/ }),
  ).toBeVisible();
  await expect(page.getByRole("tablist", { name: "Paths, best first" })).toBeVisible();
  const hops = page.getByRole("list", { name: "Hops, in order" });
  await expect(hops.getByRole("listitem").first()).toBeVisible();
  // Each hop says where it is in the path, its layer, what it is and where it lives.
  await expect(hops.getByRole("button").first()).toHaveAccessibleName(
    /^Step 1 of \d+, Component, component Login, entry, frontend\/src\/routes\/login\.tsx:\d+$/,
  );
});

test("an approval card reads as its findings and a named decision", async ({ page }) => {
  await page.goto("/acme/api/fixes/0199f000-0000-7000-8000-00000000f001");
  const card = page.getByRole("region", { name: "Approval card" });
  await expect(card.getByRole("heading", { level: 2 })).toBeVisible();
  await expect(card.getByRole("region", { name: "Root cause" })).toBeVisible();
  await expect(card.getByRole("heading", { name: "Root cause", level: 3 })).toBeVisible();
  const approve = card.getByRole("button", { name: "Approve plan", exact: true });
  await expect(approve).toHaveAttribute("aria-keyshortcuts", "Meta+Enter Control+Enter");
  await expect(page.getByRole("list", { name: "Stages" })).toContainText("Approve (current)");
});
