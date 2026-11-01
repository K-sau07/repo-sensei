/**
 * The Codebase Galaxy (6.9): the endpoint's types, and what the map works out from an answer:
 * the stars' positions and colors as GPU buffers, a path's stars, the stars a search lit, and
 * the star nearest a point on the screen.
 */
import type { Schemas } from "./types";

export type Galaxy = Schemas["GalaxyOut"];
export type GalaxyStar = Schemas["GalaxyStar"];
export type GalaxyCluster = Schemas["GalaxyCluster"];
export type GalaxyLayout = Galaxy["layout"];

export const GALAXY_LAYOUTS: readonly GalaxyLayout[] = ["semantic", "structural"];

export const LAYOUT_LABELS: Record<GalaxyLayout, string> = {
  semantic: "By meaning",
  structural: "By folder",
};

/** Each star's index by its file's path. */
export function indexByPath(stars: readonly GalaxyStar[]): Map<string, number> {
  return new Map(stars.map((star, i) => [star.path, i]));
}

/** The stars of some files, in their order (each once; a file with no star left out). */
export function starsOf(stars: readonly GalaxyStar[], paths: readonly string[]): number[] {
  const at = indexByPath(stars);
  const seen = new Set<number>();
  const out: number[] = [];
  for (const path of paths) {
    const i = at.get(path);
    if (i !== undefined && !seen.has(i)) {
      seen.add(i);
      out.push(i);
    }
  }
  return out;
}

/** The stars as one buffer of positions (x, y, z per star). */
export function positionBuffer(stars: readonly GalaxyStar[]): Float32Array {
  const out = new Float32Array(stars.length * 3);
  stars.forEach((star, i) => {
    out[i * 3] = star.x;
    out[i * 3 + 1] = star.y;
    out[i * 3 + 2] = star.z;
  });
  return out;
}

export type Rgb = readonly [number, number, number];

/** The stars' colors (r, g, b per star, 0..1): each its layer's; with some lit, the rest dimmed
 * to `dim` of it. */
export function colorBuffer(
  stars: readonly GalaxyStar[],
  colorOf: (layer: string) => Rgb,
  lit: ReadonlySet<number> | null,
  dim = 0.18,
): Float32Array {
  const out = new Float32Array(stars.length * 3);
  stars.forEach((star, i) => {
    const [r, g, b] = colorOf(star.layer);
    const k = lit && lit.size > 0 && !lit.has(i) ? dim : 1;
    out[i * 3] = r * k;
    out[i * 3 + 1] = g * k;
    out[i * 3 + 2] = b * k;
  });
  return out;
}

/** The star drawn nearest a point (`px` pixels at most), from the stars' screen positions
 * (`project`: a star's x and y on the screen, or null when behind the camera). */
export function nearestStar(
  count: number,
  project: (i: number) => readonly [number, number] | null,
  x: number,
  y: number,
  px = 10,
): number | null {
  let best: number | null = null;
  let closest = px * px;
  for (let i = 0; i < count; i++) {
    const at = project(i);
    if (!at) continue;
    const d = (at[0] - x) ** 2 + (at[1] - y) ** 2;
    if (d <= closest) {
      closest = d;
      best = i;
    }
  }
  return best;
}

/** Where the camera goes to look at a cluster: in front of it, along the line from the center
 * through it, `distance` away. */
export function viewOf(
  cluster: Pick<GalaxyCluster, "x" | "y" | "z">,
  distance = 1.1,
): { position: [number, number, number]; target: [number, number, number] } {
  const length = Math.hypot(cluster.x, cluster.y, cluster.z);
  const [dx, dy, dz] =
    length > 1e-6 ? [cluster.x / length, cluster.y / length, cluster.z / length] : [0, 0, 1];
  return {
    position: [cluster.x + dx * distance, cluster.y + dy * distance, cluster.z + dz * distance],
    target: [cluster.x, cluster.y, cluster.z],
  };
}
