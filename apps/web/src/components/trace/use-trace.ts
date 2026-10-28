"use client";

/**
 * The trace view's reads (3.15): a trace from its entry, read as the files are (at the snapshot
 * `useCodeLocation` says), and the trace page's place in its URL.
 */
import { useQuery } from "@tanstack/react-query";
import { useQueryStates } from "nuqs";

import { useCodeLocation, type Snapshot } from "@/components/code/use-code";
import { api } from "@/lib/api/client";
import type { Trace } from "@/lib/api/trace";
import { queryKeys } from "@/lib/query/keys";
import { traceParsers } from "@/lib/trace/url-state";

// A snapshot's graph never changes, so neither does a trace through it.
const STALE_MS = 5 * 60_000;

export function traceOptions(snapshot: Snapshot, entry: string) {
  const { org, repo, ref, version } = snapshot;
  return {
    queryKey: queryKeys.trace(org, repo, ref, version, entry),
    queryFn: async ({ signal }: { signal: AbortSignal }): Promise<Trace> => {
      const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/trace", {
        params: { path: { org, repo }, query: { from: entry, ...(ref ? { ref } : {}) } },
        signal,
      });
      if (!data) throw new Error("no trace");
      // openapi-fetch reads the schema's `[start, end]` pairs as plain arrays; they are pairs.
      return data as Trace;
    },
    staleTime: STALE_MS,
  };
}

/** The trace from `entry` (none: nothing to read). */
export function useTrace(entry: string | null) {
  const location = useCodeLocation();
  return useQuery({
    ...traceOptions(location, entry ?? ""),
    enabled: entry !== null && entry !== "" && location.ready,
  });
}

/** Where the trace page is: the entry, the path tab, the hop picked, the view. A new entry is a
 * history entry (Back returns to the last trace); the rest replace it. */
export function useTraceState() {
  return useQueryStates(traceParsers, { history: "replace", scroll: false });
}
