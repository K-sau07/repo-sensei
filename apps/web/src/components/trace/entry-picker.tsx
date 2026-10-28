"use client";

/**
 * Where a trace starts, when the URL doesn't say (`/trace` without `?from=`): the repository's
 * symbols by name (the ⌘K palette's lookup), or whatever is typed (a route such as
 * `POST /api/v1/items/`, a file, a name), which the API resolves.
 */
import { useQuery } from "@tanstack/react-query";
import { Route as RouteIcon, Search } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";

import { useCodeLocation } from "@/components/code/use-code";
import { SYMBOLS_MIN_LENGTH } from "@/components/palette/use-server-results";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { api } from "@/lib/api/client";
import type { SymbolHit, SymbolKind } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";
import { traceHref } from "@/lib/trace/url-state";

const DEBOUNCE_MS = 150;
const LIMIT = 20;
// What a feature starts from (a module or a variable isn't somewhere a request begins).
const ENTRY_KINDS: ReadonlySet<SymbolKind> = new Set([
  "component",
  "hook",
  "function",
  "method",
  "class",
]);

export function EntryPicker() {
  const { org, repo, ref, ready } = useCodeLocation();
  const router = useRouter();
  const [typed, setTyped] = useState("");
  const q = useDebouncedValue(typed.trim(), DEBOUNCE_MS);
  const hintId = useId();
  const symbols = useQuery({
    queryKey: queryKeys.traceEntries(org, repo, ref, q),
    queryFn: async ({ signal }): Promise<SymbolHit[]> => {
      const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/symbols", {
        params: { path: { org, repo }, query: { q, limit: LIMIT, ...(ref ? { ref } : {}) } },
        signal,
      });
      return (data?.results ?? []).filter((hit) => ENTRY_KINDS.has(hit.kind));
    },
    enabled: ready && q.length >= SYMBOLS_MIN_LENGTH,
    placeholderData: (previous) => previous,
    retry: false,
  });
  const hits = q.length >= SYMBOLS_MIN_LENGTH ? (symbols.data ?? []) : [];

  return (
    <section
      aria-labelledby={`${hintId}-title`}
      className="mx-auto flex w-full max-w-2xl flex-col gap-4 p-8"
    >
      <div className="flex flex-col gap-1">
        <h2 id={`${hintId}-title`} className="flex items-center gap-2 text-lg font-semibold">
          <RouteIcon aria-hidden className="size-5 text-fg-muted" />
          Trace a feature
        </h2>
        <p id={hintId} className="text-sm text-fg-muted">
          From where it starts (a component, a hook, a route, a function) across the layers to the
          tables it reads and writes, each hop with the code that proves it.
        </p>
      </div>
      <form
        role="search"
        onSubmit={(event) => {
          event.preventDefault();
          const entry = typed.trim();
          if (entry) router.push(traceHref(org, repo, { from: entry }));
        }}
      >
        <div className="relative">
          <Search
            aria-hidden
            className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-faint"
          />
          <Input
            aria-label="Where the trace starts"
            aria-describedby={hintId}
            placeholder="A name, a route (POST /api/v1/items/) or a file"
            value={typed}
            onChange={(event) => {
              setTyped(event.target.value);
            }}
            className="pl-8"
          />
        </div>
      </form>
      {q.length >= SYMBOLS_MIN_LENGTH && (
        <div aria-live="polite">
          {symbols.isPending ? (
            <div role="status" aria-label="Looking up symbols" className="flex flex-col gap-2">
              <Skeleton className="h-8" />
              <Skeleton className="h-8" />
            </div>
          ) : hits.length === 0 ? (
            <p className="text-sm text-fg-muted">
              No component, hook or function by that name. Press Enter to trace it as typed.
            </p>
          ) : (
            <ul aria-label="Symbols to trace from" className="flex flex-col">
              {hits.map((hit) => (
                <li key={hit.symbol_key}>
                  <Link
                    href={traceHref(org, repo, { from: hit.symbol_key })}
                    className="flex items-baseline gap-2 rounded-sm px-2 py-1.5 text-sm outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    <span className="shrink-0 font-mono text-fg">{hit.name}</span>
                    <span className="shrink-0 text-xs text-fg-muted">{hit.kind}</span>
                    <span className="min-w-0 truncate font-mono text-xs text-fg-faint">
                      {hit.path}:{hit.start_line}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
