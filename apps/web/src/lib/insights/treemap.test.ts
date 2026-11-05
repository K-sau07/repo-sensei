import { describe, expect, it } from "vitest";

import { treemap } from "./treemap";

const area = (t: { width: number; height: number }) => t.width * t.height;

describe("treemap", () => {
  it("gives each item its share of the area, inside the bounds, largest first", () => {
    const items = [6, 6, 4, 3, 2, 2, 1];
    const tiles = treemap(items, (v) => v, 600, 400);
    expect(tiles.map((t) => t.item)).toEqual([6, 6, 4, 3, 2, 2, 1]);
    for (const tile of tiles) {
      expect(area(tile)).toBeCloseTo((tile.item / 24) * 600 * 400, 6);
      expect(tile.x).toBeGreaterThanOrEqual(-1e-9);
      expect(tile.y).toBeGreaterThanOrEqual(-1e-9);
      expect(tile.x + tile.width).toBeLessThanOrEqual(600 + 1e-9);
      expect(tile.y + tile.height).toBeLessThanOrEqual(400 + 1e-9);
    }
    expect(tiles.reduce((sum, t) => sum + area(t), 0)).toBeCloseTo(600 * 400, 6);
  });

  it("keeps tiles close to square", () => {
    const tiles = treemap([6, 6, 4, 3, 2, 2, 1], (v) => v, 600, 400);
    for (const tile of tiles) {
      const ratio = Math.max(tile.width / tile.height, tile.height / tile.width);
      expect(ratio).toBeLessThan(3.5);
    }
  });

  it("skips what has no value, and lays out nothing for nothing", () => {
    expect(treemap([3, 0], (v) => v, 100, 100).map((t) => t.item)).toEqual([3]);
    expect(treemap([], (v: number) => v, 100, 100)).toEqual([]);
    expect(treemap([0], (v) => v, 100, 100)).toEqual([]);
  });
});
