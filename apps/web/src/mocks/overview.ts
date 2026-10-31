/**
 * The mock overview (4.13) of the mock snapshot (the fastapi template at b1125d8): modules read
 * from its real tree the way the API reads them (sensei_api.routers.overview.modules_of), so the
 * map agrees with the code browser; the summary, links and health are what an index of it would
 * plausibly leave, fixed so stories and tests can name them.
 *
 *   localStorage.setItem("sensei.mock.overview", "bare")
 * an index that ran without summaries or links: no summary, no feature maps, links unmeasured.
 */
import type { Module, Overview } from "@/lib/api/overview";

import { BROWSE_COMMIT } from "./browse";
import tree from "./fixtures/browse-tree.json";

export const OVERVIEW_SCENARIO_KEY = "sensei.mock.overview";

const MAX_MODULES = 12;
const SPLIT_SHARE = 0.15;
const FOLD_SHARE = 0.03;
const NOT_SOURCE = new Set(["test", "config", "docs"]);
const LAYER: Record<string, string> = { ui: "ui", api: "api", service: "svc", db: "db" };

interface File {
  path: string;
  role: string;
}

const folderOf = (path: string) => path.slice(0, Math.max(0, path.lastIndexOf("/")));

/** As the API weighs them: each layer counts the same; failing any, source; failing that, all. */
function weights(files: readonly File[]): Map<string, number> {
  const perLayer = new Map<string, number>();
  for (const f of files) {
    const layer = LAYER[f.role];
    if (layer) perLayer.set(layer, (perLayer.get(layer) ?? 0) + 1);
  }
  if (perLayer.size > 0) {
    return new Map(
      files.map((f) => {
        const layer = LAYER[f.role];
        return [f.path, layer ? 1 / (perLayer.get(layer) ?? 1) : 0];
      }),
    );
  }
  const source = files.filter((f) => !NOT_SOURCE.has(f.role));
  const held = new Set((source.length > 0 ? source : files).map((f) => f.path));
  return new Map(files.map((f) => [f.path, held.has(f.path) ? 1 / held.size : 0]));
}

/** Each file's module, as the API picks them (sensei_api.routers.overview.modules_of): the
 * module with the most code split into its folders while it holds more than a share of it. */
export function moduleOf(files: readonly File[]): Map<string, string> {
  const weight = weights(files);
  const total = [...weight.values()].reduce((a, b) => a + b, 0);
  const direct = new Map<string, number>();
  const under = new Map<string, number>();
  const below = new Map<string, Set<string>>();
  for (const f of files) {
    let folder = folderOf(f.path);
    const w = weight.get(f.path) ?? 0;
    direct.set(folder, (direct.get(folder) ?? 0) + 1);
    under.set(folder, (under.get(folder) ?? 0) + w);
    while (folder) {
      const parent = folderOf(folder);
      below.set(parent, (below.get(parent) ?? new Set()).add(folder));
      under.set(parent, (under.get(parent) ?? 0) + w);
      folder = parent;
    }
  }
  const through = (start: string) => {
    let folder = start;
    while (!direct.get(folder) && below.get(folder)?.size === 1)
      folder = [...(below.get(folder) ?? [])][0] ?? folder;
    return folder;
  };
  const assign = (modules: Set<string>) =>
    new Map(
      files.map((f) => {
        let folder = folderOf(f.path);
        while (!modules.has(folder)) folder = folderOf(folder);
        return [f.path, folder];
      }),
    );
  const codeIn = (held: Map<string, string>) => {
    const code = new Map<string, number>();
    for (const [path, module] of held)
      code.set(module, (code.get(module) ?? 0) + (weight.get(path) ?? 0));
    return code;
  };
  let modules = new Set(["", through("")]);
  let held = assign(modules);
  const done = new Set<string>();
  for (;;) {
    const code = codeIn(held);
    const open = [...modules].filter((m) => !done.has(m));
    const heaviest = open.reduce<string | null>((best, m) => {
      if (best === null) return m;
      const [a, b] = [code.get(m) ?? 0, code.get(best) ?? 0];
      return a > b || (a === b && m > best) ? m : best; // a tie: the later path, as the API
    }, null);
    if (heaviest === null || (code.get(heaviest) ?? 0) <= SPLIT_SHARE * total) break;
    done.add(heaviest);
    const children = new Set([...(below.get(heaviest) ?? [])].map(through));
    const kept = [...children].filter(
      (c) => !under.get(c) || (under.get(c) ?? 0) >= FOLD_SHARE * total,
    );
    if (kept.length === 0) continue;
    const split = new Set([...modules, ...kept, ""]);
    if (!direct.get(heaviest) && kept.length === children.size) split.delete(heaviest);
    split.add("");
    const next = assign(split);
    if ([...codeIn(next).values()].filter((c) => c > 0).length > MAX_MODULES) continue;
    modules = split;
    held = next;
  }
  return new Map([...held].map(([path, module]) => [path, module || "."]));
}

/** The modules shown: the most code first, then the largest. */
export function mockModules(files: readonly File[] = tree.files): {
  modules: Module[];
  rest: number;
} {
  const of = moduleOf(files);
  const weight = weights(files);
  const held = new Map<string, { files: number; code: number; layers: Record<string, number> }>();
  for (const f of files) {
    const path = of.get(f.path) ?? ".";
    const counted = held.get(path) ?? { files: 0, code: 0, layers: {} };
    const layer = LAYER[f.role] ?? "other";
    counted.files += 1;
    counted.code += weight.get(f.path) ?? 0;
    counted.layers[layer] = (counted.layers[layer] ?? 0) + 1;
    held.set(path, counted);
  }
  const ranked = [...held.entries()].sort(
    (a, b) => b[1].code - a[1].code || b[1].files - a[1].files || (a[0] < b[0] ? -1 : 1),
  );
  const shown = ranked
    .slice(0, MAX_MODULES)
    .map(([path, m]) => ({ path, files: m.files, layers: m.layers }));
  return { modules: shown, rest: files.length - shown.reduce((n, m) => n + m.files, 0) };
}

function bare(): boolean {
  return (
    typeof localStorage !== "undefined" && localStorage.getItem(OVERVIEW_SCENARIO_KEY) === "bare"
  );
}

/** The overview; `without` summaries and links (the "bare" scenario by default). */
export function mockOverview(without = bare()): Overview {
  const { modules, rest } = mockModules();
  const has = new Set(modules.map((m) => m.path));
  const links = [
    { source: "frontend/src/client", target: "backend/app/api/routes", edges: 14 },
    { source: "frontend/src/components", target: "frontend/src/client", edges: 12 },
    { source: "backend/app/api/routes", target: "backend/app", edges: 11 },
    { source: "frontend/src", target: "frontend/src/components", edges: 9 },
    { source: "backend/app/api/routes", target: "backend/app/api", edges: 6 },
    { source: "backend/app", target: "backend/app/core", edges: 5 },
    { source: "backend/app/api", target: "backend/app/core", edges: 4 },
    { source: "backend/app/alembic/versions", target: "backend/app", edges: 2 },
  ].filter((link) => has.has(link.source) && has.has(link.target));
  return {
    sha: BROWSE_COMMIT,
    summary: without
      ? null
      : {
          text: "A full-stack web application template: a FastAPI backend with SQLModel and PostgreSQL, a React frontend generated against its OpenAPI client, email templates, and Docker Compose for running it all.",
          directories: [
            { path: "backend", summary: "The FastAPI service: routes, models, CRUD and auth." },
            { path: "frontend", summary: "The React app: routes, components and its API client." },
            { path: "packages", summary: "Email templates built with React Email." },
            { path: "scripts", summary: "Scripts that build, test and generate the client." },
          ],
        },
    architecture: { modules, links: without ? [] : links, rest },
    feature_maps: without
      ? []
      : [
          {
            key: "backend/app/api",
            path: "backend/app/api",
            title: "The API routes",
            summary: "Login, users, items and utilities, each a router mounted under /api/v1.",
            stale: false,
          },
          {
            key: "frontend/src/routes",
            path: "frontend/src/routes",
            title: "The app's pages",
            summary: "TanStack Router routes: the dashboard, items, settings and sign-in.",
            stale: false,
          },
          {
            key: "backend/app/core",
            path: "backend/app/core",
            title: "Configuration and security",
            summary: "Settings read from the environment, the database engine and JWT handling.",
            stale: true,
          },
        ],
    questions: without
      ? ["Where does this repository start, and what does it do first?"]
      : [
          "How does the API routes work?",
          "How does the app's pages work?",
          "What happens when POST /api/v1/items/ is called?",
          "What happens when POST /api/v1/login/access-token is called?",
        ],
    health: {
      languages: [
        { language: "typescript", files: 92, with_symbols: 81 },
        { language: "python", files: 52, with_symbols: 49 },
        { language: "yaml", files: 21, with_symbols: 0 },
      ],
      edges: { total: 1840, precise: 1412, heuristic: 371, inferred: 57 },
      links: without
        ? { client_calls: null, linked: null, unresolved_edges: null }
        : { client_calls: 24, linked: 21, unresolved_edges: 37 },
      freshness: { indexed_at: "2026-10-18T11:30:00Z", behind: null },
    },
    index: {
      job_id: "0199f000-0000-7000-8000-0000000000e1",
      kind: "full",
      started_at: "2026-10-18T11:29:50Z",
      finished_at: "2026-10-18T11:30:00Z",
      stages: [
        "clone",
        "discover",
        "merkle",
        "parse",
        "symbols",
        "chunks",
        "embeddings",
        "summaries",
        "graph",
        "links",
        "finalize",
      ].map((name) => ({ name, status: without && name === "summaries" ? "skipped" : "done" })),
    },
  };
}
