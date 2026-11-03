import { describe, expect, it } from "vitest";

import { byLayer, editsOf, layerFor, planOf, requestText } from "./changes";

const FOUND = planOf({
  plan: {
    summary: "Limit the items API.",
    files: [
      {
        path: "backend/app/api/routes/items.py",
        change: "modify",
        why: "the routes",
        layer: "api",
      },
      { path: "frontend/src/client/core/request.ts", change: "modify", why: "429", layer: "ui" },
      { path: "backend/app/core/config.py", change: "modify", why: "settings", layer: "svc" },
    ],
    tests: [{ path: "backend/app/tests/test_limit.py", checks: "61st is a 429" }],
    revision: 2,
  },
});
if (!FOUND) throw new Error("no plan");
const PLAN = FOUND;

describe("a change's plan", () => {
  it("is read with defaults, and its files laid out page to table", () => {
    expect(PLAN.revision).toBe(2);
    expect(PLAN.patterns).toEqual([]);
    expect(byLayer(PLAN.files).map(([layer, files]) => [layer, files.length])).toEqual([
      ["ui", 1],
      ["api", 1],
      ["svc", 1],
    ]);
    expect(planOf({ plan: null })).toBeNull();
  });

  it("sends only what a person changed", () => {
    expect(
      editsOf(PLAN, { summary: PLAN.summary, files: PLAN.files, tests: PLAN.tests }),
    ).toBeNull();
    const fewer = PLAN.files.slice(0, 2);
    expect(
      editsOf(PLAN, { summary: "Limit it per key.", files: fewer, tests: PLAN.tests }),
    ).toEqual({
      summary: "Limit it per key.",
      files: fewer.map(({ path, change, why }) => ({ path, change, why })),
    });
    const tests = [...PLAN.tests, { path: "backend/app/tests/test_window.py", checks: "resets" }];
    expect(editsOf(PLAN, { summary: PLAN.summary, files: PLAN.files, tests })?.tests).toHaveLength(
      2,
    );
  });

  it("places a file a person adds in its likely layer", () => {
    expect(layerFor("backend/app/tests/test_x.py")).toBe("test");
    expect(layerFor("frontend/src/routes/items.tsx")).toBe("ui");
    expect(layerFor("backend/app/api/limits.py")).toBe("api");
    expect(layerFor("backend/app/models/item.py")).toBe("db");
    expect(layerFor("README.md")).toBe("other");
  });

  it("says the layers it should stay in, in the request's words", () => {
    expect(requestText(" Add a sort. ", [])).toBe("Add a sort.");
    expect(requestText("Add a sort.", ["ui", "api"])).toBe(
      "Add a sort.\n\nScope: keep the change to these layers: UI, API.",
    );
  });
});
