import { describe, expect, it } from "vitest";

import { mockModules, mockOverview, moduleOf } from "@/mocks/overview";

import {
  dominantLayer,
  healthTiles,
  layoutModules,
  linkWidth,
  moduleLabel,
  share,
} from "./overview";

describe("the architecture map", () => {
  it("puts a module in the column of the layer most of its files are in", () => {
    expect(dominantLayer({ ui: 3, api: 5 })).toBe("api");
    expect(dominantLayer({ ui: 2, db: 2 })).toBe("ui"); // a tie goes left
    expect(dominantLayer({})).toBe("other");
  });

  it("lays columns out left to right, the largest module first in each", () => {
    const placed = layoutModules([
      { path: "db", files: 2, layers: { db: 2 } },
      { path: "web/small", files: 1, layers: { ui: 1 } },
      { path: "web/big", files: 9, layers: { ui: 9 } },
    ]);
    const at = new Map(placed.map((p) => [p.module.path, p]));
    expect(at.get("web/big")?.x).toBe(0);
    expect(at.get("db")?.x).toBeGreaterThan(0);
    expect(at.get("web/big")?.y).toBeLessThan(at.get("web/small")?.y ?? 0);
    expect(at.get("db")?.y).toBe(0); // alone in its column: centred
  });

  it("names a module by its last two directories", () => {
    expect(moduleLabel("backend/app/api")).toBe("app/api");
    expect(moduleLabel(".")).toBe("(top level)");
  });

  it("draws links wider for more edges, within 1 to 4 px", () => {
    expect(linkWidth(1, 40)).toBe(1);
    expect(linkWidth(40, 40)).toBe(4);
    expect(linkWidth(3, 1)).toBe(1.5);
  });
});

describe("the health tiles", () => {
  it("are shares, and say so when there is nothing to measure", () => {
    expect(share(1, 3)).toBe(33);
    expect(share(null, 3)).toBeNull();
    expect(share(0, 0)).toBeNull();
    const tiles = healthTiles(mockOverview().health);
    expect(tiles.map((t) => [t.label, t.value])).toEqual([
      ["Files with symbols", "79%"],
      ["Precise edges", "77%"],
      ["Calls linked", "88%"],
      ["Behind the branch", "–"],
    ]);
    expect(tiles.at(-1)?.detail).toBe("Not tracked yet");
  });
});

describe("the mock overview", () => {
  it("reads the mock tree's modules the way the API does", () => {
    const { modules, rest } = mockModules();
    expect(modules.map((m) => m.path).slice(0, 3)).toEqual([
      "backend/app",
      "backend/app/api/routes",
      "frontend/src/components",
    ]);
    expect(modules.reduce((n, m) => n + m.files, 0) + rest).toBe(245);
    const paths = new Set(modules.map((m) => m.path));
    for (const link of mockOverview().architecture.links) {
      expect(paths.has(link.source) && paths.has(link.target)).toBe(true);
    }
  });

  // The API's own cases (test_overview_api.py), so the two split alike.
  it("splits where the code is, a layer weighing as much as another", () => {
    const java = "server/src/main/java/org/shop";
    const files = [
      ...range(8).map((n) => ({ path: `web/src/pages/p${String(n)}.tsx`, role: "ui" })),
      ...range(8).map((n) => ({ path: `web/src/components/c${String(n)}.tsx`, role: "ui" })),
      ...(
        [
          ["controller", "api"],
          ["service", "service"],
          ["repository", "db"],
        ] as const
      ).flatMap(([part, role]) =>
        range(3).map((n) => ({ path: `${java}/${part}/${part}${String(n)}.java`, role })),
      ),
      ...range(40).map((n) => ({ path: `docs/page${String(n)}.html`, role: "docs" })),
      { path: "README.md", role: "docs" },
    ];
    const of = moduleOf(files);
    expect(of.get("web/src/pages/p0.tsx")).toBe("web/src/pages");
    expect(of.get(`${java}/controller/controller0.java`)).toBe(`${java}/controller`);
    expect(of.get("docs/page0.html")).toBe("docs");
    expect(of.get("README.md")).toBe(".");
  });

  it("doesn't split into more modules than the map holds", () => {
    const files = range(30).map((n) => ({ path: `pkg/m${String(n)}/x.py`, role: "service" }));
    expect(new Set(moduleOf(files).values())).toEqual(new Set(["pkg"]));
  });
});

function range(n: number): number[] {
  return Array.from({ length: n }, (_, i) => i);
}
