import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

/**
 * The accessibility sweep (10.9): every page, in the light theme and the dark, checked by axe
 * against WCAG 2.2 AA's rules, then by the keyboard: Tab goes first to the skip link (in the
 * app), and every element it reaches shows where focus is (2.4.7) and isn't hidden under sticky
 * chrome (2.4.11). What a page does is its own spec's; this one is only that nobody is left out.
 */

const WCAG_22_AA = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const TABS = 30; // how far along the page the keyboard pass goes

const PAGES = [
  // The app
  "/acme", "/acme/activity", "/acme/inbox", "/acme/insights", "/acme/repos", "/acme/repos/new",
  "/acme/api", "/acme/api/ask", "/acme/api/code", "/acme/api/code/backend/app/main.py",
  "/acme/api/search", "/acme/api/trace?from=frontend%2Fsrc%2Froutes%2Flogin.tsx",
  "/acme/api/map", "/acme/api/graph", "/acme/api/graph?table=true", "/acme/api/impact",
  "/acme/api/diagrams", "/acme/api/fixes", "/acme/api/fixes/new",
  "/acme/api/fixes/0199f000-0000-7000-8000-00000000f001", "/acme/api/changes",
  "/acme/api/changes/new", "/acme/api/changes/0199f000-0000-7000-8000-00000000c001",
  "/acme/api/runs", "/acme/api/index", "/acme/api/settings", "/acme/api/wiki",
  "/acme/settings/general", "/acme/settings/members", "/acme/settings/integrations",
  "/acme/settings/api-keys", "/acme/settings/mcp", "/acme/settings/models",
  "/acme/settings/usage", "/acme/settings/security", "/acme/settings/danger",
  "/settings/profile", "/settings/sessions", "/settings/notifications", "/settings/appearance",
  "/settings/shortcuts", "/internal", "/internal/admin",
  // Signing in and joining
  "/login", "/device", "/onboarding", "/invite/seed-pending-token", "/invite/not-a-real-token",
  "/oauth/consent?request=mock-request",
  // The public site
  "/", "/product", "/product/understand", "/product/trace", "/product/diagrams", "/product/fix",
  "/product/mcp", "/pricing", "/benchmarks", "/docs", "/docs/getting-started", "/blog",
  "/blog/how-an-answer-is-checked", "/changelog", "/security", "/privacy", "/terms",
  "/explore", "/explore/fastapi/full-stack-fastapi-template", "/s/not-a-real-token",
  "/d/not-a-real-token",
]; // prettier-ignore

/** Pages reached from a list (their ids are the mock's): the list, and the link to follow. (A
 * diagram's page is made in diagrams.spec; its canvas is swept here on /product/diagrams.) */
const DETAILS: [list: string, link: RegExp][] = [
  ["/acme/api/runs", /\/acme\/api\/runs\/[^/?]+$/],
  ["/internal", /\/internal\/evals\/[^/?]+$/],
];

async function settle(page: Page, scheme?: "light" | "dark") {
  await expect(page.locator("main, body").first()).toBeVisible();
  if (scheme) await expect(page.locator("html")).toHaveAttribute("data-theme", scheme);
  await page.waitForLoadState("networkidle");
  // Development's own overlays aren't the page.
  await page.addStyleTag({
    content: "nextjs-portal, .tsqd-parent-container { display: none !important; }",
  });
}

async function expectNoViolations(page: Page) {
  const { violations } = await new AxeBuilder({ page })
    .withTags(WCAG_22_AA)
    .exclude("nextjs-portal")
    .exclude(".tsqd-parent-container")
    .analyze();
  const found = violations.map((v) => ({
    rule: v.id,
    impact: v.impact,
    nodes: v.nodes.slice(0, 3).map((n) => n.target.join(" ")),
  }));
  expect(found, `axe on ${page.url()}`).toEqual([]);
}

/** Where focus went on each Tab that shows no sign of it, or sits under something else. */
async function keyboardProblems(page: Page): Promise<string[]> {
  await page.locator("body").click({ position: { x: 1, y: 1 } });
  const problems: string[] = [];
  for (let i = 0; i < TABS; i++) {
    await page.keyboard.press("Tab");
    const problem = await page.evaluate((step) => {
      const el = document.activeElement as HTMLElement | null;
      if (!el || el === document.body) return null;
      const name = `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""} "${(el.getAttribute("aria-label") ?? el.textContent).trim().slice(0, 40)}"`;
      const box = el.getBoundingClientRect();
      if (box.width === 0 || box.height === 0) return `Tab ${step}: ${name} has no size`;
      // Something that shows focus: an outline, a ring (a box-shadow), or a changed border or
      // background (the focused element's or a parent's, for focus-within styles).
      const marked = (node: Element) => {
        const style = getComputedStyle(node);
        const drawn = (pseudo: string) => {
          const part = getComputedStyle(node, pseudo);
          return part.content !== "none" && part.backgroundColor !== "rgba(0, 0, 0, 0)";
        };
        return (
          (style.outlineStyle !== "none" && parseFloat(style.outlineWidth) > 0) ||
          style.boxShadow !== "none" ||
          drawn("::after") ||
          drawn("::before")
        );
      };
      // A composite widget (a tree, a listbox) shows focus on its active row.
      const active = el.getAttribute("aria-activedescendant");
      const shows =
        marked(el) ||
        (active !== null && active !== "" && marked(document.getElementById(active) ?? el)) ||
        [el.parentElement, el.parentElement?.parentElement].some((up) => up && marked(up));
      if (!shows) return `Tab ${step}: ${name} shows no focus`;
      // Not hidden under sticky chrome (2.4.11): something of it is on top where it is.
      el.scrollIntoView({ block: "nearest", inline: "nearest" });
      const after = el.getBoundingClientRect();
      const points = [
        [after.left + after.width / 2, after.top + after.height / 2],
        [after.left + 2, after.top + 2],
        [after.right - 2, after.bottom - 2],
      ];
      // A tooltip it opened is part of focusing it, not something over it.
      const seen = points.some(([x, y]) => {
        const top = document.elementFromPoint(x ?? 0, y ?? 0);
        if (top?.closest('[data-slot="tooltip-content"], [role="tooltip"]')) return true;
        return top !== null && (el === top || el.contains(top) || top.contains(el));
      });
      return seen ? null : `Tab ${step}: ${name} is covered`;
    }, i + 1);
    if (problem) problems.push(problem);
  }
  return problems;
}

for (const scheme of ["light", "dark"] as const) {
  test.describe(`${scheme} theme`, () => {
    test.use({ colorScheme: scheme, reducedMotion: "reduce" });
    // The app's own choice (next-themes keeps it), which outranks the system's: dark unless set.
    test.beforeEach(async ({ context }) => {
      await context.addInitScript((theme) => {
        localStorage.setItem("theme", theme);
      }, scheme);
    });

    for (const path of PAGES) {
      test(`${path} meets WCAG 2.2 AA`, async ({ page }) => {
        await page.goto(path);
        await settle(page, scheme);
        await expectNoViolations(page);
        if (scheme === "dark") return; // the keyboard pass once is enough
        expect(await keyboardProblems(page)).toEqual([]);
      });
    }

    for (const [list, link] of DETAILS) {
      test(`the first of ${list} meets WCAG 2.2 AA`, async ({ page }) => {
        await page.goto(list);
        await settle(page);
        const first = page.locator("a[href]").filter({ has: page.locator(":scope") });
        const hrefs = await first.evaluateAll((links) =>
          links.map((a) => a.getAttribute("href") ?? ""),
        );
        const href = hrefs.find((h) => link.test(h.split("?")[0] ?? ""));
        expect(href, `a link like ${String(link)} on ${list}`).toBeTruthy();
        await page.goto(href ?? "");
        await settle(page);
        await expectNoViolations(page);
      });
    }
  });
}

test("in the app, Tab goes to the skip link first, and it skips to the page", async ({ page }) => {
  await page.goto("/acme/api");
  await settle(page);
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to content" });
  await expect(skip).toBeFocused();
  await expect(skip).toBeInViewport();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  const inMain = await page.evaluate(
    () => document.querySelector("main")?.contains(document.activeElement) ?? false,
  );
  expect(inMain).toBe(true);
});
