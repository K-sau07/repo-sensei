import { describe, expect, it } from "vitest";

import fixture from "./fixtures/impact.json";
import { mockGraph } from "./graph";
import { CATEGORIES, changedNodes, impactOf } from "./impact";

describe("the mock impact", () => {
  it.each(Object.keys(fixture.cases))("is the engine's for %s", async (key) => {
    const graph = await mockGraph();
    const result = impactOf(
      graph,
      new Set(fixture.screens),
      changedNodes(graph, { symbols: [key] }),
    );
    const expected = fixture.cases[key as keyof typeof fixture.cases];
    for (const category of CATEGORIES) {
      expect(
        result.affected[category].map((a) => ({
          key: a.node.symbol_key,
          score: Math.round(a.score * 10_000) / 10_000,
          distance: a.distance,
          via: a.via,
          edge: a.edge,
        })),
        category,
      ).toEqual(expected[category]);
    }
  });
});
