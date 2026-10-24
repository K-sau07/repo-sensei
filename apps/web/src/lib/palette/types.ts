/**
 * The ⌘K palette's item model. Every provider (pages, repositories, files, symbols, history,
 * actions, Ask) turns what it knows into these, and the ranking only ever sees these.
 */
import type { ShortcutId } from "@/lib/shortcut-registry";

/** Go somewhere, do something, or ask. Each is one section of the palette. */
export type PaletteGroup = "go" | "do" | "ask";

export type PaletteKind = "page" | "repo" | "file" | "dir" | "symbol" | "action" | "ask";

/** A half-open range `[start, end)` of characters, for highlighting what matched. */
export type MatchRange = readonly [start: number, end: number];

/** Where an item lives, for the context boost: an org, a repository in it, a path in that. */
export interface ItemScope {
  org: string;
  repo?: string;
  path?: string;
}

export interface PaletteItem {
  /** Stable across providers, so the same file from history and from search is one row. */
  id: string;
  group: PaletteGroup;
  kind: PaletteKind;
  title: string;
  subtitle?: string;
  /** Other words it answers to (never shown). */
  keywords?: readonly string[];
  /** Where it goes (a same-origin path); items without one `run` instead. */
  href?: string;
  run?: () => void;
  shortcut?: ShortcutId;
  /** Shown but not selectable, with the reason in its place. */
  disabled?: { reason: string };
  scope?: ItemScope;
  /** Shown muted, not counted as a result, never selected (a provider's error or empty state). */
  notice?: boolean;
  /** Hard to undo (signing out): never selected for the user on a synonym's say-so. */
  destructive?: boolean;
}

/** What the matcher needs of an item (also what crosses to the worker: no functions). */
export interface Matchable {
  id: string;
  title: string;
  subtitle?: string;
  keywords?: readonly string[];
}

export function toMatchable(item: PaletteItem): Matchable {
  return {
    id: item.id,
    title: item.title,
    ...(item.subtitle !== undefined ? { subtitle: item.subtitle } : {}),
    ...(item.keywords !== undefined ? { keywords: item.keywords } : {}),
  };
}
