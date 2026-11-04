/**
 * The landing page's sky (10.3): fastapi/full-stack-fastapi-template at b1125d8 as the engine laid
 * it out (the constellation fixture: every file a star where its directory puts it, by layer),
 * and the request half of its sign-in trace (the trace fixture) as the path the Trace Beam runs:
 * the Login page down to the user table. Read on the server; only these numbers are sent.
 */
import type { Layer } from "@/lib/constellation";
import constellation from "@/mocks/fixtures/constellation.json";

import { demoTracePath } from "./product-demo";

export const SKY_LAYERS: readonly Layer[] = ["ui", "api", "svc", "db", "other"];

export interface BeamHop {
  /** The star it is (an index into the points). */
  star: number;
  name: string;
  layer: string;
  path: string;
  line: number | null;
}

export interface HeroSky {
  /** x, y, z of each star (inside the unit sphere). */
  points: number[];
  /** Each star's layer, an index into SKY_LAYERS. */
  layers: number[];
  /** The directory tree's links, as segments: x1, y1, z1, x2, y2, z2. */
  links: number[];
  /** The Trace Beam's stops, a file each (its first hop), from the page to the data. */
  beam: BeamHop[];
  files: number;
}

interface Fixture {
  dirs: { id: number; parent: number | null; x: number; y: number; z: number }[];
  points: { path: string; x: number; y: number; z: number; layer: Layer; dir_cluster: number }[];
}

const r4 = (value: number) => Math.round(value * 1e4) / 1e4;

export function heroSky(): HeroSky {
  const sky = constellation as unknown as Fixture;
  const dirs = new Map(sky.dirs.map((dir) => [dir.id, dir]));
  const points = sky.points.flatMap((p) => [r4(p.x), r4(p.y), r4(p.z)]);
  const layers = sky.points.map((p) => Math.max(0, SKY_LAYERS.indexOf(p.layer)));
  const links: number[] = [];
  for (const p of sky.points) {
    const dir = dirs.get(p.dir_cluster);
    if (dir) links.push(r4(p.x), r4(p.y), r4(p.z), dir.x, dir.y, dir.z);
  }
  for (const dir of sky.dirs) {
    const parent = dir.parent === null ? undefined : dirs.get(dir.parent);
    if (parent) links.push(dir.x, dir.y, dir.z, parent.x, parent.y, parent.z);
  }

  const star = new Map(sky.points.map((p, i) => [p.path, i]));
  const path = demoTracePath();
  const firstData = path.hops.findIndex((hop) => hop.layer === "db");
  const beam: BeamHop[] = [];
  for (const [i, hop] of path.hops.entries()) {
    if (firstData >= 0 && i > firstData && hop.layer !== "db") break; // the request half
    const file = hop.node.path;
    const at = file ? star.get(file) : undefined;
    if (!file || at === undefined || beam.some((stop) => stop.star === at)) continue;
    beam.push({
      star: at,
      name: hop.node.name,
      layer: hop.layer,
      path: file,
      line: hop.node.lines?.[0] ?? null,
    });
  }
  return { points, layers, links, beam, files: sky.points.length };
}
