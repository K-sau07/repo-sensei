import { describe, expect, it } from "vitest";

import type { Deps } from "@/lib/api/deps";
import fixture from "@/mocks/fixtures/deps.json";

import { MAX_REFINED, refineLayout } from "./refine-layout";

const modules = { ...fixture.levels.module.deps, sha: "abc" } as Deps;

function closest(deps: Deps): number {
  let found = Infinity;
  for (const [i, a] of deps.nodes.entries()) {
    for (const b of deps.nodes.slice(i + 1))
      found = Math.min(found, Math.hypot(a.x - b.x, a.y - b.y));
  }
  return found;
}

describe("the explorer's layout", () => {
  it("spreads a level's clumped core, within the frame, the same each time", () => {
    const refined = refineLayout(modules);
    expect(closest(refined)).toBeGreaterThan(closest(modules) * 4);
    for (const node of refined.nodes) {
      expect(Math.abs(node.x)).toBeLessThanOrEqual(1 + 1e-9);
      expect(Math.abs(node.y)).toBeLessThanOrEqual(1 + 1e-9);
    }
    expect(refineLayout(modules)).toEqual(refined);
    expect(refined.edges).toBe(modules.edges);
  });

  it("leaves a tiny or a huge level as the server placed it", () => {
    const two = { ...modules, nodes: modules.nodes.slice(0, 2) };
    expect(refineLayout(two)).toBe(two);
    const many = {
      ...modules,
      nodes: Array.from({ length: MAX_REFINED + 1 }, (_, i) => ({
        id: String(i),
        label: String(i),
        layer: "other",
        size: 1,
        cluster: null,
        x: 0,
        y: 0,
      })),
    };
    expect(refineLayout(many)).toBe(many);
  });
});
