/**
 * The palette's providers, as plain functions from what each knows to palette items: pages,
 * repositories, files, symbols, history and Ask. (Actions need the app's state: `use-actions`.)
 */
import {
  AppWindow,
  File,
  Folder,
  FolderGit2,
  MessageSquare,
  Search,
  SquareFunction,
  Zap,
  type LucideIcon,
} from "lucide-react";

import { ALL_TABS } from "@/components/repo/repo-tabs-config";
import { PRIMARY_NAV, SECONDARY_NAV } from "@/components/shell/nav-items";
import type { SymbolHit, Repo } from "@/lib/api/types";
import type { Feature } from "@/lib/indexing";
import type { HistoryEntry } from "@/lib/palette/history";
import {
  ORG_PAGE_TITLES,
  pageId,
  parentOf,
  pathId,
  repoId,
  symbolId,
} from "@/lib/palette/locations";
import type { PaletteItem, PaletteKind } from "@/lib/palette/types";
import { codePath, orgPath, repoPath } from "@/lib/routes";
import { searchPath } from "@/lib/search/url-state";

/** An item and the icon it shows (icons stay out of the pure model the worker sees). */
export type ShownItem = PaletteItem & { icon?: LucideIcon };

/** The repository the palette was opened in. */
export interface RepoContext {
  slug: string;
  /** The commit "no ref" means now (its last index), for permalinks. */
  indexedSha: string | null;
  /** What the code browser keys "no ref" reads by (see `useCodeLocation`). */
  version: string | null;
  /** A run is queued or running. */
  indexing: boolean;
  admin: boolean;
  /** Its features have loaded, and which are unlocked. */
  featuresReady: boolean;
  unlocked: (feature: Feature) => boolean;
}

export const KIND_ICONS: Record<PaletteKind, LucideIcon> = {
  page: AppWindow,
  repo: FolderGit2,
  file: File,
  dir: Folder,
  symbol: SquareFunction,
  action: Zap,
  ask: MessageSquare,
};

/** Shown beside each result, so a file and a page with the same name can be told apart. */
export const KIND_LABELS: Record<PaletteKind, string> = {
  page: "Page",
  repo: "Repository",
  file: "File",
  dir: "Folder",
  symbol: "Symbol",
  action: "Action",
  ask: "Ask",
};

// ----------------------------------------------------------------- pages

/** Org sections, then (in a repository) its tabs that are unlocked. Settings is an action. */
export function pageItems(org: string, repo: RepoContext | null, admin: boolean): ShownItem[] {
  const items: ShownItem[] = [];
  if (repo) {
    for (const tab of ALL_TABS) {
      if (tab.section === "") continue; // the repository itself is the repository item
      if (tab.adminOnly && !repo.admin) continue;
      if (tab.feature && !(repo.featuresReady && repo.unlocked(tab.feature))) continue;
      const href = repoPath(org, repo.slug, tab.section);
      items.push({
        id: pageId(href),
        group: "go",
        kind: "page",
        title: tab.label,
        subtitle: repo.slug,
        href,
        icon: tab.icon,
        ...(tab.shortcut ? { shortcut: tab.shortcut } : {}),
        scope: { org, repo: repo.slug },
      });
    }
  }
  const nav = [...PRIMARY_NAV, ...SECONDARY_NAV].filter((item) => item.section !== "settings");
  for (const entry of nav) {
    const href = orgPath(org, entry.section);
    items.push({
      id: pageId(href),
      group: "go",
      kind: "page",
      title: entry.label,
      href,
      icon: entry.icon,
      ...(entry.shortcut ? { shortcut: entry.shortcut } : {}),
      scope: { org },
    });
  }
  const members = orgPath(org, "settings/members");
  items.push({
    id: pageId(members),
    group: "go",
    kind: "page",
    title: ORG_PAGE_TITLES["settings/members"] ?? "Members",
    keywords: ["people", "invite", "team"],
    href: members,
    scope: { org },
  });
  if (admin) {
    const connect = orgPath(org, "repos/new");
    items.push({
      id: pageId(connect),
      group: "go",
      kind: "page",
      title: ORG_PAGE_TITLES["repos/new"] ?? "Connect a repository",
      keywords: ["add", "import", "github"],
      href: connect,
      scope: { org },
    });
  }
  return items;
}

// ----------------------------------------------------------------- repositories

export function repoItems(org: string, repos: readonly Repo[]): ShownItem[] {
  return repos.map((repo) => ({
    id: repoId(org, repo.slug),
    group: "go",
    kind: "repo",
    title: repo.slug,
    subtitle: repo.full_name,
    keywords: repo.name === repo.slug ? ["overview"] : [repo.name, "overview"],
    href: repoPath(org, repo.slug),
    scope: { org, repo: repo.slug },
  }));
}

// ----------------------------------------------------------------- files and symbols

export interface FileHit {
  path: string;
  name: string;
}

export function fileItem(org: string, repo: string, ref: string | null, hit: FileHit): ShownItem {
  const dir = parentOf(hit.path);
  return {
    id: pathId(org, repo, hit.path),
    group: "go",
    kind: "file",
    title: hit.name,
    subtitle: dir ? `${repo} · ${dir}` : repo,
    href: codePath(org, repo, hit.path, { ref }),
    scope: { org, repo, path: hit.path },
  };
}

export const linesOf = (hit: Pick<SymbolHit, "start_line" | "end_line">) =>
  hit.end_line > hit.start_line
    ? `${String(hit.start_line)}-${String(hit.end_line)}`
    : String(hit.start_line);

export function symbolItem(
  org: string,
  repo: string,
  ref: string | null,
  hit: SymbolHit,
): ShownItem {
  return {
    id: symbolId(org, repo, hit.symbol_key),
    group: "go",
    kind: "symbol",
    title: hit.name,
    subtitle: `${hit.kind} · ${hit.path}:${String(hit.start_line)}`,
    href: codePath(org, repo, hit.path, { ref, lines: linesOf(hit) }),
    scope: { org, repo, path: hit.path },
  };
}

/** A provider's error or empty state: one muted row in its group; the rest keep working. */
export function noticeItem(id: string, title: string): ShownItem {
  return { id: `notice:${id}`, group: "go", kind: "page", title, notice: true };
}

// ----------------------------------------------------------------- history

/** Recent places in this org, as the items they were (shown as "Recent"). Entries without a
 * link (actions picked) aren't shown; they only lift their action's rank. */
export function historyItems(entries: readonly HistoryEntry[], org: string): ShownItem[] {
  return entries
    .filter((entry) => entry.org === org && entry.href !== undefined)
    .map((entry) => ({
      id: entry.id,
      group: "go",
      kind: entry.kind,
      title: entry.title,
      ...(entry.subtitle !== undefined ? { subtitle: entry.subtitle } : {}),
      ...(entry.href !== undefined ? { href: entry.href } : {}),
      scope: {
        org: entry.org,
        ...(entry.repo !== undefined ? { repo: entry.repo } : {}),
        ...(entry.path !== undefined ? { path: entry.path } : {}),
      },
    }));
}

// ----------------------------------------------------------------- permalinks

/** A whole commit id: SHA-1 (40 hex) or SHA-256 (64 hex). */
export const isFullCommit = (ref: string) => /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(ref);

export interface PinCommits {
  /** The commit the page showed (its tree or file response's `X-Commit-Sha`). */
  rendered: string | null;
  /** The repository's last index: only for a page that rendered nothing and has no ref. */
  indexed: string | null;
}

/**
 * The current page's address, pinned on code pages to the commit it showed (`?ref=<sha>`), so
 * the link shows the same lines however the branch moves on. A ref that is already a whole
 * commit stays. A branch or a short prefix is replaced by the commit the page showed (the API
 * resolves a branch to its newest snapshot and a prefix to whatever commit it names, which
 * needn't be the last index); with nothing shown yet it stays as it is rather than guessed.
 */
export function permalinkFor(href: string, code: boolean, commits: PinCommits): string {
  const url = new URL(href);
  if (!code) return url.toString();
  const ref = url.searchParams.get("ref");
  if (ref !== null && isFullCommit(ref)) return url.toString();
  const commit = commits.rendered ?? (ref === null ? commits.indexed : null);
  if (commit !== null && isFullCommit(commit)) url.searchParams.set("ref", commit);
  return url.toString();
}

// ----------------------------------------------------------------- ask

/**
 * The repository Ask goes to: the one you're in, else the one you were in last, else the first
 * one connected. (There is no org-wide Ask page.) Outside a repository only connected ones count,
 * so `undefined` while the list loads, and null when there are none.
 */
export function askTarget(
  current: string | null,
  history: readonly HistoryEntry[],
  repos: readonly Repo[] | undefined,
  org: string,
): string | null | undefined {
  if (current) return current;
  if (repos === undefined) return undefined;
  const known = new Set(repos.map((repo) => repo.slug));
  const recent = history.find(
    (entry) => entry.org === org && entry.repo !== undefined && known.has(entry.repo),
  );
  return recent?.repo ?? repos[0]?.slug ?? null;
}

export function askHref(org: string, repo: string, query: string): string {
  const q = query.trim();
  return `${repoPath(org, repo, "ask")}${q ? `?${new URLSearchParams({ q }).toString()}` : ""}`;
}

export const ASK_ID = "ask";
export const SEARCH_ID = "search";

/** "Search “…”": the query on the repository's search page (only once something is typed). */
export function searchItem(org: string, repo: string, query: string): ShownItem | null {
  const q = query.trim();
  if (!q) return null;
  return {
    id: SEARCH_ID,
    group: "ask",
    kind: "action",
    title: `Search “${q}”`,
    subtitle: `in ${repo}`,
    href: searchPath(org, repo, { q }),
    icon: Search,
  };
}

/** `repo`: where it goes (see `askTarget`); undefined while that isn't known yet. */
export function askItem(org: string, repo: string | null | undefined, query: string): ShownItem {
  const q = query.trim();
  const title = q ? `Ask “${q}”` : "Ask a question";
  if (!repo) {
    return {
      id: ASK_ID,
      group: "ask",
      kind: "ask",
      title,
      disabled: {
        reason:
          repo === undefined ? "Checking repositories…" : "Connect a repository to ask about it",
      },
    };
  }
  return {
    id: ASK_ID,
    group: "ask",
    kind: "ask",
    title,
    subtitle: `about ${repo}`,
    href: askHref(org, repo, q),
  };
}
