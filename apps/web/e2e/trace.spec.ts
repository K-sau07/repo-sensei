import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page } from "@playwright/test";

import { expect, test } from "./fixtures";

const LOGIN = "tsx frontend/src/routes/login.tsx/Login().";
const EXPORT = "tsx frontend/src/components/Items/ItemsReport.tsx/ExportItems().";
const trace = (entry: string, search = "") =>
  `/acme/api/trace?from=${encodeURIComponent(entry)}${search}`;

/** Pick a mock trace situation (see src/mocks/handlers/trace.ts) before the page loads. */
async function useTraceScenario(context: BrowserContext, scenario: string) {
  await context.addInitScript((value) => {
    localStorage.setItem("sensei.mock.trace", value);
  }, scenario);
}

/** A move between pages can be a view transition: the page takes clicks once it has finished. */
async function settled(page: Page) {
  await page.waitForFunction(
    () =>
      document
        .getAnimations()
        .every(
          (animation) =>
            !(animation.effect as KeyframeEffect | null)?.pseudoElement?.startsWith(
              "::view-transition",
            ),
        ),
    undefined,
    { timeout: 5_000 },
  );
}

const hops = (page: Page) =>
  page.getByTestId("trace-view").getByRole("list", { name: "Hops, in order" });

test.describe("trace", () => {
  test("an entry mid-flow shows where it is reached from and where it goes", async ({ page }) => {
    await page.goto(trace("POST /api/v1/items/"));
    const view = page.getByTestId("trace-view");
    // From the page that starts it, through the entry (the endpoint), on to the table.
    await expect(view.getByText("Starts at Items", { exact: true })).toBeVisible();
    await expect(view.getByText("Reaches the item table", { exact: true })).toBeVisible();
    await expect(hops(page).getByRole("listitem")).toHaveCount(7);
    await expect(
      hops(page).getByRole("button", {
        name: /^Step 1 of 7, Component, component Items, starts here/,
      }),
    ).toBeVisible();
    await expect(
      hops(page).getByRole("button", { name: /^Step 5 of 7, Endpoint, .*the entry traced/ }),
    ).toBeVisible();
    // The header points at the entry's own place, and its hop opens on it.
    await expect(view.getByText(/backend\/app\/api\/routes\/items\.py:\d+/).first()).toBeVisible();
  });

  test("a deep link shows the Login page's path as swimlanes, with the beam", async ({ page }) => {
    await page.goto(trace(LOGIN));
    const view = page.getByTestId("trace-view");
    await expect(view.getByRole("heading", { name: /Trace from\s*Login/ })).toBeVisible();
    await expect(hops(page).getByRole("listitem")).toHaveCount(9);
    await expect(view.getByText("Reaches the user table", { exact: true })).toBeVisible();
    // The lanes, in the order a request travels them, the path's hops in theirs.
    await expect(hops(page).getByRole("button", { name: /^Step 1 of 9, Component/ })).toBeVisible();
    await expect(
      hops(page).getByRole("button", { name: /^Step 9 of 9, Data, table user/ }),
    ).toBeAttached();
    // The path resolved: the beam's pulse ran (or is running) once.
    await expect(view.getByTestId("trace-beam")).toHaveAttribute("data-pulse", /playing|done/);
    // Pips say how sure on hover.
    await hops(page)
      .getByRole("button", { name: /^Verified, 90% sure/ })
      .first()
      .hover();
    await expect(page.locator("[data-slot=tooltip-content]")).toContainText("Verified, 90% sure");
    await page.mouse.move(0, 0);
    // Later phases' actions are shown, disabled, with when.
    await expect(view.getByRole("button", { name: /Open as diagram/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await expect(view.getByRole("button", { name: /Impact/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );

    const { violations } = await new AxeBuilder({ page })
      .include('[data-testid="trace-view"]')
      .analyze();
    expect(violations).toEqual([]);
  });

  test("path tabs switch the path shown, kept in the URL", async ({ page }) => {
    await page.goto(trace(LOGIN));
    const tabs = page.getByRole("tablist", { name: "Paths, best first" });
    await expect(tabs.getByRole("tab")).toHaveCount(3);
    await tabs.getByRole("tab", { name: /^Path 3/ }).click();
    await expect(page).toHaveURL(/[?&]path=3/);
    await expect(tabs.getByRole("tab", { name: /^Path 3/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(hops(page).getByRole("button", { name: /registerUser/ })).toBeVisible();
    await expect(hops(page).getByRole("listitem")).toHaveCount(8);
  });

  test("a hop opens its code below, at the line that proves it", async ({ page }) => {
    await page.goto(trace(LOGIN));
    await hops(page)
      .getByRole("button", { name: /^Step 7 of 9, Data, function authenticate/ })
      .click();
    await expect(page).toHaveURL(/[?&]hop=6/);
    const code = page.getByRole("region", { name: "Code of authenticate" });
    await expect(code).toBeVisible();
    await expect(code.getByText("backend/app/api/routes/login.py:30")).toBeVisible();
    await expect(code.locator(".cm-range-line")).toHaveCount(1);
    await expect(code.locator(".cm-range-line")).toContainText("crud.authenticate(");
    await expect(code.getByRole("link", { name: /Open in code/ })).toHaveAttribute(
      "href",
      "/acme/api/code/backend/app/api/routes/login.py?L=30",
    );
    await code.getByRole("button", { name: "Close the code" }).click();
    await expect(code).toBeHidden();
  });

  test("a gap says why the path stops, and Help me resolve copies a question", async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.goto(trace(EXPORT));
    const gap = page.getByRole("group", { name: "Gap in the trace" });
    await expect(gap).toContainText("client call has no matching endpoint");
    await expect(gap).toContainText(
      "no POST endpoint at /api/v1/items/export (it has DELETE, GET, PUT)",
    );
    await expect(page.getByTestId("trace-beam")).toHaveAttribute("data-pulse", "off");
    await gap.getByRole("button", { name: /Help me resolve/ }).click();
    await expect(page.getByText("Copied a question about this gap")).toBeVisible();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    expect(copied).toContain("stops after POST /api/v1/items/export");
    expect(copied).toContain("client call has no matching endpoint");
  });

  test("the list view says the same as an ordered list", async ({ page }) => {
    await page.goto(trace(LOGIN));
    await expect(page.getByTestId("trace-swimlane")).toBeVisible();
    await page.getByRole("group", { name: "View" }).getByRole("button", { name: /List/ }).click();
    await expect(page).toHaveURL(/[?&]view=list/);
    const list = page.getByTestId("trace-list");
    await expect(list).toBeVisible();
    await expect(page.getByTestId("trace-swimlane")).toHaveCount(0);
    await expect(list.getByRole("listitem")).toHaveCount(9);
    await expect(list.getByRole("listitem").nth(1)).toContainText("useAuth");
    await list.getByRole("button", { name: /^Step 2 of 9/ }).click();
    await expect(page.getByRole("region", { name: "Code of useAuth" })).toBeVisible();
  });

  test("without an entry, pick one by name", async ({ page }) => {
    await page.goto("/acme/api/trace");
    await page.getByRole("textbox", { name: "Where the trace starts" }).fill("Login");
    await page
      .getByRole("list", { name: "Symbols to trace from" })
      .getByRole("link", { name: /^Login\s?component/ })
      .click();
    await expect(page).toHaveURL(/\/acme\/api\/trace\?from=/);
    await expect(hops(page).getByRole("listitem")).toHaveCount(9);
  });

  test("t on a component in the code view lands on its trace", async ({ page }) => {
    await page.goto("/acme/api/code/frontend/src/routes/login.tsx?L=54");
    await settled(page);
    const viewer = page.getByTestId("code-viewer");
    await expect(viewer.locator(".cm-line", { hasText: "useAuth()" }).first()).toBeVisible();
    // At the start of the line: no name under the cursor, so `t` traces the definition around it.
    await viewer
      .locator(".cm-line", { hasText: "const { loginMutation } = useAuth()" })
      .click({ position: { x: 1, y: 4 } });
    await page.mouse.move(0, 0);
    await page.keyboard.press("t");
    await expect(page).toHaveURL(/\/acme\/api\/trace\?from=/);
    await expect(
      hops(page).getByRole("button", { name: /^Step 1 of 9, Component, component Login/ }),
    ).toBeVisible();
  });

  test("the constellation draws the trace's path", async ({ page }) => {
    await page.goto(trace(LOGIN));
    await page.getByRole("link", { name: "Show on the constellation" }).click();
    await expect(page).toHaveURL(/\/acme\/api\?trace=/);
    const caption = page.getByRole("region", { name: "Trace on the constellation" });
    await expect(caption).toContainText("across 6 files");
    await expect(caption.getByRole("listitem")).toHaveText([
      "login.tsx",
      "→useAuth.ts",
      "→sdk.gen.ts",
      "→login.py",
      "→crud.py",
      "→models.py",
    ]);
    await caption.getByRole("button", { name: "Clear" }).click();
    await expect(caption).toBeHidden();
    await expect(page).toHaveURL(/\/acme\/api$/);
  });

  test("before the links stage, the trace says it stays in the code", async ({ page, context }) => {
    await useTraceScenario(context, "unlinked");
    await page.goto(trace(LOGIN));
    await expect(page.getByText("This trace stays in the code.")).toBeVisible();
    await expect(page.getByRole("group", { name: "Gap in the trace" })).toContainText(
      "calls nothing the trace follows further",
    );
  });

  test("too many traces: when to try again", async ({ page, context }) => {
    await useTraceScenario(context, "rate-limited");
    await page.goto(trace(LOGIN));
    const alert = page.getByRole("alert").filter({ hasText: "Too many traces for now" });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText(/You can trace again in \d+ seconds/);
    await expect(alert.getByRole("button", { name: "Try again" })).toBeDisabled();
  });
});
