import { describe, expect, it } from "vitest";

import {
  colorBuffer,
  indexByPath,
  nearestStar,
  positionBuffer,
  starsOf,
  viewOf,
  type GalaxyStar,
} from "./galaxy";

function star(path: string, x: number, layer = "api"): GalaxyStar {
  return { path, x, y: -x, z: 0.5, cluster: 0, layer, language: "python" };
}

const STARS = [star("a.py", 0.1), star("b.py", 0.2, "ui"), star("c.py", 0.3)];

describe("the galaxy", () => {
  it("finds files' stars, in their order, each once", () => {
    expect(indexByPath(STARS).get("b.py")).toBe(1);
    expect(starsOf(STARS, ["c.py", "x.py", "a.py", "c.py"])).toEqual([2, 0]);
  });

  it("makes the GPU's buffers: positions, and colors with the unlit dimmed", () => {
    expect(Array.from(positionBuffer(STARS).slice(3, 6))).toEqual([
      Math.fround(0.2),
      Math.fround(-0.2),
      0.5,
    ]);
    const colorOf = (layer: string) =>
      layer === "ui" ? ([0, 0, 1] as const) : ([1, 0, 0] as const);
    expect(Array.from(colorBuffer(STARS, colorOf, null))).toEqual([1, 0, 0, 0, 0, 1, 1, 0, 0]);
    const lit = Array.from(colorBuffer(STARS, colorOf, new Set([1]), 0.5));
    expect(lit).toEqual([0.5, 0, 0, 0, 0, 1, 0.5, 0, 0]);
  });

  it("picks the star nearest a click, within reach, and none behind the camera", () => {
    const at = [[10, 10], null, [30, 10]] as const;
    expect(nearestStar(3, (i) => at[i] ?? null, 28, 12)).toBe(2);
    expect(nearestStar(3, (i) => at[i] ?? null, 100, 100)).toBeNull();
  });

  it("flies to a cluster from outside it, looking at it", () => {
    const view = viewOf({ x: 0.5, y: 0, z: 0 }, 1);
    expect(view.target).toEqual([0.5, 0, 0]);
    expect(view.position).toEqual([1.5, 0, 0]);
    expect(viewOf({ x: 0, y: 0, z: 0 }, 2).position).toEqual([0, 0, 2]);
  });
});
