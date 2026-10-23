import AxeBuilder from "@axe-core/playwright";
import type { BrowserContext, Page } from "@playwright/test";

import { expect, test } from "./fixtures";
import tree from "../src/mocks/fixtures/browse-tree.json" with { type: "json" };

// historyKey(OCTO) in lib/palette/history.ts: the mock's signed-in user.
const HISTORY_KEY = "sensei.palette.history.v1:0199f000-0000-7000-8000-000000000001";

async function openShell(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("html")).toHaveAttribute("data-shell-ready", "true");
}

const palette = (page: Page) => page.getByRole("dialog", { name: "Command palette" });
const input = (page: Page) => page.getByRole("combobox", { name: "Command" });
const selected = (page: Page) => palette(page).locator('[role="option"][aria-selected="true"]');

async function openPalette(page: Page) {
  await page.keyboard.press("ControlOrMeta+k");
  await expect(input(page)).toBeFocused();
}

/** Recent places, as the palette stores them, before the page loads. */
async function seedHistory(context: BrowserContext, entries: object[]) {
  await context.addInitScript(
    ([key, value]) => {
      localStorage.setItem(key, value);
    },
    [HISTORY_KEY, JSON.stringify(entries)] as const,
  );
}

async function expectNoAxeViolations(page: Page) {
  const results = await new AxeBuilder({ page }).analyze();
  expect(
    results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(" | ")}`),
  ).toEqual([]);
}

test.describe("command palette", () => {
  test("type, then ↵ opens a file", async ({ page }) => {
    await openShell(page, "/acme/api");
    await openPalette(page);
    await page.keyboard.type("items.py");
    await expect(selected(page)).toContainText("items.py");
    await expect(selected(page)).toContainText("backend/app/api/routes");
    // The combobox points at the selected option, through every move of the selection.
    const pointsAtSelection = async () => {
      await expect(input(page)).toHaveAttribute(
        "aria-activedescendant",
        (await selected(page).getAttribute("id")) ?? "missing",
      );
    };
    await pointsAtSelection();
    await page.keyboard.press("ArrowDown");
    await expect(selected(page)).not.toContainText("backend/app/api/routes");
    await pointsAtSelection();
    await page.keyboard.press("End");
    await expect(selected(page)).toContainText("Ask “items.py”");
    await pointsAtSelection();
    await page.keyboard.press("Home");
    await expect(selected(page)).toContainText("backend/app/api/routes");
    await pointsAtSelection();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/acme\/api\/code\/backend\/app\/api\/routes\/items\.py$/);
    await expect(page.getByRole("heading", { level: 2, name: "items.py" })).toBeVisible();
    await expect(palette(page)).toBeHidden();
  });

  test("↵ on a symbol opens its lines", async ({ page }) => {
    await openShell(page, "/acme/api");
    await openPalette(page);
    await page.keyboard.type("login_access_token");
    await expect(selected(page)).toContainText("login_access_token");
    await expect(selected(page)).toContainText("function · backend/app/api/routes/login.py:23");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(
      /\/acme\/api\/code\/backend\/app\/api\/routes\/login\.py\?L=23-42$/,
    );
    const viewer = page.getByTestId("code-viewer");
    await expect(viewer.locator(".cm-range-line")).toHaveCount(20);
    await expect(viewer.locator(".cm-range-line").nth(1)).toContainText("def login_access_token(");
  });

  test("Tab sends what's typed to Ask", async ({ page }) => {
    await openShell(page, "/acme/api/code");
    await openPalette(page);
    await page.keyboard.type("how does login work");
    await expect(
      palette(page).getByRole("option", { name: /Ask “how does login work”/ }),
    ).toBeVisible();
    await page.keyboard.press("Tab");
    await expect(page).toHaveURL(/\/acme\/api\/ask\?q=how\+does\+login\+work$/);
  });

  test("outside a repository, Ask goes to the repository you were in last", async ({ page }) => {
    await openShell(page, "/acme/api/code");
    await openShell(page, "/acme/inbox");
    await openPalette(page);
    await page.keyboard.type("where are tokens made");
    const ask = palette(page).getByRole("option", { name: /Ask “where are tokens made”/ });
    await expect(ask).toContainText("about api");
    await page.keyboard.press("Tab");
    await expect(page).toHaveURL(/\/acme\/api\/ask\?q=where\+are\+tokens\+made$/);
  });

  test("gives focus back to what had it, and opens with no animation", async ({ page }) => {
    await openShell(page, "/acme/api");
    const trigger = page.getByRole("button", { name: /Search or ask/ });
    await trigger.click();
    await expect(input(page)).toBeFocused();
    expect(await palette(page).evaluate((el) => getComputedStyle(el).animationName)).toBe("none");
    await page.keyboard.type("set");
    await page.keyboard.press("Escape");
    await expect(palette(page)).toBeHidden();
    await expect(trigger).toBeFocused();
  });

  test("context boost: a file in the current repository ranks first", async ({ page, context }) => {
    // A file of the same name in another repository, opened a minute ago, many times.
    await seedHistory(context, [
      {
        id: "code:acme/web:src/routes/items.py",
        kind: "file",
        title: "items.py",
        subtitle: "web · src/routes",
        href: "/acme/web/code/src/routes/items.py",
        org: "acme",
        repo: "web",
        path: "src/routes/items.py",
        at: Date.now() - 60_000,
        count: 12,
      },
    ]);
    await openShell(page, "/acme/api");
    await openPalette(page);
    await page.keyboard.type("items.py");
    const options = palette(page).getByRole("option");
    await expect(options.filter({ hasText: "web · src/routes" })).toBeVisible();
    await expect(options.first()).toContainText("api · backend/app/api/routes");
    await expect(options.filter({ hasText: "web · src/routes" })).toContainText("Recent");
  });

  test("actions show their shortcuts; coming-soon ones are shown, not runnable", async ({
    page,
  }) => {
    await openShell(page, "/acme/api");
    await openPalette(page);
    await page.keyboard.type("toggle side");
    await expect(selected(page)).toContainText("Toggle the sidebar");
    await expect(selected(page).getByRole("img")).toHaveAccessibleName("[");
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type("trace this");
    const trace = palette(page).getByRole("option", { name: /Trace this/ });
    await expect(trace).toHaveAttribute("aria-disabled", "true");
    await expect(trace).toContainText("Tracing is coming soon");
  });

  test("repositories come from the sidebar's cache, with no request of their own", async ({
    page,
  }) => {
    await openShell(page, "/acme");
    const sidebar = page.getByRole("complementary", { name: "Sidebar" });
    await expect(sidebar.getByRole("link", { name: /\bapi\b/ })).toBeVisible();
    const listed: string[] = [];
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/v1/orgs/acme/repos") listed.push(request.url());
    });
    await openPalette(page);
    await page.keyboard.type("api");
    await expect(palette(page).getByRole("option", { name: /^api.*acme-gh\/api/ })).toBeVisible();
    expect(listed).toEqual([]);
  });

  test("↵ never signs out on a synonym or a few letters", async ({ page }) => {
    await openShell(page, "/acme");
    await openPalette(page);
    // "log" is too short to read as "log out".
    await page.keyboard.type("log");
    await expect(selected(page)).toBeVisible();
    await expect(palette(page).getByRole("option", { name: /^Sign out/ })).toHaveCount(0);
    // "logout" does find it, but only a synonym matched: it's offered, not selected.
    await page.keyboard.type("out");
    await expect(palette(page).getByRole("option", { name: /^Sign out/ })).toBeVisible();
    await expect(selected(page)).not.toContainText("Sign out");
    await page.keyboard.press("Enter");
    await expect(page).not.toHaveURL(/\/login|\/$/);
  });

  test("synonyms: “refresh” finds Reindex", async ({ page }) => {
    await openShell(page, "/acme/api");
    await openPalette(page);
    await page.keyboard.type("refresh");
    await expect(palette(page).getByRole("option", { name: /Reindex api/ })).toBeVisible();
  });

  test("has no accessibility violations, with results showing", async ({ page }) => {
    await openShell(page, "/acme/api");
    await openPalette(page);
    await expectNoAxeViolations(page);
    await page.keyboard.type("login_acc");
    await expect(palette(page).getByRole("option", { name: /^login_access_token/ })).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: /\d+ results/ })).toBeAttached();
    await expectNoAxeViolations(page);
  });

  test.describe("on a phone", () => {
    test.use({ viewport: { width: 390, height: 844 } });

    test("the bottom bar's Ask opens it full-width, with a way to close it", async ({ page }) => {
      await openShell(page, "/acme/api");
      await page
        .getByRole("navigation", { name: "Quick" })
        .getByRole("button", { name: "Ask" })
        .click();
      await expect(input(page)).toBeFocused();
      const box = await palette(page).boundingBox();
      expect(box?.width).toBe(390);
      await page.keyboard.type("login_acc");
      await expect(
        palette(page).getByRole("option", { name: /^login_access_token/ }),
      ).toBeVisible();
      await expectNoAxeViolations(page);
      await palette(page).getByRole("button", { name: "Close" }).click();
      await expect(palette(page)).toBeHidden();
    });
  });

  test(
    "opens to its first local results within 50 ms",
    { tag: "@perf" },
    async ({ page, context }) => {
      // 50 recent files, so the expanded list is the 50 rows that decide whether to virtualize.
      const now = Date.now();
      await seedHistory(
        context,
        tree.files.slice(0, 50).map((file, index) => ({
          id: `code:acme/api:${file.path}`,
          kind: "file",
          title: file.path.slice(file.path.lastIndexOf("/") + 1),
          subtitle: `api · ${file.path.slice(0, Math.max(0, file.path.lastIndexOf("/")))}`,
          href: `/acme/api/code/${file.path}`,
          org: "acme",
          repo: "api",
          path: file.path,
          at: now - index * 60_000,
          count: 1,
        })),
      );
      // The code page: the overview's WebGL constellation starts up in the background (a
      // ~550 ms task of its own), which would be measured instead of the palette.
      await openShell(page, "/acme/api/code");
      await expect(page.getByRole("tree", { name: "Repository files" })).toBeVisible();
      // Warm up once (the first opening loads the palette's code in dev mode).
      await openPalette(page);
      await page.keyboard.press("Escape");

      interface Opening {
        results: number;
        painted: number;
      }
      // Armed before the key goes down: from ⌘K's keydown to the first option in the DOM, and
      // to the start of the next frame (the one with the options has painted by then).
      const arm = () =>
        page.evaluate(() => {
          const state = window as unknown as { __opening?: Promise<Opening> };
          state.__opening = new Promise<Opening>((resolve) => {
            const onKey = (event: KeyboardEvent) => {
              if (event.key.toLowerCase() !== "k") return;
              document.removeEventListener("keydown", onKey, true);
              const start = event.timeStamp;
              const shown = () =>
                document.querySelector('[role="dialog"][data-open] [role="option"]');
              const finish = () => {
                const results = performance.now() - start;
                requestAnimationFrame(() => {
                  setTimeout(() => {
                    resolve({ results, painted: performance.now() - start });
                  }, 0);
                });
              };
              if (shown()) {
                finish();
                return;
              }
              const observer = new MutationObserver(() => {
                if (!shown()) return;
                observer.disconnect();
                finish();
              });
              // The popup is marked open (an attribute) after its options are in.
              observer.observe(document.body, {
                childList: true,
                subtree: true,
                attributes: true,
                attributeFilter: ["data-open"],
              });
            };
            document.addEventListener("keydown", onKey, true);
          });
        });
      const opening = () =>
        page.evaluate(() => (window as unknown as { __opening: Promise<Opening> }).__opening);
      const runs: Opening[] = [];
      for (let run = 0; run < 5; run += 1) {
        await arm();
        await page.keyboard.press("ControlOrMeta+k");
        runs.push(await opening());
        await expect(input(page)).toBeFocused();
        await page.keyboard.press("Escape");
        await expect(palette(page)).toBeHidden();
      }
      const painted = runs.map((run) => run.painted).sort((a, b) => a - b);
      const median = painted[Math.floor(painted.length / 2)] ?? Infinity;
      console.log(
        `⌘K open → first local results: in the DOM ${runs.map((r) => r.results.toFixed(1)).join(", ")} ms; painted ${painted.map((p) => p.toFixed(1)).join(", ")} ms (median ${median.toFixed(1)})`,
      );
      expect(median).toBeLessThan(50);

      // 50 rows: show the whole Go group (6 → 50 rows), then type with all of them up.
      await openPalette(page);
      await expect(
        palette(page)
          .getByRole("option", { name: /^Show \d+ more$/ })
          .first(),
      ).toBeVisible();
      const grown = await page.evaluate(
        () =>
          new Promise<number>((resolve) => {
            const more = [
              ...document.querySelectorAll<HTMLElement>('[role="dialog"] [role="option"]'),
            ].find((option) => /^Show \d+ more$/.test(option.textContent));
            const rows = () =>
              document.querySelectorAll('[role="dialog"] [cmdk-group]:first-child [role="option"]')
                .length;
            if (!more) {
              resolve(Infinity);
              return;
            }
            const start = performance.now();
            const observer = new MutationObserver(() => {
              if (rows() < 50) return;
              observer.disconnect();
              requestAnimationFrame(() => {
                setTimeout(() => {
                  resolve(performance.now() - start);
                }, 0);
              });
            });
            observer.observe(document.body, { childList: true, subtree: true });
            more.click();
          }),
      );
      // 50 results (the most a group shows) and the row for the rest.
      await expect(palette(page).locator('[cmdk-group]:first-child [role="option"]')).toHaveCount(
        51,
      );
      await page.evaluate(() => {
        const frames: number[] = [];
        const longTasks: number[] = [];
        const state = window as unknown as { __frames: number[]; __long: number[]; __run: boolean };
        state.__frames = frames;
        state.__long = longTasks;
        state.__run = true;
        new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) longTasks.push(entry.duration);
        }).observe({ type: "longtask" });
        let last = performance.now();
        const tick = (now: number) => {
          frames.push(now - last);
          last = now;
          if (state.__run) requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      });
      await page.keyboard.type("backend", { delay: 60 });
      await page.waitForTimeout(400); // the server results land too
      const typing = await page.evaluate(() => {
        const state = window as unknown as { __frames: number[]; __long: number[]; __run: boolean };
        state.__run = false;
        const sorted = [...state.__frames].sort((a, b) => a - b);
        return {
          frames: sorted.length,
          p95: sorted[Math.floor(sorted.length * 0.95)] ?? 0,
          max: sorted.at(-1) ?? 0,
          over: sorted.filter((frame) => frame > 1000 / 60 + 4).length,
          longTasks: state.__long,
        };
      });
      console.log(
        `50 rows: shown and painted in ${grown.toFixed(1)} ms; typing over them: ${String(typing.frames)} frames, p95 ${typing.p95.toFixed(1)} ms, max ${typing.max.toFixed(1)} ms, ${String(typing.over)} frames over 20.7 ms, long tasks [${typing.longTasks.map((t) => t.toFixed(0)).join(", ")}] ms`,
      );
      expect(grown).toBeLessThan(50);
      // No frame dropped badly while typing: nothing near a long task.
      expect(typing.max).toBeLessThan(50);

      // Server results, in a fresh palette: the query arrives in one input event, so the
      // symbols search is made once, for all of it. The frontend spec's budget for server fuzzy
      // results is 200 ms, which includes the 120 ms the palette waits for typing to pause. That
      // wait is ours by design and doesn't vary, so the test measures the rest (from the
      // request's start to the results painted) against what
      // the budget leaves for it: 200 − 120 = 80 ms. Measuring from the keystroke would leave a
      // slow CI runner the same 80 ms, with the fixed wait hiding how much of it was used.
      await page.keyboard.press("Escape");
      await expect(palette(page)).toBeHidden();
      await openPalette(page);
      await expect(input(page)).toHaveValue("");
      await page.evaluate(() => {
        const state = window as unknown as {
          __typedAt?: number;
          __paintedAt?: number;
          __request?: { start: number; duration: number };
        };
        // The request starts when the API client calls fetch (resource timing can't be relied
        // on here: the mock answers from a service worker, and dev mode fills its buffer).
        const fetch = window.fetch.bind(window);
        window.fetch = async (input, init) => {
          const url = input instanceof Request ? input.url : String(input);
          const start = performance.now();
          const response = await fetch(input, init);
          if (url.includes("/symbols?q=login_access_tok")) {
            state.__request = { start, duration: performance.now() - start };
          }
          return response;
        };
        document.addEventListener(
          "input",
          (event) => {
            state.__typedAt = event.timeStamp;
          },
          { capture: true, once: true },
        );
        const observer = new MutationObserver(() => {
          const rows = document.querySelectorAll('[role="dialog"][data-open] [data-kind="symbol"]');
          if (![...rows].some((row) => row.textContent.includes("login_access_token"))) return;
          observer.disconnect();
          // The next frame has started once the frame with the results has painted.
          requestAnimationFrame(() => {
            setTimeout(() => {
              state.__paintedAt = performance.now();
            }, 0);
          });
        });
        observer.observe(document.body, { childList: true, subtree: true, characterData: true });
      });
      await page.keyboard.insertText("login_access_tok");
      await expect(
        palette(page).locator('[data-kind="symbol"]', { hasText: "login_access_token" }).first(),
      ).toBeVisible();
      await expect
        .poll(() =>
          page.evaluate(() => {
            const state = window as unknown as { __paintedAt?: number; __request?: object };
            return state.__paintedAt !== undefined && state.__request !== undefined;
          }),
        )
        .toBe(true);
      const server = await page.evaluate(() => {
        const state = window as unknown as {
          __typedAt?: number;
          __paintedAt?: number;
          __request?: { start: number; duration: number };
        };
        const painted = state.__paintedAt ?? Infinity;
        return {
          fromRequest: painted - (state.__request?.start ?? -Infinity),
          fromTyping: painted - (state.__typedAt ?? -Infinity),
          request: state.__request?.duration ?? null,
        };
      });
      console.log(
        `server results: request start → painted ${server.fromRequest.toFixed(1)} ms (the request itself ${String(server.request?.toFixed(1))} ms); typed → painted ${server.fromTyping.toFixed(1)} ms, the 120 ms debounce included`,
      );
      expect(server.fromRequest).toBeLessThan(200 - 120);
    },
  );
});
