import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

const ITEMS = "/acme/api/code/backend/app/api/routes/items.py";

/** A move between files is a view transition: the page takes clicks once it has finished. */
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

/** The middle of `word` where it first appears in the viewer's line containing `lineText`. */
async function wordAt(page: Page, lineText: string, word: string) {
  const viewer = page.getByTestId("code-viewer");
  await settled(page);
  await expect(viewer.locator(".cm-line", { hasText: lineText }).first()).toBeVisible();
  const point = await viewer.evaluate(
    (root, [needle, name]) => {
      const line = [...root.querySelectorAll(".cm-line")].find((element) =>
        element.textContent.includes(needle),
      );
      if (!line) return null;
      const at = line.textContent.indexOf(needle) + needle.indexOf(name);
      const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
      let seen = 0;
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const length = node.textContent?.length ?? 0;
        if (at < seen + length) {
          const range = document.createRange();
          range.setStart(node, at - seen);
          range.setEnd(node, Math.min(at - seen + name.length, length));
          const rect = range.getBoundingClientRect();
          return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
        }
        seen += length;
      }
      return null;
    },
    [lineText, word] as const,
  );
  if (!point) throw new Error(`${word} isn't on a line with ${lineText}`);
  return point;
}

async function clickWord(page: Page, lineText: string, word: string) {
  const { x, y } = await wordAt(page, lineText, word);
  await page.mouse.click(x, y);
  await page.mouse.move(0, 0); // only the cursor stays: no hover card over what opens next
}

test.describe("code intelligence", () => {
  test("a hover card goes to the definition in another file, and ⌥← comes back", async ({
    page,
  }) => {
    await page.goto(ITEMS);
    const { x, y } = await wordAt(page, "session: SessionDep, current_user", "SessionDep");
    await page.mouse.move(x, y);
    const card = page.getByTestId("symbol-card");
    await expect(card).toBeVisible();
    await expect(card.getByText("SessionDep", { exact: true })).toBeVisible();
    await expect(card.getByText("backend/app/api/deps.py:26")).toBeVisible();
    // Phases to come are shown, disabled, with when.
    await expect(card.getByRole("button", { name: /Impact/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );

    await card.getByRole("button", { name: /Go to definition/ }).click();
    await expect(page).toHaveURL(/\/backend\/app\/api\/deps\.py\?L=26$/);
    await expect(page.getByRole("heading", { level: 2, name: "deps.py" })).toBeVisible();

    await page.getByTestId("code-viewer").locator(".cm-content").focus();
    await page.keyboard.press("Alt+ArrowLeft");
    await expect(page).toHaveURL(/\/backend\/app\/api\/routes\/items\.py\?L=\d+$/);
  });

  test("g d goes to the definition of the name under the cursor, ⌘-click too", async ({ page }) => {
    await page.goto(ITEMS);
    await clickWord(page, "session: SessionDep, current_user: CurrentUser", "CurrentUser");
    await page.keyboard.press("g");
    await page.keyboard.press("d");
    await expect(page).toHaveURL(/\/backend\/app\/api\/deps\.py\?L=49$/);
    await expect(page.getByRole("heading", { level: 2, name: "deps.py" })).toBeVisible();

    // In deps.py: get_current_user's dependency, ⌘/Ctrl-clicked.
    await page.mouse.move(0, 0); // no card left open over the line
    const { x, y } = await wordAt(page, "Depends(get_current_user)", "get_current_user");
    await page.keyboard.down("ControlOrMeta");
    await page.mouse.click(x, y);
    await page.keyboard.up("ControlOrMeta");
    await expect(page).toHaveURL(/\/backend\/app\/api\/deps\.py\?L=30$/);
  });

  test("g r lists the references by layer, and a row opens the place", async ({ page }) => {
    await page.goto("/acme/api/code/backend/app/models.py?L=87");
    await clickWord(page, "class Item(ItemBase, table=True)", "Item");
    await page.keyboard.press("g");
    await page.keyboard.press("r");
    const drawer = page.getByRole("region", { name: "References to Item" });
    await expect(drawer).toBeVisible();
    await expect(drawer.getByRole("region", { name: "API (7)" })).toBeVisible();
    await expect(drawer.getByRole("region", { name: "Data (3)" })).toBeVisible();
    await expect(drawer.getByRole("region", { name: /^Service/ })).toHaveCount(0);

    const { violations } = await new AxeBuilder({ page })
      .include('[data-testid="references-drawer"]')
      .analyze();
    expect(violations).toEqual([]);

    await drawer
      .getByRole("region", { name: "API (7)" })
      .getByRole("button", { name: /^read_items/ })
      .click();
    await expect(page).toHaveURL(/\/backend\/app\/api\/routes\/items\.py\?L=\d+$/);
    // The drawer stays open while the list is walked; Escape closes it.
    await expect(drawer).toBeVisible();
    await drawer.getByRole("button", { name: /^read_items/ }).focus();
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();
  });

  test("the Symbol rail: the outline, callers around the cursor, and t to trace", async ({
    page,
  }) => {
    await page.goto("/acme/api/code/backend/app/api/deps.py");
    await expect(page.getByText("def get_db()")).toBeVisible();
    await page.keyboard.press("]");
    const rail = page.getByRole("complementary", { name: "Symbol" });
    const outline = rail.getByRole("region", { name: "Outline" });
    await expect(outline.getByRole("button", { name: /^get_db/ })).toBeVisible();

    await clickWord(page, "with Session(engine) as session", "session");
    await expect(outline.getByRole("button", { name: /^get_db/ })).toHaveAttribute(
      "aria-current",
      "location",
    );
    const calls = rail.getByRole("region", { name: "Callers and callees" });
    await expect(calls.getByText("get_db", { exact: true })).toBeVisible();
    await expect(
      calls.getByRole("region", { name: "Callers" }).getByRole("button"),
    ).not.toHaveCount(0);
    await expect(
      calls
        .getByRole("region", { name: "Callers" })
        .getByRole("button", { name: "read_items", exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId("mini-graph")).toBeVisible();

    // Re-centred on a caller from the list.
    await calls
      .getByRole("region", { name: "Callers" })
      .getByRole("button", { name: "read_items", exact: true })
      .click();
    await expect(calls.getByText("read_items", { exact: true }).first()).toBeVisible();

    await page.getByTestId("code-viewer").locator(".cm-content").focus();
    await clickWord(page, "with Session(engine) as session", "session");
    await page.keyboard.press("t");
    await expect(page).toHaveURL(
      `/acme/api/trace?from=${encodeURIComponent("py backend/app/api/deps.py/get_db().")}`,
    );
    // The trace renders: get_db is a dependency, reached from the page whose request a handler
    // injects it into, and it is where this way ends (it calls no more of the repository's code:
    // an end, not a gap).
    const view = page.getByTestId("trace-view");
    await expect(view.getByRole("heading", { name: /Trace from\s*get_db/ })).toBeVisible();
    await expect(view.getByText(/^Starts at /)).toBeVisible();
    await expect(
      view.getByRole("button", {
        name: /^Step 6 of 6, Handler, function get_db, injected, the entry traced/,
      }),
    ).toBeVisible();
    const end = view.getByRole("group", { name: "Where this path ends" });
    await expect(end).toContainText("get_db calls no more of the repository's code");
    await expect(end.getByRole("button", { name: /Help me resolve/ })).toHaveCount(0);
  });
});
