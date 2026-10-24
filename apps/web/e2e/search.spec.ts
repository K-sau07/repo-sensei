import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Locator, Page } from "@playwright/test";

import { budget, expect, test } from "./fixtures";

/** A search situation (see src/mocks/handlers/search.ts) before the page loads. */
async function useSearchScenario(context: BrowserContext, scenario: string) {
  await context.addInitScript((value) => {
    localStorage.setItem("sensei.mock.search", value);
  }, scenario);
}

const field = (page: Page) => page.getByRole("searchbox", { name: "Search the code" });
const lane = (page: Page, name: string) =>
  page.getByRole("region", { name: new RegExp(`^${name}, `) });
const focusedCard = (page: Page) => page.locator("[data-card]:focus");

async function expectNoAxeViolations(page: Page) {
  const results = await new AxeBuilder({ page }).analyze();
  expect(
    results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`),
  ).toEqual([]);
}

/** An element whose animations (a popover's fade-in) have all finished. */
async function settled(locator: Locator) {
  await expect(locator).toBeVisible();
  await locator.evaluate((element) =>
    Promise.all(element.getAnimations({ subtree: true }).map((animation) => animation.finished)),
  );
}

async function openSearch(page: Page, query = "") {
  await page.goto(`/acme/api/search${query ? `?q=${encodeURIComponent(query)}` : ""}`);
  await expect(field(page)).toBeVisible();
}

test.describe("search", () => {
  test("type, the lanes fill, ↵ opens the code at the exact lines", async ({ page }) => {
    // Opening morphs the card into the viewer with a View Transition: count them.
    await page.addInitScript(() => {
      const start = document.startViewTransition.bind(document);
      Reflect.set(window, "__transitions", 0);
      document.startViewTransition = ((...args: Parameters<typeof start>) => {
        Reflect.set(window, "__transitions", (Reflect.get(window, "__transitions") as number) + 1);
        return start(...args);
      }) as typeof start;
    });
    await openSearch(page);
    await expect(page.getByRole("region", { name: "Ways to search" })).toBeVisible();
    await page.keyboard.press("Slash");
    await expect(field(page)).toBeFocused();
    await page.keyboard.type("create_user");
    await expect(page).toHaveURL(/\/acme\/api\/search\?q=create_user$/);
    await expect(page.getByRole("button", { name: /^Read as symbol/ })).toBeVisible();

    const api = lane(page, "API");
    await expect(api.getByRole("heading")).toContainText("API");
    const first = api.locator("[data-card]").first();
    await expect(first).toHaveAccessibleName(
      "create_user function backend/app/api/routes/users.py:54–78",
    );
    await expect(page.getByRole("status").filter({ hasText: /results in \d+ lanes/ })).toHaveText(
      /^\d+ results in \d+ lanes$/,
    );
    // ↓ from the field moves into the results; ↵ opens the focused one.
    await page.keyboard.press("ArrowDown");
    await expect(focusedCard(page)).toHaveCount(1);
    await page.keyboard.press("ArrowRight");
    await expect(focusedCard(page)).toHaveAccessibleName(/^create_user function backend\/app\/api/);
    // Focus previews it too (and reads the file, so the viewer has it at once).
    await expect(page.getByLabel("Preview of backend/app/api/routes/users.py")).toContainText(
      "def create_user(",
    );
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(
      /\/acme\/api\/code\/backend\/app\/api\/routes\/users\.py\?L=54-78$/,
    );
    expect(await page.evaluate(() => Reflect.get(window, "__transitions") as number)).toBe(1);
    const viewer = page.getByTestId("code-viewer");
    await expect(viewer.locator(".cm-range-line")).toHaveCount(25);
    await expect(
      viewer.locator(".cm-range-line").filter({ hasText: "def create_user(" }),
    ).toHaveCount(1);

    // Back returns to the results, as they were.
    await page.goBack();
    await expect(page).toHaveURL(/\/acme\/api\/search\?q=create_user$/);
    await expect(field(page)).toHaveValue("create_user");
    await expect(lane(page, "API").locator("[data-card]").first()).toBeVisible();
  });

  test("with reduced motion, a result opens with no transition", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript(() => {
      Reflect.set(window, "__transitions", 0);
      const start = document.startViewTransition.bind(document);
      document.startViewTransition = ((...args: Parameters<typeof start>) => {
        Reflect.set(window, "__transitions", 1);
        return start(...args);
      }) as typeof start;
    });
    await openSearch(page, "create_user");
    await lane(page, "DB").locator("[data-card]").first().click();
    await expect(page).toHaveURL(/\/acme\/api\/code\/backend\/app\/crud\.py\?L=10-17$/);
    await expect(page.getByTestId("code-viewer")).toBeVisible();
    expect(await page.evaluate(() => Reflect.get(window, "__transitions") as number)).toBe(0);
  });

  test("j/k move within a lane, ←/→ switch lanes, focus stays visible", async ({ page }) => {
    await openSearch(page, "user email");
    await expect(lane(page, "API").locator("[data-card]").first()).toBeVisible();
    await page.locator("main").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("j");
    const ui = lane(page, "UI").locator("[data-card]");
    await expect(ui.first()).toBeFocused();
    await page.keyboard.press("j");
    await expect(ui.nth(1)).toBeFocused();
    await page.keyboard.press("k");
    await expect(ui.first()).toBeFocused();
    // The focus ring shows (keyboard focus).
    const ring = await ui
      .first()
      .evaluate((el) => getComputedStyle(el.parentElement ?? el).boxShadow);
    expect(ring).not.toBe("none");
    await page.keyboard.press("ArrowRight");
    await expect(lane(page, "API").locator("[data-card]").first()).toBeFocused();
    await page.keyboard.press("j");
    await expect(lane(page, "API").locator("[data-card]").nth(1)).toBeFocused();
    await page.keyboard.press("ArrowRight");
    // The same place in the next lane.
    await expect(lane(page, "Service").locator("[data-card]").nth(1)).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    await expect(ui.nth(1)).toBeFocused();
    // Only the lane's current card is in the tab order.
    await expect(lane(page, "UI").locator('[data-card][tabindex="0"]')).toHaveCount(1);
    // t: tracing isn't built yet.
    await page.keyboard.press("t");
    await expect(page.getByText("Trace is coming soon.")).toBeVisible();
  });

  test("hovering a result previews its lines", async ({ page }) => {
    await openSearch(page, "create_user");
    const card = lane(page, "DB").locator("[data-card]").first();
    await card.hover();
    const preview = page.getByLabel("Preview of backend/app/crud.py");
    await expect(preview).toBeVisible();
    await expect(preview).toContainText("def create_user(");
    await expect(preview).toContainText("Trace: coming soon");
  });

  test("more pages one lane with its cursor", async ({ page }) => {
    await openSearch(page, "user");
    const ui = lane(page, "UI");
    await expect(ui.locator("[data-card]")).toHaveCount(8);
    await ui.getByRole("button", { name: /^More UI results/ }).click();
    await expect(ui.locator("[data-card]")).toHaveCount(16);
  });

  test("pivot to exact: a literal runs as a keyword search, a regex over the files", async ({
    page,
  }) => {
    await openSearch(page, "create_user");
    const pivot = page.getByRole("navigation", { name: "Pivot to exact" });
    await pivot.getByRole("link", { name: "symbol create_user" }).click();
    await expect(page).toHaveURL(/\?q=create_user&mode=keyword$/);
    await expect(page.getByRole("button", { name: /^Read as keyword · exact/ })).toBeVisible();
    await expect(lane(page, "API").locator("[data-card]").first()).toBeVisible();

    await page
      .getByRole("navigation", { name: "Pivot to exact" })
      .getByRole("link", {
        name: "Back to smart search",
      })
      .click();
    await expect(page).toHaveURL(/\?q=create_user$/);
    await page
      .getByRole("navigation", { name: "Pivot to exact" })
      .getByRole("link", { name: /^regex / })
      .click();
    await expect(page).toHaveURL(/mode=regex/);
    const exact = page.getByRole("region", { name: /^Exact matches/ });
    await expect(exact).toBeVisible();
    await expect(exact.locator("[data-card]").first()).toHaveAccessibleName(
      /^backend\/app\/(api\/routes\/(private|users)\.py|crud\.py):\d+$/,
    );
    await exact.locator("[data-card]").first().click();
    await expect(page).toHaveURL(/\/acme\/api\/code\/backend\/app\/.+\?L=\d+$/);
  });

  test("filters round-trip through the URL, and Back undoes them", async ({ page }) => {
    await openSearch(page, "user");
    await page.getByRole("button", { name: "Filters" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("checkbox", { name: /DB/ }).click();
    await expect(page).toHaveURL(/\?q=user&layer=db$/);
    await dialog.getByRole("checkbox", { name: "Python" }).click();
    await expect(page).toHaveURL(/layer=db&lang=python/);
    await dialog.getByLabel("Path starts with").fill("backend/app/");
    await dialog.getByLabel("Path starts with").press("Enter");
    await expect(page).toHaveURL(/path=backend%2Fapp%2F|path=backend\/app\//);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "Filters 3 set" })).toBeVisible();
    await expect(lane(page, "UI")).toContainText("filtered out");
    await expect(lane(page, "DB").locator("[data-card]").first()).toBeVisible();

    // A reload keeps them; Back takes them off one by one.
    await page.reload();
    await expect(page.getByRole("button", { name: "Filters 3 set" })).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(/\?q=user&layer=db&lang=python$/);
    await expect(page.getByRole("button", { name: "Filters 2 set" })).toBeVisible();
  });

  test("partial: keyword results only, and the banner says why", async ({ page, context }) => {
    await useSearchScenario(context, "partial");
    await openSearch(page, "how are passwords hashed");
    await expect(page.getByText("Keyword and symbol matches only.")).toBeVisible();
    await expect(page.getByText(/embeddings aren.t ready yet/)).toBeVisible();
    await expect(lane(page, "Service").locator("[data-card]").first()).toBeVisible();
    await expect(page.locator('[aria-label="Why it matched"]').getByText("semantic")).toHaveCount(
      0,
    );
  });

  test("a rate limit says when to try again", async ({ page, context }) => {
    await useSearchScenario(context, "rate-limited");
    await openSearch(page, "login");
    await expect(page.getByRole("heading", { name: "Too many searches for now" })).toBeVisible();
    await expect(page.getByText(/You can search again in \d+ seconds/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Try again" })).toBeDisabled();
  });

  test("an error offers Retry", async ({ page, context }) => {
    await useSearchScenario(context, "error");
    await openSearch(page, "login");
    const alert = page.getByRole("alert").filter({ hasText: "Couldn't search" });
    await expect(alert).toBeVisible({ timeout: 15_000 });
    await expect(alert.getByRole("button", { name: "Try again" })).toBeVisible();
  });

  test("no results suggests what to try", async ({ page }) => {
    await openSearch(page, "zzqxv_nothing_here");
    await expect(page.getByRole("heading", { name: "No results" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Ask instead" })).toHaveAttribute(
      "href",
      "/acme/api/ask?q=zzqxv_nothing_here",
    );
  });

  test("the Search tab and ⌘K lead here", async ({ page }) => {
    await page.goto("/acme/api");
    await expect(page.locator("html")).toHaveAttribute("data-shell-ready", "true");
    const tab = page.getByRole("navigation", { name: "Repository" }).getByRole("link", {
      name: "Search",
    });
    await expect(tab).not.toContainText("locked");
    await page.keyboard.press("ControlOrMeta+k");
    await page.keyboard.type("password hash");
    await page
      .getByRole("dialog", { name: "Command palette" })
      .getByRole("option", { name: /Search “password hash”/ })
      .click();
    await expect(page).toHaveURL(/\/acme\/api\/search\?q=password\+hash$/);
    await expect(field(page)).toHaveValue("password hash");
  });

  test("axe: results, filters and a preview", async ({ page }) => {
    await openSearch(page, "how are passwords hashed");
    await expect(lane(page, "API").locator("[data-card]").first()).toBeVisible();
    await expectNoAxeViolations(page);
    await page.getByRole("button", { name: /^Read as concept/ }).click();
    await expect(page.getByRole("list", { name: "Rewrites" })).toBeVisible();
    await settled(page.getByRole("dialog", { name: "How the query was read" }));
    await expectNoAxeViolations(page);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Filters" }).click();
    // Contrast is checked once the popover has faded in, not halfway.
    await settled(page.getByRole("dialog", { name: "Search filters" }));
    await expectNoAxeViolations(page);
  });

  test("on a narrow screen one lane shows, picked from a segmented control", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openSearch(page, "create_user");
    const picker = page.getByRole("group", { name: "Lane" });
    await expect(picker).toBeVisible();
    await expect(lane(page, "UI")).toBeVisible();
    await expect(lane(page, "API")).toBeHidden();
    await picker.getByRole("button", { name: /API/ }).click();
    await expect(lane(page, "API")).toBeVisible();
    await expect(lane(page, "UI")).toBeHidden();
    await expectNoAxeViolations(page);
  });

  test(
    "results render within the 500 ms data budget, without layout shift",
    { tag: "@perf" },
    async ({ page }) => {
      // Layout shifts from the first paint on, as CLS counts them (not within 500 ms of input),
      // of the search page's own content (the shell settling around it isn't this page's).
      await page.addInitScript(() => {
        const shifts: number[] = [];
        Reflect.set(window, "__shifts", shifts);
        new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) {
            const shift = entry as unknown as {
              value: number;
              hadRecentInput: boolean;
              sources?: { node?: Node | null }[];
            };
            const own = (shift.sources ?? []).some((source) => {
              const node = source.node;
              const page = node instanceof Element ? node.closest("[data-search-page]") : null;
              return page !== null && page !== node;
            });
            if (!shift.hadRecentInput && own) shifts.push(shift.value);
          }
        }).observe({ type: "layout-shift", buffered: true });
      });
      // Opened with a query: the skeleton lanes, then the results in their place.
      await openSearch(page, "login");
      await expect(lane(page, "UI").locator("[data-card]").first()).toBeVisible();
      const timings: number[] = [];
      for (const q of ["create_user", "how are passwords hashed", "user email", "sidebar"]) {
        await page.evaluate(() => {
          performance.clearMeasures("search:results");
        });
        await field(page).fill(q);
        await field(page).press("Enter");
        const measured = await page.waitForFunction(
          () => performance.getEntriesByName("search:results", "measure").at(-1)?.duration ?? null,
        );
        timings.push((await measured.jsonValue()) ?? Infinity);
      }
      const cls = await page.evaluate(() =>
        (Reflect.get(window, "__shifts") as number[]).reduce((sum, value) => sum + value, 0),
      );
      console.log(
        `search results painted in ${timings.map((t) => t.toFixed(0)).join(", ")} ms; CLS ${cls.toFixed(4)}`,
      );
      expect(Math.max(...timings)).toBeLessThan(budget(500));
      expect(cls).toBeLessThanOrEqual(0.05);
    },
  );
});
