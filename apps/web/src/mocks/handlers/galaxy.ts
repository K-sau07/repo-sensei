/**
 * The mock `GET …/galaxy?layout=semantic|structural`: by folder, the mock constellation's stars
 * grouped by folder (as the API groups them: a big folder split into its parts); by meaning, fixtures/galaxy.json
 * (scripts/mock_galaxy.py: the engine's layout of the files' content).
 *
 * Situations worth seeing, from the browser:
 *   localStorage.setItem("sensei.mock.galaxy", "computing")
 * "computing" (the semantic layout is being made: 409 `galaxy_computing`, retry in 2 s, then
 * there), "none" (no summaries: 409 `semantic_not_ready`) or "big" (100,000 stars).
 */
import { http, HttpResponse } from "msw";

import type { Galaxy, GalaxyCluster, GalaxyStar } from "@/lib/api/galaxy";

import { apiError } from "./errors";
import { graphReady } from "./graph";

interface Params {
  org: string;
  repo: string;
}

interface ConstellationFixture {
  points: {
    path: string;
    x: number;
    y: number;
    z: number;
    layer: string;
    language: string | null;
  }[];
}

export const GALAXY_SCENARIO_KEY = "sensei.mock.galaxy";
export const BIG_STARS = 100_000;
const ROOT = "(root)";
const READY_AFTER_MS = 2_000;
let computingSince: number | null = null;

function scenario(): string | null {
  return typeof localStorage === "undefined" ? null : localStorage.getItem(GALAXY_SCENARIO_KEY);
}

// As the engine's layout by folder (`sensei_engine.galaxy.folder_groups`).
const SPLIT = 0.25;
const SPLIT_FILES = 40;
const SMALL = 0.005;
const MAX_FOLDERS = 16;
const REST = "(the rest)";

function partOf(path: string, folder: string): string {
  if (!folder) return path.includes("/") ? (path.split("/")[0] ?? ROOT) : ROOT;
  const rest = path.slice(folder.length + 1);
  return rest.includes("/") ? `${folder}/${rest.split("/")[0] ?? ""}` : folder;
}

const biggestFirst =
  (groups: Map<string, number[]>) =>
  (a: string, b: string): number =>
    (groups.get(b)?.length ?? 0) - (groups.get(a)?.length ?? 0) || (a < b ? -1 : a > b ? 1 : 0);

/**
 * The files (their indexes in `paths`) grouped by folder, biggest first, as the engine groups
 * them: the top folders; one with more than a quarter of the files (and 40 or more) split into
 * its subfolders (its own files left under its name), again while one is and there is room;
 * then the groups under 0.5% of the files, and the smallest past 16, together as the rest.
 */
export function folderGroups(
  paths: readonly string[],
  { split = SPLIT, small = SMALL, most = MAX_FOLDERS } = {},
): [string, number[]][] {
  const total = paths.length;
  const groups = new Map<string, number[]>();
  const add = (into: Map<string, number[]>, key: string, i: number) => {
    const held = into.get(key);
    if (held) held.push(i);
    else into.set(key, [i]);
  };
  paths.forEach((path, i) => {
    add(groups, partOf(path, ""), i);
  });
  const done = new Set([ROOT]);
  for (;;) {
    const big = [...groups].filter(
      ([f, m]) => !done.has(f) && m.length > split * total && m.length >= SPLIT_FILES,
    );
    if (big.length === 0) break;
    const [folder, members] = big.reduce((best, g) =>
      g[1].length > best[1].length || (g[1].length === best[1].length && g[0] > best[0]) ? g : best,
    );
    const parts = new Map<string, number[]>();
    for (const i of members) add(parts, partOf(paths[i] ?? "", folder), i);
    const named = [...groups].filter(([f, m]) => f !== folder && m.length >= small * total).length;
    const room = most - named - 1;
    if ((parts.size === 1 && parts.has(folder)) || (parts.size > 1 && room < 2)) {
      done.add(folder);
      continue;
    }
    groups.delete(folder);
    const ranked = [...parts.keys()].sort(biggestFirst(parts));
    if (ranked.length > room) {
      const left: number[] = [];
      for (const f of ranked.slice(room - 1)) {
        left.push(...(parts.get(f) ?? []));
        parts.delete(f);
      }
      groups.set(
        folder,
        [...(groups.get(folder) ?? []), ...left].sort((a, b) => a - b),
      );
      done.add(folder);
    }
    for (const [f, m] of parts) groups.set(f, [...(groups.get(f) ?? []), ...m]);
  }
  const ranked = [...groups.keys()].sort(biggestFirst(groups));
  let kept = ranked.filter((f) => (groups.get(f)?.length ?? 0) >= small * total).slice(0, most - 1);
  if (kept.length === ranked.length - 1) kept = ranked; // one left over: its own name
  const out: [string, number[]][] = kept.map((f) => [f, groups.get(f) ?? []]);
  const rest = ranked
    .filter((f) => !kept.includes(f))
    .flatMap((f) => groups.get(f) ?? [])
    .sort((a, b) => a - b);
  if (rest.length > 0) out.push([REST, rest]);
  return out.sort(([a, am], [b, bm]) => bm.length - am.length || (a < b ? -1 : a > b ? 1 : 0));
}

/** By folder: the constellation's places, its stars grouped by folder (as the API). */
export function structural(points: ConstellationFixture["points"]): Omit<Galaxy, "sha"> {
  const groups = folderGroups(points.map((p) => p.path));
  const clusterOf = new Map<number, number>();
  groups.forEach(([, members], c) => {
    for (const i of members) clusterOf.set(i, c);
  });
  const stars: GalaxyStar[] = points.map((p, i) => ({
    path: p.path,
    x: p.x,
    y: p.y,
    z: p.z,
    cluster: clusterOf.get(i) ?? 0,
    layer: p.layer,
    language: p.language,
  }));
  const clusters: GalaxyCluster[] = groups.map(([label, at], c) => {
    const mean = (axis: "x" | "y" | "z") =>
      at.reduce((sum, i) => sum + (points[i]?.[axis] ?? 0), 0) / Math.max(1, at.length);
    return { id: c, label, x: mean("x"), y: mean("y"), z: mean("z"), files: at.length };
  });
  return { layout: "structural", stars, clusters };
}

/** 100,000 stars in 12 seeded clusters (spheres around points on a shell). */
function big(): Omit<Galaxy, "sha"> {
  let seed = 11;
  const random = () => {
    seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648;
    return seed / 2_147_483_648;
  };
  const layers = ["ui", "api", "svc", "db", "other"];
  const count = 12;
  const centers = Array.from({ length: count }, (_, c) => {
    const t = (c / count) * Math.PI * 2;
    return [Math.cos(t) * 0.7, Math.sin(t) * 0.7, (c % 3) * 0.3 - 0.3] as const;
  });
  const stars: GalaxyStar[] = Array.from({ length: BIG_STARS }, (_, i) => {
    const c = i % count;
    const [cx, cy, cz] = centers[c] ?? [0, 0, 0];
    const r = Math.cbrt(random()) * 0.22;
    const u = random() * 2 - 1;
    const t = random() * Math.PI * 2;
    const s = Math.sqrt(1 - u * u);
    return {
      path: `pkg${String(c)}/module${String(i)}.ts`,
      x: cx + r * s * Math.cos(t),
      y: cy + r * s * Math.sin(t),
      z: cz + r * u,
      cluster: c,
      layer: layers[c % layers.length] ?? "other",
      language: "typescript",
    };
  });
  const clusters = centers.map(([x, y, z], c) => ({
    id: c,
    label: `pkg${String(c)}`,
    x,
    y,
    z,
    files: BIG_STARS / count,
  }));
  return { layout: "semantic", stars, clusters };
}

export const galaxyHandlers = [
  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/galaxy", async ({ params, request }) => {
    const found = graphReady(params, request);
    if (found instanceof Response) return found;
    const layout = new URL(request.url).searchParams.get("layout") ?? "semantic";
    const situation = scenario();
    let body: Omit<Galaxy, "sha">;
    if (layout === "structural") {
      const fixture = (await import("../fixtures/constellation.json"))
        .default as ConstellationFixture;
      body = structural(fixture.points);
    } else if (situation === "big") {
      body = big();
    } else if (situation === "none") {
      return apiError(
        409,
        "semantic_not_ready",
        "The semantic layout needs this commit's summaries, embedded.",
      );
    } else {
      if (situation === "computing") {
        computingSince ??= Date.now();
        if (Date.now() - computingSince < READY_AFTER_MS) {
          const waiting = apiError(
            409,
            "galaxy_computing",
            "The semantic layout is being worked out; try again in a moment.",
          );
          waiting.headers.set("Retry-After", "2");
          return waiting;
        }
      }
      const fixture = (await import("../fixtures/galaxy.json")).default as Omit<Galaxy, "sha">;
      body = { ...fixture, layout: "semantic" };
    }
    return HttpResponse.json<Galaxy>(
      { ...body, sha: found.commit },
      { headers: { "X-Commit-Sha": found.commit } },
    );
  }),
];
