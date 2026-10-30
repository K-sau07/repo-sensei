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
const MAX_DEPTH = 3;
const DOMINANT = 0.6;
const LAYER: Record<string, string> = { ui: "ui", api: "api", service: "svc", db: "db" };

function moduleAt(path: string, depth: number): string {
  const parts = path.split("/").slice(0, -1);
  return parts.length > 0 ? parts.slice(0, depth).join("/") : ".";
}

/** As the API picks them: the shallowest depth that splits the files without one module
 * holding most of them. */
export function mockModules(files = tree.files): { modules: Module[]; rest: number } {
  let depth = MAX_DEPTH;
  for (let d = 1; d <= MAX_DEPTH; d++) {
    const sizes = new Map<string, number>();
    for (const f of files)
      sizes.set(moduleAt(f.path, d), (sizes.get(moduleAt(f.path, d)) ?? 0) + 1);
    if (sizes.size >= 3 && Math.max(...sizes.values()) < DOMINANT * files.length) {
      depth = d;
      break;
    }
  }
  const held = new Map<string, { files: number; layers: Record<string, number> }>();
  for (const f of files) {
    const path = moduleAt(f.path, depth);
    const counted = held.get(path) ?? { files: 0, layers: {} };
    const layer = LAYER[f.role] ?? "other";
    counted.files += 1;
    counted.layers[layer] = (counted.layers[layer] ?? 0) + 1;
    held.set(path, counted);
  }
  const ranked = [...held.entries()].sort(
    (a, b) => b[1].files - a[1].files || (a[0] < b[0] ? -1 : 1),
  );
  const shown = ranked.slice(0, MAX_MODULES).map(([path, m]) => ({ path, ...m }));
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
    { source: "frontend", target: "backend", edges: 18 },
    { source: "backend", target: "packages", edges: 4 },
    { source: "scripts", target: "backend", edges: 3 },
    { source: "scripts", target: "frontend", edges: 2 },
    { source: ".github", target: "scripts", edges: 5 },
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
