"use client";

/**
 * `/[org]/[repo]/search`: one query, its results in layer lanes (UI | API | Service | DB |
 * Tests/Config), how it was read, and the exact queries behind it. Everything the page shows is
 * in its URL (`lib/search/url-state.ts`), so Back, Forward and a shared link restore it.
 */
import { Clock, Hourglass, Lock, SearchX } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { PartialBanner } from "@/components/feedback/banners";
import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { formatEta, LockedState } from "@/components/feedback/locked-state";
import { askHref } from "@/components/palette/items";
import { useRepoFeatures, useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { ApiError } from "@/lib/api/errors";
import { LAYERS, type Layer } from "@/lib/api/search";
import { STAGE_LABELS } from "@/lib/indexing";
import { repoPath } from "@/lib/routes";
import { searchPath, type SearchState } from "@/lib/search/url-state";

import { ExactResults } from "./exact-results";
import { plural } from "./meta";
import { FiltersPopover, PivotToExact, QueryClassChip, SearchInput } from "./query-bar";
import { Swimlanes, SwimlanesSkeleton } from "./swimlanes";
import { useResultKeys } from "./use-result-keys";
import { measureResultsPainted, useGrep, useSearch, useSearchState } from "./use-search";

const EXAMPLES: { q: string; what: string }[] = [
  { q: "create_user", what: "a name" },
  { q: "how are passwords hashed", what: "a question" },
  { q: '"SECRET_KEY"', what: "exact text" },
  { q: "def \\w+_token", what: "a regex" },
];

function Frame({ children }: { children: ReactNode }) {
  return <div className="mx-auto flex w-full max-w-3xl flex-col p-8">{children}</div>;
}

/** Not searchable yet: locked while a run brings it, otherwise waiting for an index. */
function NotSearchable({ title }: { title?: string }) {
  const { org, repo } = useRepoSlug();
  const { state, active } = useRepoFeatures();
  const keyword = state("keyword_search");
  const stage = keyword.stage ? STAGE_LABELS[keyword.stage] : "indexing";
  if (active && keyword.coming) {
    return (
      <Frame>
        <LockedState
          feature="Search"
          stage={stage}
          {...(keyword.etaSeconds !== null ? { etaSeconds: keyword.etaSeconds } : {})}
          href={repoPath(org, repo)}
        />
      </Frame>
    );
  }
  return (
    <Frame>
      <EmptyState
        icon={<Lock />}
        title={title ?? "Search appears after indexing"}
        description={`It unlocks when ${stage} has run on this repository.`}
        action={
          <Link
            href={repoPath(org, repo, "index")}
            className="text-sm underline underline-offset-4"
          >
            Indexing
          </Link>
        }
      />
    </Frame>
  );
}

/** 429: when to try again, counted down; Retry waits for it. */
function RateLimited({ seconds, onRetry }: { seconds: number | null; onRetry: () => void }) {
  const [left, setLeft] = useState(seconds ?? 0);
  useEffect(() => {
    if (left <= 0) return;
    const timer = setTimeout(() => {
      setLeft((value) => value - 1);
    }, 1000);
    return () => {
      clearTimeout(timer);
    };
  }, [left]);
  return (
    <section
      role="alert"
      className="flex flex-col items-start gap-3 rounded-xl border border-warn/30 bg-warn/8 p-6"
    >
      <div className="flex items-center gap-2 text-warn">
        <Clock aria-hidden className="size-4" />
        <h3 className="text-sm font-semibold">Too many searches for now</h3>
      </div>
      <p className="text-sm text-fg-muted">
        {left > 0 ? (
          <>
            You can search again in <span className="tabular-nums">{left}</span>{" "}
            {left === 1 ? "second" : "seconds"}.
          </>
        ) : (
          "You can search again now."
        )}
      </p>
      <Button variant="outline" size="sm" disabled={left > 0} onClick={onRetry}>
        Try again
      </Button>
    </section>
  );
}

function SearchFailed({ error, onRetry }: { error: Error; onRetry: () => void }) {
  const api = error instanceof ApiError ? error : null;
  if (api?.status === 429) return <RateLimited seconds={api.retryAfter} onRetry={onRetry} />;
  if (api?.status === 422) {
    return (
      <EmptyState icon={<SearchX />} title="That query can't be run" description={api.message} />
    );
  }
  return (
    <ErrorState
      title="Couldn't search"
      description={api?.message ?? "The search didn't finish. Trying again usually works."}
      onRetry={onRetry}
      {...(api?.requestId ? { requestId: api.requestId } : {})}
    />
  );
}

/** Nothing typed yet: what kinds of query work. */
function Landing({ org, repo }: { org: string; repo: string }) {
  return (
    <section aria-label="Ways to search" className="flex flex-col gap-3 py-6">
      <p className="text-sm text-fg-muted">
        Search by name, by meaning, or exactly. Results come in lanes by layer, from the UI to the
        data.
      </p>
      <ul className="flex flex-wrap gap-2">
        {EXAMPLES.map((example) => (
          <li key={example.q}>
            <Link
              href={searchPath(org, repo, { q: example.q })}
              className="inline-flex h-8 items-center gap-2 rounded-sm border border-line bg-surface-1 px-2.5 text-sm hover:border-line-strong hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              <code className="font-mono">{example.q}</code>
              <span className="text-xs text-fg-muted">{example.what}</span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** No results: ways to widen the search, or to ask instead. */
function NoResults({ org, repo, state }: { org: string; repo: string; state: SearchState }) {
  const filtered = state.layer.length > 0 || state.lang.length > 0 || state.path.trim() !== "";
  return (
    <EmptyState
      icon={<SearchX />}
      title="No results"
      description={
        <>
          Nothing matched <q className="font-mono">{state.q.trim()}</q>
          {filtered ? " with these filters" : ""}. Check the spelling, try fewer or other words, or
          ask a question instead.
        </>
      }
      action={
        <div className="flex flex-wrap items-center justify-center gap-3 text-sm">
          {filtered && (
            <Link
              href={searchPath(org, repo, { q: state.q, mode: state.mode, ref: state.ref })}
              className="underline underline-offset-4"
            >
              Search every layer and path
            </Link>
          )}
          {state.mode && (
            <Link
              href={searchPath(org, repo, { ...state, mode: null })}
              className="underline underline-offset-4"
            >
              Search by meaning too
            </Link>
          )}
          <Link
            href={askHref(org, repo, state.q) as Route}
            className="underline underline-offset-4"
          >
            Ask instead
          </Link>
        </div>
      }
    />
  );
}

/** Semantic search isn't ready: what's shown and why. */
function SemanticPending() {
  const { state } = useRepoFeatures();
  const semantic = state("semantic_search");
  const eta =
    semantic.coming && semantic.etaSeconds !== null
      ? ` Semantic search is available in ~${formatEta(semantic.etaSeconds)}.`
      : "";
  return (
    <PartialBanner>
      <strong className="font-medium">Keyword and symbol matches only.</strong> This commit&apos;s
      embeddings aren&apos;t ready yet, so nothing is matched by meaning.{eta}
    </PartialBanner>
  );
}

/** Announced politely when results arrive (not on every keystroke). */
function Announcer({ message }: { message: string }) {
  const settled = useDebouncedValue(message, 250);
  return (
    <p role="status" aria-live="polite" className="sr-only">
      {settled}
    </p>
  );
}

export function SearchPage() {
  const { org, repo } = useRepoSlug();
  const features = useRepoFeatures();
  const [state, setState] = useSearchState();
  const keyword = features.state("keyword_search");
  const searchable = features.ready && keyword.available;
  const search = useSearch(state, searchable);
  const grep = useGrep(state, searchable);
  const input = useRef<HTMLInputElement>(null);
  const results = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState<Layer | null>(null);
  const keys = useResultKeys({
    root: results,
    input,
    onLane: (lane) => {
      if ((LAYERS as readonly string[]).includes(lane)) setVisible(lane as Layer);
    },
    enabled: searchable,
  });

  const exact = state.mode === "regex";
  const active = exact ? grep : search;
  const data = search.data;
  const shownData = exact ? grep.data : data;
  // The query the results on screen are for (they stay while the next one runs).
  const stale = active.isPlaceholderData || (active.isFetching && shownData !== undefined);

  useEffect(() => {
    if (shownData && !stale) return measureResultsPainted();
  }, [shownData, stale]);

  if (features.failed) {
    return (
      <Frame>
        <ErrorState
          title="Couldn't check whether search is ready"
          description="This repository's index status didn't load."
          onRetry={features.retry}
        />
      </Frame>
    );
  }
  if (!features.ready) {
    return (
      <div role="status" aria-label="Loading" className="@container flex flex-col gap-3 px-6 py-4">
        <Skeleton className="h-9 w-full max-w-2xl" />
        <SwimlanesSkeleton />
      </div>
    );
  }
  if (!keyword.available) return <NotSearchable />;

  const error = active.error instanceof ApiError ? active.error : null;
  if (error?.code === "repo_not_indexed") {
    return (
      <Frame>
        <EmptyState
          icon={<Hourglass />}
          title="Not indexed yet"
          description="Search opens once this repository has been indexed."
          action={
            <Link
              href={repoPath(org, repo, "index")}
              className="text-sm underline underline-offset-4"
            >
              Indexing
            </Link>
          }
        />
      </Frame>
    );
  }
  if (error?.code === "not_indexed") return <NotSearchable />;

  const q = state.q.trim();
  const target = { org, repo, ref: state.ref };
  const total = data ? LAYERS.reduce((sum, layer) => sum + data.counts[layer], 0) : 0;
  const lanes = data ? LAYERS.filter((layer) => data.counts[layer] > 0).length : 0;
  let announcement = "";
  if (q && !stale && exact && grep.data) {
    announcement =
      grep.data.hits.length === 0
        ? "No results"
        : plural(grep.data.hits.length, "exact match", "exact matches");
  } else if (q && !stale && data) {
    announcement =
      total === 0 ? "No results" : `${plural(total, "result")} in ${plural(lanes, "lane")}`;
  }
  const languages = [
    ...new Set(
      LAYERS.flatMap((layer) => data?.lanes[layer].map((hit) => hit.language) ?? []).filter(
        (language): language is string => language !== null,
      ),
    ),
  ];

  let body: ReactNode;
  if (!q) body = <Landing org={org} repo={repo} />;
  else if (active.isError && !shownData) {
    body = <SearchFailed error={active.error} onRetry={() => void active.refetch()} />;
  } else if (exact) {
    body = grep.data ? (
      grep.data.hits.length === 0 ? (
        <NoResults org={org} repo={repo} state={state} />
      ) : (
        <ExactResults result={grep.data} target={target} stale={stale} />
      )
    ) : (
      <SwimlanesSkeleton />
    );
  } else if (!data) body = <SwimlanesSkeleton />;
  else if (total === 0) body = <NoResults org={org} repo={repo} state={state} />;
  else {
    body = (
      <Swimlanes
        result={data}
        target={target}
        layers={state.layer}
        visible={visible && data.counts[visible] > 0 ? visible : null}
        onVisible={setVisible}
        stale={stale}
      />
    );
  }

  return (
    <div data-search-page="" className="@container flex min-h-0 flex-1 flex-col gap-3 px-6 py-4">
      <h2 className="sr-only">Search</h2>
      <div role="search" className="flex items-center gap-2">
        <SearchInput
          value={state.q}
          inputRef={input}
          onSearch={(next, history) => void setState({ q: next }, { history })}
          onDown={keys.enter}
        />
        <FiltersPopover state={state} setState={setState} languages={languages} />
      </div>
      {q && (
        // How the query was read and the exact queries behind it: one row, its height kept
        // while they load, so the lanes below don't move when they come.
        <div className="flex min-h-7 flex-wrap items-center gap-x-3 gap-y-1.5">
          {exact || data ? (
            <QueryClassChip
              interpretation={exact ? null : (data?.interpretation ?? null)}
              exact={state.mode}
            />
          ) : (
            <Skeleton className="h-7 w-24" />
          )}
          <PivotToExact
            org={org}
            repo={repo}
            state={state}
            queries={!exact && data ? data.exact_queries : []}
          />
        </div>
      )}
      {q && !exact && data?.partial.semantic_pending && <SemanticPending />}
      {active.isError && shownData && (
        <SearchFailed error={active.error} onRetry={() => void active.refetch()} />
      )}
      <div ref={results} {...keys.containerProps} className="min-h-0 flex-1">
        {body}
      </div>
      <Announcer message={announcement} />
    </div>
  );
}
