/**
 * The mock `GET …/galaxy?layout=semantic|structural`: by folder, the mock constellation's stars
 * each in its top folder's cluster (as the API groups them); by meaning, fixtures/galaxy.json
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

/** By folder: each star in its top folder's cluster, the biggest first (as the API). */
export function structural(points: ConstellationFixture["points"]): Omit<Galaxy, "sha"> {
  const top = (path: string) => (path.includes("/") ? (path.split("/")[0] ?? ROOT) : ROOT);
  const members = new Map<string, number[]>();
  points.forEach((p, i) => {
    const key = top(p.path);
    members.set(key, [...(members.get(key) ?? []), i]);
  });
  const order = [...members.keys()].sort(
    (a, b) => (members.get(b)?.length ?? 0) - (members.get(a)?.length ?? 0) || (a < b ? -1 : 1),
  );
  const ids = new Map(order.map((key, i) => [key, i]));
  const stars: GalaxyStar[] = points.map((p) => ({
    path: p.path,
    x: p.x,
    y: p.y,
    z: p.z,
    cluster: ids.get(top(p.path)) ?? 0,
    layer: p.layer,
    language: p.language,
  }));
  const clusters: GalaxyCluster[] = order.map((key) => {
    const at = members.get(key) ?? [];
    const mean = (axis: "x" | "y" | "z") =>
      at.reduce((sum, i) => sum + (points[i]?.[axis] ?? 0), 0) / Math.max(1, at.length);
    return {
      id: ids.get(key) ?? 0,
      label: key,
      x: mean("x"),
      y: mean("y"),
      z: mean("z"),
      files: at.length,
    };
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
