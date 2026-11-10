/**
 * Bundle budgets (10.11): the JavaScript each route loads before it can run (its layouts' and
 * page's client entries, and the framework's), gzipped, from a production build's manifests.
 * Heavy views loaded on demand (3D, graph, diagrams, the editor, an answer's markdown) aren't
 * counted.
 *
 * Each route has a ceiling (`bundle-budget.json`): what it weighed when last recorded, plus a
 * little. Growing past it fails the build, so the size only goes down; `--update` records the
 * sizes as they are now (after making a route lighter). The design's target (180 KB for the
 * workspace's routes) is reported alongside: the distance still to go.
 *
 *   node scripts/bundle-budget.mjs [--update] [distDir]
 */
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import { gzipSync } from "node:zlib";

const KB = 1024;
const TARGET_KB = 180; // frontend design 9: JS per route (initial), the workspace shell
const HEADROOM = 1.03; // a ceiling's room over what was recorded
const CEILINGS = join(dirname(fileURLToPath(import.meta.url)), "bundle-budget.json");

const args = process.argv.slice(2);
const update = args.includes("--update");
const dist = args.find((arg) => !arg.startsWith("--")) ?? ".next";
const appDir = join(dist, "server", "app");
if (!existsSync(appDir)) {
  console.error(`No build at ${dist}: run next build first.`);
  process.exit(2);
}

function* manifests(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) yield* manifests(path);
    else if (entry.name === "page_client-reference-manifest.js") yield path;
  }
}

const sizes = new Map();
function gzipped(file) {
  if (!sizes.has(file)) sizes.set(file, gzipSync(readFileSync(join(dist, file))).length);
  return sizes.get(file);
}

/** Each route and the gzipped KB it loads first. */
function measure() {
  const routes = {};
  for (const path of manifests(appDir)) {
    const context = {};
    context.globalThis = context;
    runInNewContext(readFileSync(path, "utf8"), context);
    const [[key, manifest]] = Object.entries(context.__RSC_MANIFEST);
    const files = new Set(Object.values(manifest.entryJSFiles ?? {}).flat());
    const build = join(path.replace(/_client-reference-manifest\.js$/, ""), "build-manifest.json");
    if (existsSync(build)) {
      for (const file of JSON.parse(readFileSync(build, "utf8")).rootMainFiles ?? []) {
        files.add(file);
      }
    }
    const route = key.replace(/\/page$/, "") || "/";
    routes[route] = [...files].reduce((sum, file) => sum + gzipped(file), 0) / KB;
  }
  return routes;
}

const routes = measure();
const names = Object.keys(routes).sort((a, b) => routes[b] - routes[a]);

if (update) {
  const ceilings = Object.fromEntries(
    Object.keys(routes)
      .sort()
      .map((route) => [route, Math.ceil(routes[route] * HEADROOM)]),
  );
  writeFileSync(CEILINGS, `${JSON.stringify(ceilings, null, 2)}\n`);
  console.log(`Recorded ${String(names.length)} routes' ceilings in ${CEILINGS}.`);
  process.exit(0);
}

const ceilings = existsSync(CEILINGS) ? JSON.parse(readFileSync(CEILINGS, "utf8")) : {};
const widest = Math.max(TARGET_KB, ...Object.values(ceilings)); // a new route: no heavier
let over = 0;
for (const route of names) {
  const kb = routes[route];
  const ceiling = ceilings[route] ?? widest;
  const past = kb > ceiling;
  if (past) over += 1;
  const target = kb > TARGET_KB ? `+${(kb - TARGET_KB).toFixed(0)} KB to target` : "within target";
  console.log(
    `${kb.toFixed(1).padStart(7)} KB  ceiling ${String(ceiling).padStart(4)}  ${target.padEnd(20)} ${route}${past ? "  OVER ITS CEILING" : ""}`,
  );
}
const within = names.filter((route) => routes[route] <= TARGET_KB).length;
console.log(
  `\n${String(within)} of ${String(names.length)} routes within the ${String(TARGET_KB)} KB target.`,
);
if (over > 0) {
  console.error(
    `${String(over)} route(s) grew past their ceiling: make them lighter (load the heavy part on demand), or say why and run --update.`,
  );
  process.exit(1);
}
