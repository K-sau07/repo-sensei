/**
 * A repository's overview (4.13), as the API sends it, and what its page draws from it: the
 * architecture map's layout (modules in columns by the layer most of their files are in, UI on
 * the left, data on the right) and the health tiles' shares.
 */
import type { Schemas } from "./types";

export type Overview = Schemas["OverviewOut"];
export type Module = Schemas["ModuleOut"];
export type ModuleLink = Schemas["ModuleLinkOut"];
export type Health = Schemas["HealthOut"];
export type Home = Schemas["HomeOut"];

export const MAP_LAYERS = ["ui", "api", "svc", "db", "other"] as const;
export type MapLayer = (typeof MAP_LAYERS)[number];

export const COLUMN_WIDTH = 220;
export const ROW_HEIGHT = 76;

/** The layer most of the module's files are in (ties go to the one further left). */
export function dominantLayer(layers: Record<string, number>): MapLayer {
  let best: MapLayer = "other";
  let most = 0;
  for (const layer of MAP_LAYERS) {
    const n = layers[layer] ?? 0;
    if (n > most) {
      best = layer;
      most = n;
    }
  }
  return best;
}

/** A module's short name: its last two directories ("." for the files at the top). */
export function moduleLabel(path: string): string {
  if (path === ".") return "(top level)";
  return path.split("/").slice(-2).join("/");
}

export interface PlacedModule {
  module: Module;
  layer: MapLayer;
  x: number;
  y: number;
}

/** Each module's place: a column per layer that has any (in MAP_LAYERS' order), the largest
 * first in each, the columns centred on one another. */
export function layoutModules(modules: readonly Module[]): PlacedModule[] {
  const columns = new Map<MapLayer, Module[]>();
  for (const held of modules) {
    const layer = dominantLayer(held.layers);
    columns.set(layer, [...(columns.get(layer) ?? []), held]);
  }
  const used = MAP_LAYERS.filter((layer) => columns.has(layer));
  return used.flatMap((layer, column) => {
    const held = [...(columns.get(layer) ?? [])].sort((a, b) => b.files - a.files);
    const top = (-(held.length - 1) * ROW_HEIGHT) / 2;
    return held.map((placed, row) => ({
      module: placed,
      layer,
      x: column * COLUMN_WIDTH,
      y: top + row * ROW_HEIGHT,
    }));
  });
}

/** A link's stroke: wider for more edges, from 1 to 4 px. */
export function linkWidth(edges: number, most: number): number {
  if (most <= 1) return 1.5;
  return 1 + (3 * Math.log(edges)) / Math.log(most);
}

/** `part` of `whole` as a whole percentage; null when there is nothing to divide. */
export function share(part: number | null, whole: number | null): number | null {
  if (part === null || whole === null || whole <= 0) return null;
  return Math.round((100 * part) / whole);
}

export interface HealthTile {
  label: string;
  value: string;
  detail: string;
  /** "warn" for a share under half. */
  tone: "ok" | "warn" | "muted";
}

function tone(percent: number | null): HealthTile["tone"] {
  if (percent === null) return "muted";
  return percent >= 50 ? "ok" : "warn";
}

/** The health tiles: symbols found, edges a compiler or resolver gave, client calls linked to
 * their endpoints, and freshness (unknown until pushes are acted on, and said so). */
export function healthTiles(health: Health): HealthTile[] {
  const files = health.languages.reduce((n, language) => n + language.files, 0);
  const defined = health.languages.reduce((n, language) => n + language.with_symbols, 0);
  const symbols = share(defined, files);
  const precise = share(health.edges.precise, health.edges.total);
  const linked = share(health.links.linked, health.links.client_calls);
  const shown = (percent: number | null) => (percent === null ? "–" : `${String(percent)}%`);
  return [
    {
      label: "Files with symbols",
      value: shown(symbols),
      detail: `${defined.toLocaleString("en")} of ${files.toLocaleString("en")} files`,
      tone: tone(symbols),
    },
    {
      label: "Precise edges",
      value: shown(precise),
      detail:
        health.edges.total > 0
          ? `${health.edges.precise.toLocaleString("en")} of ${health.edges.total.toLocaleString("en")}; ${health.edges.heuristic.toLocaleString("en")} by name or type`
          : "No edges",
      tone: tone(precise),
    },
    {
      label: "Calls linked",
      value: shown(linked),
      detail:
        health.links.client_calls === null
          ? "Not measured at this index"
          : `${String(health.links.linked ?? 0)} of ${String(health.links.client_calls)} client calls`,
      tone: tone(linked),
    },
    {
      label: "Behind the branch",
      value: health.freshness.behind === null ? "–" : String(health.freshness.behind),
      detail:
        health.freshness.behind === null
          ? "Not tracked yet"
          : `${String(health.freshness.behind)} commits since the index`,
      tone: "muted",
    },
  ];
}
