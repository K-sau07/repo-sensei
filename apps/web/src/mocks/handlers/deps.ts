/**
 * The mock dependency endpoints (`…/deps`, `…/deps/dsm`, `…/deps/cycles`, `…/deps/edge-evidence`,
 * `…/deps/packages`) over fixtures/deps.json: the engine's own levels of the mock snapshot's graph
 * (scripts/mock_deps.py), with the graph endpoints' checks (404 before any index, 409
 * `graph_not_ready` during the first run). `focus` and `depth`, `types` and `min_conf` are
 * applied as the API applies them.
 *
 * A level the size the explorer must stay smooth at, from the browser:
 *   localStorage.setItem("sensei.mock.deps", "big")
 * (20,000 nodes in 40 clusters, at every level).
 */
import { http, HttpResponse } from "msw";

import {
  DEP_LEVELS,
  edgeKey,
  neighborhood,
  type DepEdge,
  type DepLevel,
  type DepNode,
  type Deps,
  type Dsm,
  type EdgeEvidence,
  type Packages,
} from "@/lib/api/deps";

import { validationFailed } from "./browse";
import { apiError } from "./errors";
import { graphReady } from "./graph";

interface Params {
  org: string;
  repo: string;
}

interface LevelFixture {
  deps: Omit<Deps, "sha">;
  dsm: Omit<Dsm, "sha">;
  evidence?: Record<string, EdgeEvidence>;
}

interface Fixture {
  levels: Record<DepLevel, LevelFixture>;
  packages: Omit<Packages, "sha">;
}

export const DEPS_SCENARIO_KEY = "sensei.mock.deps";
export const BIG_NODES = 20_000;
const BIG_CLUSTERS = 40;

let loaded: Promise<Fixture> | null = null;

function fixture(): Promise<Fixture> {
  loaded ??= import("../fixtures/deps.json").then((module) => module.default as Fixture);
  return loaded;
}

function big(): boolean {
  return typeof localStorage !== "undefined" && localStorage.getItem(DEPS_SCENARIO_KEY) === "big";
}

/** A seeded level of `BIG_NODES` nodes: clusters on a ring, each a disc of nodes, every node
 * depending on two others (mostly in its cluster). */
function bigLevel(level: DepLevel): Omit<Deps, "sha"> {
  let seed = 7;
  const random = () => {
    seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648;
    return seed / 2_147_483_648;
  };
  const layers = ["ui", "route", "service", "db", "other"];
  const nodes: DepNode[] = [];
  const edges: DepEdge[] = [];
  const per = BIG_NODES / BIG_CLUSTERS;
  for (let c = 0; c < BIG_CLUSTERS; c++) {
    const angle = (c / BIG_CLUSTERS) * Math.PI * 2;
    const [cx, cy] = [Math.cos(angle) * 0.75, Math.sin(angle) * 0.75];
    for (let i = 0; i < per; i++) {
      const r = Math.sqrt(random()) * 0.12;
      const t = random() * Math.PI * 2;
      nodes.push({
        id: `pkg${String(c)}/mod${String(i)}`,
        label: `mod${String(i)}`,
        layer: layers[c % layers.length] ?? "other",
        size: 1 + Math.floor(random() * 20),
        cluster: `pkg${String(c)}`,
        x: cx + Math.cos(t) * r,
        y: cy + Math.sin(t) * r,
      });
    }
  }
  const seen = new Set<string>();
  for (const [index, node] of nodes.entries()) {
    for (let k = 0; k < 2; k++) {
      const local = random() < 0.9;
      const other = local
        ? Math.floor(index / per) * per + Math.floor(random() * per)
        : Math.floor(random() * nodes.length);
      const target = nodes[other];
      const key = target ? edgeKey(node.id, target.id) : "";
      if (!target || target.id === node.id || seen.has(key)) continue;
      seen.add(key);
      edges.push({
        source: node.id,
        target: target.id,
        weight: 1,
        types: { imports: 1 },
        confidence: 0.9,
        samples: [],
      });
    }
  }
  return {
    level,
    layout: "spiral",
    total: nodes.length,
    truncated: false,
    nodes,
    edges,
    clusters: Array.from({ length: BIG_CLUSTERS }, (_, c) => ({
      id: `pkg${String(c)}`,
      nodes: per,
    })),
  };
}

function levelOf(raw: string | null): DepLevel | Response {
  const level = raw ?? "module";
  return (DEP_LEVELS as readonly string[]).includes(level)
    ? (level as DepLevel)
    : validationFailed("level", `Input should be ${DEP_LEVELS.map((l) => `'${l}'`).join(", ")}`);
}

/** The edges as `types` (comma-separated) and `min_conf` (0.5 unless asked) keep them: counted
 * by those types. */
function filtered(edges: readonly DepEdge[], url: URL): DepEdge[] {
  const wanted = (url.searchParams.get("types") ?? "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  const least = Number(url.searchParams.get("min_conf") ?? "0.5"); // the API's default
  const kept: DepEdge[] = [];
  for (const edge of edges) {
    if (edge.confidence < least) continue;
    if (wanted.length === 0) {
      kept.push(edge);
      continue;
    }
    const types = Object.fromEntries(
      Object.entries(edge.types).filter(([t]) => wanted.includes(t)),
    );
    const weight = Object.values(types).reduce((sum, n) => sum + n, 0);
    if (weight > 0) kept.push({ ...edge, types, weight });
  }
  return kept;
}

async function levelFor(params: Params, request: Request) {
  const found = graphReady(params, request);
  if (found instanceof Response) return found;
  const level = levelOf(found.url.searchParams.get("level"));
  if (level instanceof Response) return level;
  const data = await fixture();
  return { ...found, level, data, at: data.levels[level] };
}

export const depsHandlers = [
  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/deps", async ({ params, request }) => {
    const found = await levelFor(params, request);
    if (found instanceof Response) return found;
    const whole = big() ? bigLevel(found.level) : found.at.deps;
    let nodes = whole.nodes;
    let edges = filtered(whole.edges, found.url);
    const focus = found.url.searchParams.get("focus");
    if (focus !== null) {
      if (!nodes.some((n) => n.id === focus)) {
        return apiError(404, "deps_focus_not_found", `No ${found.level} named '${focus}'.`);
      }
      const depth = Number(found.url.searchParams.get("depth") ?? "1");
      const near = neighborhood(edges, focus, depth);
      nodes = nodes.filter((n) => near.has(n.id));
      edges = edges.filter((e) => near.has(e.source) && near.has(e.target));
    }
    return HttpResponse.json<Deps>(
      { ...whole, sha: found.commit, total: nodes.length, nodes, edges },
      { headers: { "X-Commit-Sha": found.commit } },
    );
  }),

  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/deps/dsm", async ({ params, request }) => {
    const found = await levelFor(params, request);
    if (found instanceof Response) return found;
    if (big()) {
      return apiError(
        422,
        "dsm_too_large",
        `Too many ${found.level}s (${String(BIG_NODES)}) to draw as a matrix: pick a coarser level.`,
      );
    }
    return HttpResponse.json<Dsm>({ ...found.at.dsm, sha: found.commit });
  }),

  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/deps/cycles", async ({ params, request }) => {
    const found = await levelFor(params, request);
    if (found instanceof Response) return found;
    const { level, cycles } = found.at.dsm;
    return HttpResponse.json({ level, cycles, sha: found.commit });
  }),

  http.get<Params>(
    "*/api/v1/orgs/:org/repos/:repo/deps/edge-evidence",
    async ({ params, request }) => {
      const found = await levelFor(params, request);
      if (found instanceof Response) return found;
      const src = found.url.searchParams.get("src");
      const dst = found.url.searchParams.get("dst");
      if (!src) return validationFailed("src", "Field required");
      if (!dst) return validationFailed("dst", "Field required");
      const held = found.at.evidence?.[edgeKey(src, dst)];
      const limit = Number(found.url.searchParams.get("limit") ?? "200");
      return HttpResponse.json<EdgeEvidence>(
        held ? { total: held.total, edges: held.edges.slice(0, limit) } : { total: 0, edges: [] },
      );
    },
  ),

  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/deps/packages", async ({ params, request }) => {
    const found = graphReady(params, request);
    if (found instanceof Response) return found;
    const data = await fixture();
    return HttpResponse.json<Packages>({ ...data.packages, sha: found.commit });
  }),
];
