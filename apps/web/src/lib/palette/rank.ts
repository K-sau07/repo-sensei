/**
 * The palette's ranking. An item's score is how well it matches, plus a **context boost** (it is
 * in the repository you're in, more so in the directory of the file you're on, most for that
 * file) and a **recency boost** (you went there or picked it lately; it fades with time).
 *
 * Server results arrive already ranked: their scores are normalised to 0–1 the same way local
 * matches are, never above the result before them, so they stay in the server's order among
 * themselves unless context or recency lifts one. Sorting ends on stable keys, so identical input
 * always gives the identical order.
 */
import type { HistoryEntry } from "./history";
import type { ItemMatch } from "./match";
import type { MatchRange, PaletteGroup, PaletteItem } from "./types";

export const CONTEXT_REPO = 0.3;
export const CONTEXT_DIR = 0.1;
export const CONTEXT_FILE = 0.05;
export const RECENCY_MAX = 0.2;
/** A visit counts half as much after this long. */
export const RECENCY_HALF_LIFE_MS = 3 * 24 * 60 * 60 * 1000;
/** Local matches weaker than this are noise (a few letters scattered through a long title). */
export const MIN_LOCAL_SCORE = 0.25;

/**
 * The best score each server search can give, to bring its scores to 0–1. Files:
 * `word_similarity` on the name (×2) and on the path, +1 for a name prefix, +0.5 for a path
 * substring (`GET /files/search`). Symbols already score 0–1.
 */
export const SERVER_SCORE_MAX = { file: 4.5, symbol: 1 } as const;

export interface RankContext {
  org: string | null;
  repo: string | null;
  /** The path open in the code browser ("" is its root; null elsewhere). */
  path: string | null;
  /** That path is a file (else a directory). */
  pathIsFile: boolean;
  now: number;
}

export type Source = "local" | "server";

export interface Candidate {
  item: PaletteItem;
  /** 0–1 (see `fuzzyMatch`; server scores normalised by `normaliseServerScores`). */
  match: number;
  title: readonly MatchRange[];
  subtitle: readonly MatchRange[];
  source: Source;
  /** Where its provider put it (registry order, or the server's rank). */
  order: number;
  /** It came from history (shown as "Recent"). */
  recent?: boolean;
  /** Only a synonym of the query matched it. */
  synonym?: boolean;
}

export interface Ranked extends Candidate {
  score: number;
}

const dirname = (path: string) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");

/**
 * The context boost. "The current directory" is the open directory itself, or the directory of
 * the open file (never a directory's parent: its siblings aren't where you are).
 */
export function contextBoost(item: PaletteItem, context: RankContext): number {
  const scope = item.scope;
  if (scope?.org !== context.org || !context.repo || scope.repo !== context.repo) return 0;
  let boost = CONTEXT_REPO;
  const current = context.path;
  if (current !== null && scope.path !== undefined) {
    const here = context.pathIsFile ? dirname(current) : current;
    if (dirname(scope.path) === here) boost += CONTEXT_DIR;
    if (context.pathIsFile && scope.path === current) boost += CONTEXT_FILE;
  }
  return boost;
}

/** Up to `RECENCY_MAX`, halving every `RECENCY_HALF_LIFE_MS`; things used often fade slower. */
export function recencyBoost(entry: HistoryEntry | undefined, now: number): number {
  if (!entry) return 0;
  const age = Math.max(0, now - entry.at);
  const decay = 2 ** (-age / RECENCY_HALF_LIFE_MS);
  const frequency = Math.min(1, 0.6 + 0.1 * (entry.count - 1));
  return RECENCY_MAX * decay * frequency;
}

/** Server scores → 0–1, in the server's order (each at most the one before it). */
export function normaliseServerScores(scores: readonly number[], max: number): number[] {
  const out: number[] = [];
  let ceiling = 1;
  for (const score of scores) {
    const value = Math.min(ceiling, Math.max(0, Number.isFinite(score) ? score / max : 0));
    out.push(value);
    ceiling = value;
  }
  return out;
}

/** Turn matcher output into candidates (dropping noise); `items` gives each id its item. */
export function localCandidates(
  matches: readonly ItemMatch[],
  items: ReadonlyMap<string, { item: PaletteItem; order: number; recent?: boolean }>,
  query: string,
): Candidate[] {
  const empty = query.trim() === "";
  const out: Candidate[] = [];
  for (const match of matches) {
    const found = items.get(match.id);
    if (!found) continue;
    if (!empty && match.score < MIN_LOCAL_SCORE) continue;
    out.push({
      item: found.item,
      match: match.score,
      title: match.title,
      subtitle: match.subtitle,
      source: "local",
      order: found.order,
      ...(found.recent ? { recent: true } : {}),
      ...(match.synonym ? { synonym: true } : {}),
    });
  }
  return out;
}

const GROUP_ORDER: Record<PaletteGroup, number> = { go: 0, do: 1, ask: 2 };
const SOURCE_ORDER: Record<Source, number> = { local: 0, server: 1 };

function compare(a: Ranked, b: Ranked): number {
  return (
    b.score - a.score ||
    GROUP_ORDER[a.item.group] - GROUP_ORDER[b.item.group] ||
    SOURCE_ORDER[a.source] - SOURCE_ORDER[b.source] ||
    a.order - b.order ||
    (a.item.title < b.item.title ? -1 : a.item.title > b.item.title ? 1 : 0) ||
    (a.item.id < b.item.id ? -1 : a.item.id > b.item.id ? 1 : 0)
  );
}

/**
 * Score, merge and sort. The same id from several providers (a file from history and from
 * search) becomes one row: the live one (it's current), scored and highlighted on its own text
 * (the row shows that text, not history's), and marked recent if history had it. Found twice
 * live (the same text), it keeps the better match.
 *
 * With an empty query nothing is matched: recent items come first (by their recency boost), then
 * everything else in its provider's order.
 */
export function rank(
  candidates: readonly Candidate[],
  context: RankContext,
  history: readonly HistoryEntry[],
  query: string,
): Ranked[] {
  const byId = new Map<string, Candidate>();
  for (const candidate of candidates) {
    const seen = byId.get(candidate.item.id);
    if (!seen) {
      byId.set(candidate.item.id, candidate);
      continue;
    }
    const recent = seen.recent === true || candidate.recent === true;
    if (recent && seen.recent !== candidate.recent) {
      // History's entry and the live item: the row is the live one, on its own text.
      byId.set(candidate.item.id, { ...(seen.recent ? candidate : seen), recent: true });
      continue;
    }
    // The same text twice: the better match, and a literal match from either wins.
    const better = candidate.match > seen.match ? candidate : seen;
    const { synonym: _synonym, ...rest } = better;
    byId.set(candidate.item.id, {
      ...rest,
      ...(recent ? { recent: true } : {}),
      ...(seen.synonym && candidate.synonym ? { synonym: true } : {}),
    });
  }
  const recency = new Map(history.map((entry) => [entry.id, entry]));
  const empty = query.trim() === "";
  const ranked: Ranked[] = [];
  for (const candidate of byId.values()) {
    const entry = recency.get(candidate.item.id);
    const score = empty
      ? candidate.recent && entry
        ? 1 + recencyBoost(entry, context.now) // recent first, above everything else
        : 0
      : candidate.match + contextBoost(candidate.item, context) + recencyBoost(entry, context.now);
    ranked.push({ ...candidate, score });
  }
  return ranked.sort(compare);
}

/**
 * Every row the selection can land on, in list order (a capped group's "more" row after its
 * rows), and whether the palette may select it by itself. It never selects a destructive action
 * that only a synonym matched: typing "log out" may mean "show me logout.py", and ↵ must not sign
 * anyone out by surprise.
 */
export function selectionOrder(sections: readonly Section[]): { value: string; auto: boolean }[] {
  return sections.flatMap((section) => [
    ...section.rows
      .filter((row) => !row.item.disabled && !row.item.notice)
      .map((row) => ({
        value: row.item.id,
        auto: !(row.item.destructive && row.synonym),
      })),
    ...(section.hidden > 0 ? [{ value: moreValue(section.group), auto: true }] : []),
  ]);
}

export const moreValue = (group: PaletteGroup) => `more:${group}`;

/** Results in total, before each group's cap (notices aren't results; Ask isn't either). */
export function resultCount(sections: readonly Section[]): number {
  return sections
    .filter((section) => section.group !== "ask")
    .reduce(
      (sum, section) =>
        sum + section.rows.filter((row) => !row.item.notice).length + section.hidden,
      0,
    );
}

export interface Section {
  group: PaletteGroup;
  rows: Ranked[];
  /** Rows left out by the cap. */
  hidden: number;
}

export const GROUP_CAP = 6;
export const GROUP_MAX = 50;

/**
 * Ranked items → the palette's sections, each capped (`expanded` ones show up to `GROUP_MAX`).
 * Go and Do come in the order of their best row; Ask is always last.
 */
export function toSections(
  ranked: readonly Ranked[],
  expanded: ReadonlySet<PaletteGroup> = new Set(),
): Section[] {
  const groups = new Map<PaletteGroup, Ranked[]>();
  for (const row of ranked) {
    const list = groups.get(row.item.group) ?? [];
    list.push(row);
    groups.set(row.item.group, list);
  }
  const sections: Section[] = [];
  for (const [group, rows] of groups) {
    // Notices (a provider's error) never take a result's place.
    const results = rows.filter((row) => !row.item.notice);
    const notices = rows.filter((row) => row.item.notice);
    const cap = group === "ask" ? 1 : expanded.has(group) ? GROUP_MAX : GROUP_CAP;
    sections.push({
      group,
      rows: [...results.slice(0, cap), ...notices],
      hidden: Math.max(0, results.length - cap),
    });
  }
  const best = (section: Section) => section.rows.find((row) => !row.item.notice)?.score ?? -1;
  return sections.sort((a, b) => {
    if (a.group === "ask" || b.group === "ask") return a.group === "ask" ? 1 : -1;
    return best(b) - best(a) || GROUP_ORDER[a.group] - GROUP_ORDER[b.group];
  });
}
