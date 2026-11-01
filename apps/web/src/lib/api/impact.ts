/**
 * Impact (6.4, 6.8): the endpoint's types, and what the impact page works out from an answer:
 * where each node is drawn (the change at the center, each ring a hop further), its heat color
 * (the score on a signal → warn → danger ramp), and the answer as a PR note.
 */
import type { Route } from "next";

import { repoPath } from "@/lib/routes";

import type { Schemas } from "./types";

export type Impact = Schemas["ImpactOut"];
export type ImpactNode = Schemas["ImpactNodeOut"];
export type Affected = Schemas["AffectedOut"];
export type ImpactCategory = keyof Impact["affected"];

/** The lists beside the graph, in the order a reviewer reads them (other symbols last). */
export const IMPACT_CATEGORIES: readonly ImpactCategory[] = [
  "routes",
  "screens",
  "tests",
  "tables",
  "symbols",
];

export const CATEGORY_LABELS: Record<ImpactCategory, string> = {
  routes: "Routes",
  screens: "Screens",
  tests: "Tests",
  tables: "Tables",
  symbols: "Other code",
};

/** The impact page for a change to a symbol (its key or name) or to some files. */
export function impactHref(
  org: string,
  repo: string,
  change: { symbol?: string; files?: readonly string[] } = {},
): Route {
  const query = new URLSearchParams();
  if (change.symbol) query.set("symbol", change.symbol);
  for (const file of change.files ?? []) query.append("files", file);
  const search = query.size > 0 ? `?${query.toString()}` : "";
  return `${repoPath(org, repo, "impact")}${search}` as Route;
}

export function allAffected(impact: Impact): Affected[] {
  return IMPACT_CATEGORIES.flatMap((c) => impact.affected[c]);
}

export interface Placed {
  x: number;
  y: number;
}

/** Where each node is drawn, within [-1, 1]: the changed ones at the center, each affected one
 * on the ring of its distance, a ring's nodes in their way's order (so a branch stays together:
 * a node beside the one it is reached through, on its ring or the one inside), evenly around. */
export function blastLayout(
  changed: readonly ImpactNode[],
  affected: readonly Affected[],
): Map<string, Placed> {
  const placed = new Map<string, Placed>();
  const angles = new Map<string, number>();
  const center = changed.length === 1 ? 0 : 0.06;
  changed.forEach((node, i) => {
    const angle = (i / Math.max(1, changed.length)) * Math.PI * 2;
    angles.set(node.key, angle);
    placed.set(node.key, { x: Math.cos(angle) * center, y: Math.sin(angle) * center });
  });
  const deepest = Math.max(1, ...affected.map((a) => a.distance));
  const rings = new Map<number, Affected[]>();
  for (const item of affected) {
    if (placed.has(item.key)) continue;
    const ring = rings.get(item.distance);
    if (ring) ring.push(item);
    else rings.set(item.distance, [item]);
  }
  for (const distance of [...rings.keys()].sort((a, b) => a - b)) {
    const found = rings.get(distance) ?? [];
    const angleOf = (item: Affected) => (item.via !== null ? (angles.get(item.via) ?? 0) : 0);
    const order = (a: Affected, b: Affected) =>
      angleOf(a) - angleOf(b) || b.score - a.score || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
    // Reached through one on this ring (an endpoint through its handler: no hop): right after it.
    const here = new Set(found.map((a) => a.key));
    const after = new Map<string, Affected[]>();
    for (const item of found) {
      if (item.via !== null && here.has(item.via)) {
        after.set(item.via, [...(after.get(item.via) ?? []), item]);
      }
    }
    const ring: Affected[] = [];
    const visit = (item: Affected) => {
      ring.push(item);
      for (const next of (after.get(item.key) ?? []).sort(order)) visit(next);
    };
    for (const item of found.filter((a) => a.via === null || !here.has(a.via)).sort(order)) {
      visit(item);
    }
    const radius = (distance / deepest) * 0.95;
    ring.forEach((item, i) => {
      // Offset by the first one's way, so a lone node sits beside what it is reached through.
      const angle = angleOf(ring[0] ?? item) + (i / ring.length) * Math.PI * 2;
      angles.set(item.key, angle);
      placed.set(item.key, { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius });
    });
  }
  return placed;
}

function channels(hex: string): [number, number, number] {
  const value = hex.replace("#", "");
  return [0, 2, 4].map((i) => Number.parseInt(value.slice(i, i + 2), 16)) as [
    number,
    number,
    number,
  ];
}

function mix(a: string, b: string, t: number): string {
  const [x, y] = [channels(a), channels(b)];
  const parts = x.map((v, i) => Math.round(v + ((y[i] ?? v) - v) * t));
  return `#${parts.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

/** A score's color (hex): the least sure and furthest the signal color, through warn, to the
 * surest and nearest in danger. */
export function heatColor(score: number, ramp: { low: string; mid: string; high: string }): string {
  const t = Math.min(1, Math.max(0, score));
  return t < 0.5 ? mix(ramp.low, ramp.mid, t * 2) : mix(ramp.mid, ramp.high, (t - 0.5) * 2);
}

export function percent(score: number): string {
  return `${String(Math.round(score * 100))}%`;
}

/** The answer as markdown for a pull request: what the change may break, by category, each
 * ranked with how sure and how far. */
export function prNote(impact: Impact, { listed = 10 }: { listed?: number } = {}): string {
  const names = impact.changed.map((n) => `\`${n.name}\``).join(", ");
  const lines = [`### Impact of changing ${names}`, ""];
  let any = false;
  for (const category of IMPACT_CATEGORIES) {
    const items = impact.affected[category];
    if (items.length === 0) continue;
    any = true;
    lines.push(`**${CATEGORY_LABELS[category]}** (${String(items.length)})`, "");
    for (const item of items.slice(0, listed)) {
      const where = item.path ? ` — \`${item.path}:${String(item.start_line)}\`` : "";
      const hops = `${String(item.distance)} ${item.distance === 1 ? "hop" : "hops"}`;
      lines.push(`- \`${item.name}\`${where} (${percent(item.score)} sure, ${hops})`);
    }
    if (items.length > listed) lines.push(`- …and ${String(items.length - listed)} more`);
    lines.push("");
  }
  if (!any) lines.push("Nothing in the code graph depends on it.", "");
  lines.push(
    `<sub>From the code graph at \`${impact.sha.slice(0, 7)}\`: a score is the surest way back to the change, ×0.85 a hop.${impact.truncated ? " The list was cut at its longest." : ""}</sub>`,
  );
  return `${lines.join("\n")}\n`;
}
