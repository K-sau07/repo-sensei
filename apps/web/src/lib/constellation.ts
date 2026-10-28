/**
 * The Living Constellation's data on the client: pages from `GET …/constellation` gathered into
 * one picture, what a directory cluster holds, and the geometry both renderers share (the static
 * 2D projection, picking the point under the pointer, how big a star is).
 *
 * Positions come from the API (a directory layout inside the unit sphere: clusters are
 * directories, deeper ones further out). Every point has a cursor (its index); a page carries the
 * points from `since` on, so pages simply append, unless the generation changed (discovery started
 * over), which starts the picture again.
 */
import type { ConstellationDir, ConstellationPage, ConstellationPoint } from "@/lib/api/types";

export type Layer = ConstellationPoint["layer"];
export type SkyStatus = ConstellationPage["status"];

export interface Sky {
  generation: string | null;
  status: SkyStatus;
  commitSha: string | null;
  /** Points there will be, once known. */
  total: number | null;
  /** Points the API has now (more than `points.length` while pages are still coming). */
  available: number;
  dirs: ConstellationDir[];
  points: ConstellationPoint[];
}

export const EMPTY_SKY: Sky = {
  generation: null,
  status: "pending",
  commitSha: null,
  total: null,
  available: 0,
  dirs: [],
  points: [],
};

/** `sky` with `page` added, or null when the page belongs to another generation and doesn't
 * start at the beginning (the caller reads again from 0). */
export function addPage(sky: Sky, page: ConstellationPage): Sky | null {
  const sameGeneration = page.generation === null || page.generation === sky.generation;
  if (!sameGeneration && sky.generation !== null && page.since > 0) return null;
  const base = sameGeneration ? sky : EMPTY_SKY;
  if (page.since !== base.points.length && page.points.length > 0) return null; // out of step
  return {
    generation: page.generation ?? base.generation,
    status: page.status,
    commitSha: page.commit_sha ?? base.commitSha,
    total: page.total ?? base.total,
    available: Math.max(page.available, base.points.length + page.points.length),
    dirs: page.dirs.length > 0 ? page.dirs : base.dirs,
    points: page.points.length > 0 ? [...base.points, ...page.points] : base.points,
  };
}

export interface Cluster {
  dir: ConstellationDir;
  /** Files directly in the directory (all of them, also those not loaded yet). */
  files: number;
  /** The most common language among its loaded files. */
  topLanguage: string | null;
  loaded: number;
}

/** What each directory with files holds, by directory id. */
export function clusters(sky: Sky): Map<number, Cluster> {
  const counts = new Map<number, Map<string, number>>();
  const loaded = new Map<number, number>();
  for (const point of sky.points) {
    loaded.set(point.dir_cluster, (loaded.get(point.dir_cluster) ?? 0) + 1);
    if (!point.language) continue;
    let languages = counts.get(point.dir_cluster);
    if (!languages) {
      languages = new Map<string, number>();
      counts.set(point.dir_cluster, languages);
    }
    languages.set(point.language, (languages.get(point.language) ?? 0) + 1);
  }
  const out = new Map<number, Cluster>();
  for (const dir of sky.dirs) {
    const here = loaded.get(dir.id) ?? 0;
    if (dir.files === 0 && here === 0) continue;
    let top: string | null = null;
    let best = 0;
    for (const [language, count] of counts.get(dir.id) ?? new Map<string, number>()) {
      // Ties go to the name first in order, so the answer never depends on arrival order.
      if (count > best || (count === best && top !== null && language < top)) {
        top = language;
        best = count;
      }
    }
    out.set(dir.id, { dir, files: Math.max(dir.files, here), topLanguage: top, loaded: here });
  }
  return out;
}

export const dirLabel = (dir: ConstellationDir) => (dir.path === "" ? "(root)" : `${dir.path}/`);

/** The code browser's URL section for a path (each segment encoded). */
export const codeSection = (path: string) =>
  `code/${path.split("/").map(encodeURIComponent).join("/")}`;

export function fileName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** A star's radius relative to the smallest: grows slowly with the file's size (1 to 2.5). */
export function starScale(size: number): number {
  return 1 + Math.min(1.5, Math.log10(1 + size) / 4);
}

/** How far from the centre the picture reaches (at least 0.25): views scale it to fill their
 * box. Directory anchors are all known from the first page, so it barely moves as stars come. */
export function reach(sky: Sky): number {
  let far = 0.25;
  for (const { x, y, z } of sky.dirs) far = Math.max(far, Math.hypot(x, y, z));
  for (const { x, y, z } of sky.points) far = Math.max(far, Math.hypot(x, y, z));
  return far;
}

/** The stars of a trace's files (`files` in its order), each once; a file without a star (not
 * in this sky) is left out. */
export function traceStars(sky: Sky, files: readonly string[]): number[] {
  const at = new Map(sky.points.map((point, i) => [point.path, i]));
  const stars: number[] = [];
  for (const file of files) {
    const star = at.get(file);
    if (star !== undefined && !stars.includes(star)) stars.push(star);
  }
  return stars;
}

// ------------------------------------------------------------------ the static view

/** The angle the static (SVG) view looks from, the same as the 3D view's starting camera. */
export const VIEW = { yaw: -0.6, pitch: 0.32 };

/** A point seen from `VIEW` (orthographic): x right, y down, both about -1..1; and its depth
 * (larger is nearer). */
export function project(
  x: number,
  y: number,
  z: number,
  view: { yaw: number; pitch: number } = VIEW,
): [number, number, number] {
  const cy = Math.cos(view.yaw);
  const sy = Math.sin(view.yaw);
  const rx = cy * x + sy * z;
  const rz = -sy * x + cy * z;
  const cp = Math.cos(view.pitch);
  const sp = Math.sin(view.pitch);
  const ry = cp * y - sp * rz;
  const depth = sp * y + cp * rz;
  return [rx, -ry, depth];
}

/** The index of the point nearest (`px`, `py`) within `radius`, among `count` screen positions
 * stored as x, y pairs; -1 if none. */
export function nearest(
  screen: Float32Array,
  count: number,
  px: number,
  py: number,
  radius: number,
): number {
  let found = -1;
  let best = radius * radius;
  for (let i = 0; i < count; i += 1) {
    const dx = (screen[2 * i] ?? Infinity) - px;
    const dy = (screen[2 * i + 1] ?? Infinity) - py;
    const distance = dx * dx + dy * dy;
    if (distance <= best) {
      best = distance;
      found = i;
    }
  }
  return found;
}

// ------------------------------------------------------------------ how it moves

/** Motion for new points and the camera: "live" (fade and scale in, glow, drift) or "static". */
export type MotionMode = "live" | "static";

/** Devices that should get the static picture: few cores, or the user asked to save data. */
export function isLowPower(nav: {
  hardwareConcurrency?: number;
  connection?: { saveData?: boolean };
}): boolean {
  const cores = nav.hardwareConcurrency;
  return (typeof cores === "number" && cores > 0 && cores < 4) || nav.connection?.saveData === true;
}

/** Whether a WebGL renderer name is a software rasterizer (no GPU): there the bloom pass alone
 * blocks the page for about half a second, so the picture is drawn still. */
export function isSoftwareRenderer(name: string | null | undefined): boolean {
  return /swiftshader|llvmpipe|softpipe|software|basic render/i.test(name ?? "");
}

/** When each new point starts to appear, in ms from now: a batch that arrives at once is spread
 * out (up to `spread` ms), so it still forms rather than pops. */
export function stagger(count: number, spread = 1200): number[] {
  const span = Math.min(spread, count * 4);
  return Array.from({ length: count }, (_, i) => (count > 1 ? (i / (count - 1)) * span : 0));
}

// ------------------------------------------------------------------ synthetic skies (stories)

function seeded(seed: number) {
  let state = seed;
  return () => {
    // mulberry32
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const LAYERS: Layer[] = ["ui", "api", "svc", "db", "other"];
const ROLE_OF: Record<Layer, string> = {
  ui: "ui",
  api: "api",
  svc: "service",
  db: "db",
  other: "config",
};

/** A made-up constellation of `count` files in nested directories (for stories and the
 * performance check), the same for the same seed. */
export function syntheticSky(count: number, seed = 1): Sky {
  const random = seeded(seed);
  const dirs: ConstellationDir[] = [
    { id: 0, path: "", parent: null, depth: 0, x: 0, y: 0, z: 0, files: 0 },
  ];
  const leaves: number[] = [];
  const grow = (parent: number, depth: number, theta: number, phi: number, spread: number) => {
    const children = depth === 0 ? 12 : depth < 3 ? 2 + Math.floor(random() * 4) : 0;
    for (let c = 0; c < children; c += 1) {
      const t = theta + (random() - 0.5) * spread;
      const p = Math.max(0.15, Math.min(Math.PI - 0.15, phi + (random() - 0.5) * spread));
      const r = 1 - 0.75 ** (depth + 2);
      const id = dirs.length;
      const parentPath = dirs[parent]?.path ?? "";
      dirs.push({
        id,
        path: `${parentPath ? `${parentPath}/` : ""}d${String(c)}`,
        parent,
        depth: depth + 1,
        x: r * Math.sin(p) * Math.cos(t),
        y: r * Math.cos(p),
        z: r * Math.sin(p) * Math.sin(t),
        files: 0,
      });
      leaves.push(id);
      grow(id, depth + 1, t, p, spread * 0.5);
    }
  };
  grow(0, 0, 0, Math.PI / 2, Math.PI * 2);
  const points: ConstellationPoint[] = [];
  for (let i = 0; i < count; i += 1) {
    const dirId = leaves[Math.floor(random() * leaves.length)] ?? 0;
    const dir = dirs[dirId];
    if (!dir) continue;
    dir.files += 1;
    const layer = LAYERS[Math.floor(random() * LAYERS.length)] ?? "other";
    const jitter = 0.05 / (1 + dir.depth * 0.3);
    points.push({
      file_id: `00000000-0000-4000-8000-${i.toString(16).padStart(12, "0")}`,
      path: `${dir.path}/f${String(i)}.ts`,
      x: dir.x + (random() - 0.5) * jitter * 2,
      y: dir.y + (random() - 0.5) * jitter * 2,
      z: dir.z + (random() - 0.5) * jitter * 2,
      layer,
      role: ROLE_OF[layer],
      language: random() < 0.7 ? "typescript" : "python",
      dir_cluster: dirId,
      size: Math.floor(random() * 40_000),
      loc: Math.floor(random() * 900),
    });
  }
  // Points in discovery order: by path.
  points.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return {
    generation: `synthetic-${String(seed)}`,
    status: "complete",
    commitSha: null,
    total: points.length,
    available: points.length,
    dirs,
    points,
  };
}
