"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown } from "lucide-react";
import type { Route } from "next";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { useMemo, useRef, useState, type KeyboardEvent } from "react";

import { DelayedSpinner } from "@/components/primitives/delayed-spinner";
import { KeyHint } from "@/components/primitives/key-hint";
import { useRepo, useRepoFeatures } from "@/components/repo/use-repo";
import {
  Command,
  CommandGroup,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "@/components/ui/command";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { readHistory, recordHistory } from "@/lib/palette/history";
import { matchItem, matchItems } from "@/lib/palette/match";
import {
  GROUP_CAP,
  localCandidates,
  moreValue,
  normaliseServerScores,
  rank,
  resultCount,
  selectionOrder,
  SERVER_SCORE_MAX,
  toSections,
  type Candidate,
  type RankContext,
  type Ranked,
} from "@/lib/palette/rank";
import { toMatchable, type MatchRange, type PaletteGroup } from "@/lib/palette/types";
import { shortcut } from "@/lib/shortcut-registry";

import {
  askItem,
  askTarget,
  fileItem,
  historyItems,
  KIND_ICONS,
  KIND_LABELS,
  noticeItem,
  pageItems,
  searchItem,
  repoItems,
  symbolItem,
  type RepoContext,
  type ShownItem,
} from "./items";
import { PaletteInput, useListState } from "./palette-input";
import { isFilePath } from "./rendered";
import { useActions } from "./use-actions";
import { useServerResults } from "./use-server-results";
import { useWorkerMatches } from "./use-worker-matches";

/** Where the palette was opened: the org, and the repository, path and ref in the URL. */
export interface PaletteLocation {
  org: string;
  repo: string | null;
  /** The path open in the code browser. */
  path: string | null;
  ref: string | null;
  /** The code browser is open. */
  code: boolean;
}

function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

export function usePaletteLocation(): PaletteLocation {
  const params = useParams<{ org: string; repo?: string; path?: string[] }>();
  const pathname = usePathname();
  const search = useSearchParams();
  const repo = params.repo ?? null;
  const code = repo !== null && pathname.split("/").filter(Boolean)[2] === "code";
  const path = code ? (params.path ?? []).map(decodeSegment).join("/") : null;
  return { org: params.org, repo, path, ref: code ? search.get("ref") : null, code };
}

const GROUP_HEADINGS: Record<PaletteGroup, string> = { go: "Go", do: "Do", ask: "Ask" };
// Keys that move the selection (cmdk's, including its Ctrl+N/P/J/K bindings).
const MOVES = new Set(["ArrowDown", "ArrowUp", "Home", "End", "PageDown", "PageUp"]);
const CTRL_MOVES = new Set(["n", "p", "j", "k"]);

/** `text` with the matched characters emphasised. */
function Highlight({ text, ranges }: { text: string; ranges: readonly MatchRange[] }) {
  if (ranges.length === 0) return text;
  const parts = [];
  let at = 0;
  for (const [start, end] of ranges) {
    if (start > at) parts.push(text.slice(at, start));
    parts.push(
      <mark key={start} className="bg-transparent font-semibold text-fg">
        {text.slice(start, end)}
      </mark>,
    );
    at = end;
  }
  if (at < text.length) parts.push(text.slice(at));
  return parts;
}

function Row({ row, onSelect }: { row: Ranked; onSelect: () => void }) {
  const item = row.item as ShownItem;
  const Icon = item.icon ?? KIND_ICONS[item.kind];
  const badge = item.notice
    ? null
    : row.recent
      ? "Recent"
      : item.kind === "page" || item.kind === "action" || item.kind === "ask"
        ? null
        : KIND_LABELS[item.kind];
  const subtitle = item.disabled?.reason ?? item.subtitle;
  return (
    <CommandItem
      value={item.id}
      disabled={item.disabled !== undefined || item.notice === true}
      onSelect={onSelect}
      className={item.notice ? "text-fg-muted data-[disabled=true]:opacity-100" : "min-h-9"}
      data-kind={item.kind}
    >
      <Icon aria-hidden className="size-4 shrink-0 text-fg-muted" />
      <span className="flex min-w-0 flex-1 items-baseline gap-2">
        <span className="truncate">
          <Highlight text={item.title} ranges={row.title} />
        </span>
        {subtitle && (
          <span className="min-w-0 truncate text-xs text-fg-muted">
            {item.disabled ? subtitle : <Highlight text={subtitle} ranges={row.subtitle} />}
          </span>
        )}
      </span>
      {badge && <span className="shrink-0 text-xs text-fg-muted">{badge}</span>}
      {item.kind === "ask" && !item.disabled && (
        <CommandShortcut>
          <KeyHint shortcut="tab" />
        </CommandShortcut>
      )}
      {item.shortcut && (
        <CommandShortcut>
          <KeyHint shortcut={shortcut(item.shortcut).keys} />
        </CommandShortcut>
      )}
    </CommandItem>
  );
}

function notIndexed(error: unknown): boolean {
  return (
    error instanceof ApiError && (error.code === "not_indexed" || error.code === "repo_not_indexed")
  );
}

/** Files or symbols from the server, scored for merging; an error becomes one muted row. */
function serverCandidates<T>(
  state: { hits: T[]; error: unknown },
  build: (hit: T) => ShownItem,
  score: (hit: T) => number,
  max: number,
  offset: number,
  query: string,
  errors: { notIndexed: string; failed: string; id: string },
): Candidate[] {
  if (state.error) {
    const title = notIndexed(state.error) ? errors.notIndexed : errors.failed;
    return [
      {
        item: noticeItem(errors.id, title),
        match: 0,
        title: [],
        subtitle: [],
        source: "server",
        order: offset,
      },
    ];
  }
  const normalised = normaliseServerScores(state.hits.map(score), max);
  return state.hits.map((hit, index) => {
    const item = build(hit);
    // Matched here only to highlight; the server's score decides.
    const shown = matchItem(query, toMatchable(item));
    return {
      item,
      match: normalised[index] ?? 0,
      title: shown?.title ?? [],
      subtitle: shown?.subtitle ?? [],
      source: "server",
      order: offset + index,
    };
  });
}

interface PaletteProps {
  location: PaletteLocation;
  repo: RepoContext | null;
  onClose: () => void;
}

/** What to keep of an item picked, so it comes back as "Recent" and ranks higher. */
function remember(item: ShownItem, org: string, user: string | null) {
  if (item.notice || item.disabled || item.group === "ask") return;
  const scope = item.scope;
  recordHistory(user, {
    id: item.id,
    kind: item.kind,
    title: item.title,
    ...(item.subtitle !== undefined ? { subtitle: item.subtitle } : {}),
    ...(item.href !== undefined ? { href: item.href } : {}),
    org: scope?.org ?? org,
    ...(scope?.repo !== undefined ? { repo: scope.repo } : {}),
    ...(scope?.path !== undefined ? { path: scope.path } : {}),
  });
}

function Palette({ location, repo, onClose }: PaletteProps) {
  const { org } = location;
  const router = useRouter();
  const queryClient = useQueryClient();
  const { role, me } = useOrgRole();
  const admin = roleAtLeast(role, "admin");
  const user = me.data?.id ?? null;
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<ReadonlySet<PaletteGroup>>(new Set());
  const [chosen, setChosen] = useState("");
  const [moved, setMoved] = useState(false);
  // Read once per opening (and user): what was used before this one.
  const history = useMemo(() => readHistory(user), [user]);
  const [now] = useState(() => Date.now());
  const listRef = useRef<HTMLDivElement>(null);
  const list = useListState(listRef);

  // The sidebar's query: read from its cache, never fetched again just for the palette.
  const repos = $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos",
    { params: { path: { org } } },
    { refetchOnMount: false },
  );

  // Small, fixed sets are matched right here, so they are there the frame the palette opens.
  const pages = pageItems(org, repo, admin);
  const actions = useActions({
    org,
    repo,
    code: location.code,
    path: location.path,
    ref: location.ref,
  });
  const statics: ShownItem[] = [...pages, ...actions];
  const staticMatches = matchItems(query, statics.map(toMatchable));

  // Repositories and history can be long: matched in the worker.
  const repoList = repos.data;
  const repoRows = useMemo(() => repoItems(org, repoList ?? []), [org, repoList]);
  const recentRows = useMemo(() => historyItems(history, org), [history, org]);
  const repoMatchables = useMemo(() => repoRows.map(toMatchable), [repoRows]);
  const recentMatchables = useMemo(() => recentRows.map(toMatchable), [recentRows]);
  const repoMatches = useWorkerMatches("repos", repoMatchables, query);
  const recentMatches = useWorkerMatches("history", recentMatchables, query);

  const server = useServerResults({ org, repo: repo?.slug ?? null, ref: location.ref }, query);

  const index = <T extends ShownItem>(items: readonly T[], offset: number, recent = false) =>
    new Map(items.map((item, i) => [item.id, { item, order: offset + i, recent }]));
  const slug = repo?.slug ?? null;
  const candidates: Candidate[] = [
    ...localCandidates(staticMatches, index(statics, 0), query),
    ...localCandidates(repoMatches, index(repoRows, 500), query),
    ...localCandidates(recentMatches, index(recentRows, 1000, true), query),
    ...(slug
      ? [
          ...serverCandidates(
            server.files,
            (hit) => fileItem(org, slug, location.ref, hit),
            (hit) => hit.score,
            SERVER_SCORE_MAX.file,
            2000,
            query,
            {
              id: "files",
              notIndexed: "Files appear after indexing",
              failed: "Couldn't search files",
            },
          ),
          ...serverCandidates(
            server.symbols,
            (hit) => symbolItem(org, slug, location.ref, hit),
            (hit) => hit.score,
            SERVER_SCORE_MAX.symbol,
            3000,
            query,
            {
              id: "symbols",
              notIndexed: "Symbols appear after indexing",
              failed: "Couldn't search symbols",
            },
          ),
        ]
      : []),
  ];
  const pathIsFile =
    slug !== null && location.path !== null
      ? isFilePath(queryClient, {
          org,
          repo: slug,
          path: location.path,
          ref: location.ref,
          version: repo?.version ?? null,
        })
      : false;
  const context: RankContext = { org, repo: slug, path: location.path, pathIsFile, now };
  // `repoList` is undefined while the list loads: Ask waits for it rather than guess.
  const ask = askItem(org, askTarget(slug, history, repoList, org), query);
  const askRow: Ranked = {
    item: ask,
    match: 0,
    title: [],
    subtitle: [],
    source: "local",
    order: 0,
    score: 0,
  };
  // In a repository that can be searched, the query can open the search page too.
  const search =
    repo && repo.featuresReady && repo.unlocked("keyword_search")
      ? searchItem(org, repo.slug, query)
      : null;
  const searchRows: Ranked[] = search ? [{ ...askRow, item: search }] : [];
  const ranked = [...rank(candidates, context, history, query), ...searchRows, askRow];
  const sections = toSections(ranked, expanded);

  // The selection follows the top result until the user moves it (results arriving later
  // don't pull it out from under them), skipping what the palette mustn't pick by itself.
  const order = selectionOrder(sections);
  const value =
    moved && order.some((row) => row.value === chosen)
      ? chosen
      : (order.find((row) => row.auto)?.value ?? "");

  // All of them, not just the rows each group's cap shows.
  const results = resultCount(sections);
  const searching = server.files.pending || server.symbols.pending;
  const announcement = useDebouncedValue(
    query.trim() === "" || searching
      ? ""
      : results === 0
        ? "No results"
        : `${String(results)} ${results === 1 ? "result" : "results"}`,
    250,
  );

  const pick = (item: ShownItem) => {
    if (item.disabled || item.notice) return;
    remember(item, org, user);
    onClose();
    if (item.href !== undefined) router.push(item.href as Route);
    else item.run?.();
  };

  const onInputKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    // Tab sends what's typed to Ask.
    if (
      event.key === "Tab" &&
      !event.shiftKey &&
      !event.altKey &&
      !event.metaKey &&
      !event.ctrlKey &&
      query.trim() !== "" &&
      !ask.disabled
    ) {
      event.preventDefault();
      pick(ask);
    }
  };

  return (
    <Command
      shouldFilter={false}
      loop
      value={value}
      onValueChange={setChosen}
      className="min-h-0 flex-1 bg-transparent"
      onKeyDown={(event) => {
        if (MOVES.has(event.key) || (event.ctrlKey && CTRL_MOVES.has(event.key))) setMoved(true);
      }}
    >
      <div className="flex items-center gap-1 pr-1">
        <div className="min-w-0 flex-1">
          <PaletteInput
            placeholder="Go to, do, or ask…"
            aria-label="Command"
            value={query}
            onValueChange={(next) => {
              setQuery(next);
              setMoved(false);
            }}
            onKeyDown={onInputKeyDown}
            listId={list.listId}
            activeId={list.activeId}
          />
        </div>
        <DelayedSpinner active={searching} label="Searching" className="px-1 pt-1" />
      </div>
      <CommandList
        ref={listRef}
        label="Results"
        className="max-h-none min-h-0 flex-1 sm:max-h-[min(60vh,32rem)]"
        onPointerMove={() => {
          setMoved(true);
        }}
      >
        {sections.map((section) => (
          <CommandGroup key={section.group} heading={GROUP_HEADINGS[section.group]}>
            {section.rows.map((row) => (
              <Row
                key={row.item.id}
                row={row}
                onSelect={() => {
                  pick(row.item);
                }}
              />
            ))}
            {section.hidden > 0 && (
              <CommandItem
                value={moreValue(section.group)}
                className="text-fg-muted"
                onSelect={() => {
                  // The selection moves to the first row it reveals.
                  const next = ranked.filter(
                    (row) => row.item.group === section.group && !row.item.notice,
                  )[GROUP_CAP]?.item.id;
                  setExpanded(new Set([...expanded, section.group]));
                  if (next) {
                    setChosen(next);
                    setMoved(true);
                  }
                }}
              >
                <ChevronDown aria-hidden className="size-4 text-fg-muted" />
                Show {section.hidden} more
              </CommandItem>
            )}
          </CommandGroup>
        ))}
      </CommandList>
      <div aria-live="polite" role="status" className="sr-only">
        {announcement}
      </div>
    </Command>
  );
}

/** In a repository: its tabs, permalink and reindex need what the repository pages loaded. */
function RepoPalette({ location, onClose }: Omit<PaletteProps, "repo">) {
  const repo = useRepo({ refetchOnMount: false });
  const features = useRepoFeatures({ refetchOnMount: false });
  const { role } = useOrgRole();
  const running = repo.data?.active_job;
  const indexedSha = repo.data?.last_indexed_sha ?? null;
  const context: RepoContext = {
    slug: location.repo ?? "",
    indexedSha,
    // As `useCodeLocation` keys reads made with no ref.
    version: indexedSha ?? (running ? `run:${running.id}:${running.stage ?? "queued"}` : null),
    indexing: features.active !== null,
    admin: roleAtLeast(role, "admin"),
    featuresReady: features.ready,
    unlocked: (feature) => features.state(feature).available,
  };
  return <Palette location={location} repo={context} onClose={onClose} />;
}

/** The palette's content (mounted while it's open). */
export function CommandPalette({ onClose }: { onClose: () => void }) {
  const location = usePaletteLocation();
  return location.repo ? (
    <RepoPalette location={location} onClose={onClose} />
  ) : (
    <Palette location={location} repo={null} onClose={onClose} />
  );
}
