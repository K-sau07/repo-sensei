/**
 * The palette's local history: pages visited and items picked, most recent first, capped. It
 * lives in localStorage, which anyone (or an old version) may have left anything in, so every
 * entry is checked on the way in and anything malformed is dropped, never trusted (an `href`
 * must be a path on this site).
 *
 * Each user has their own (keyed by user id), and signing out or a session ending clears all of
 * them: on a shared browser nobody sees where someone else has been.
 */
import type { PaletteKind } from "./types";

export const HISTORY_PREFIX = "sensei.palette.history.v1";
export const historyKey = (user: string) => `${HISTORY_PREFIX}:${encodeURIComponent(user)}`;
export const HISTORY_CAP = 50;
/** A visit right after a pick (or a reload) is the same use, not another one. */
export const REPEAT_WINDOW_MS = 5_000;
const MAX_TEXT = 300;
const KINDS = new Set<PaletteKind>(["page", "repo", "file", "dir", "symbol", "action"]);

export interface HistoryEntry {
  id: string;
  kind: PaletteKind;
  title: string;
  subtitle?: string;
  /** Where it goes; entries without one (actions) only lift their item's rank. */
  href?: string;
  org: string;
  repo?: string;
  path?: string;
  /** Last used (ms since the epoch). */
  at: number;
  /** Times used. */
  count: number;
}

export type HistoryInput = Omit<HistoryEntry, "at" | "count">;

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem" | "key" | "length">;

function defaultStorage(): StorageLike | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null; // access itself can throw (blocked storage)
  }
}

const text = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0 && value.length <= MAX_TEXT;

// Any origin works: a path that stays on it stays on ours.
const ORIGIN = "https://palette.invalid";

/** Whitespace, control characters or a backslash: the URL parser drops or rewrites them, so
 * "/\t/evil.example" would become "//evil.example". */
function hasUnsafeCharacter(value: string): boolean {
  for (const char of value) {
    const code = char.charCodeAt(0);
    if (code <= 0x20 || code === 0x7f || char === "\\" || /\s/u.test(char)) return true;
  }
  return false;
}

/** Only paths on this site: "/acme/api", never "//evil.example", "/\t/evil.example" or
 * "javascript:…". */
export function isSafeHref(value: unknown): value is string {
  if (!text(value) || !value.startsWith("/") || value.startsWith("//")) return false;
  if (hasUnsafeCharacter(value)) return false;
  try {
    return new URL(value, ORIGIN).origin === ORIGIN;
  } catch {
    return false;
  }
}

/** One stored entry, checked field by field; null if it isn't one. */
export function parseEntry(value: unknown): HistoryEntry | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Record<string, unknown>;
  if (!text(raw.id) || !text(raw.title) || !text(raw.org)) return null;
  if (typeof raw.kind !== "string" || !KINDS.has(raw.kind as PaletteKind)) return null;
  if (typeof raw.at !== "number" || !Number.isFinite(raw.at) || raw.at <= 0) return null;
  const count =
    typeof raw.count === "number" && Number.isFinite(raw.count) && raw.count >= 1
      ? Math.min(Math.floor(raw.count), 1000)
      : 1;
  if (raw.href !== undefined && !isSafeHref(raw.href)) return null;
  return {
    id: raw.id,
    kind: raw.kind as PaletteKind,
    title: raw.title,
    ...(text(raw.subtitle) ? { subtitle: raw.subtitle } : {}),
    ...(isSafeHref(raw.href) ? { href: raw.href } : {}),
    org: raw.org,
    ...(text(raw.repo) ? { repo: raw.repo } : {}),
    ...(typeof raw.path === "string" && raw.path.length <= 4096 ? { path: raw.path } : {}),
    at: raw.at,
    count,
  };
}

/** Most recent first, one entry per id, at most `HISTORY_CAP`. */
function normalise(entries: readonly HistoryEntry[]): HistoryEntry[] {
  const seen = new Set<string>();
  const out: HistoryEntry[] = [];
  const sorted = [...entries].sort((a, b) => b.at - a.at || (a.id < b.id ? -1 : 1));
  for (const entry of sorted) {
    if (seen.has(entry.id)) continue;
    seen.add(entry.id);
    out.push(entry);
    if (out.length === HISTORY_CAP) break;
  }
  return out;
}

/** This user's history (none while we don't know who is signed in). */
export function readHistory(
  user: string | null,
  storage: StorageLike | null = defaultStorage(),
): HistoryEntry[] {
  if (!storage || !user) return [];
  try {
    const raw = storage.getItem(historyKey(user));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return normalise(parsed.map(parseEntry).filter((entry) => entry !== null));
  } catch {
    return [];
  }
}

function write(storage: StorageLike, user: string, entries: readonly HistoryEntry[]): void {
  try {
    storage.setItem(historyKey(user), JSON.stringify(entries));
  } catch {
    // Full or blocked storage: history is a nicety, the palette works without it.
  }
}

/** Note a use: the entry moves to the front and counts once more (not again within
 * `REPEAT_WINDOW_MS`). Returns the history as stored. */
export function recordHistory(
  user: string | null,
  input: HistoryInput,
  now: number = Date.now(),
  storage: StorageLike | null = defaultStorage(),
): HistoryEntry[] {
  if (!storage || !user) return [];
  const entries = readHistory(user, storage);
  const candidate = parseEntry({ ...input, at: now, count: 1 });
  if (!candidate) return entries;
  const previous = entries.find((entry) => entry.id === input.id);
  const repeat = previous !== undefined && now - previous.at < REPEAT_WINDOW_MS;
  const entry = { ...candidate, count: (previous?.count ?? 0) + (repeat ? 0 : 1) };
  const next = normalise([entry, ...entries.filter((item) => item.id !== input.id)]);
  write(storage, user, next);
  return next;
}

/** Every user's history (on sign-out and when a session ends). */
export function clearHistory(storage: StorageLike | null = defaultStorage()): void {
  if (!storage) return;
  try {
    const keys: string[] = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key?.startsWith(HISTORY_PREFIX)) keys.push(key);
    }
    for (const key of keys) storage.removeItem(key);
  } catch {
    // Nothing to do: it can't be read either.
  }
}
