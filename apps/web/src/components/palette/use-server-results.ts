"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { api } from "@/lib/api/client";
import { symbolsApi, type SymbolHit } from "@/lib/api/symbols";
import type { FileSearch } from "@/lib/api/types";

/** Server searches wait for typing to pause this long. */
export const SERVER_DEBOUNCE_MS = 120;
/** Shortest query each searches for (a single letter finds too many symbols to be useful). */
export const FILES_MIN_LENGTH = 1;
export const SYMBOLS_MIN_LENGTH = 2;
const FILES_LIMIT = 20;
const SYMBOLS_LIMIT = 20;

type Kind = "files" | "symbols";

export const paletteKeys = {
  all: ["palette"] as const,
  search: (kind: Kind, org: string, repo: string, ref: string | null, q: string) =>
    ["palette", kind, org, repo, ref, q] as const,
};

/**
 * Keep the last results while a query is fetched only if it extends the last one ("logi" after
 * "log"): they are still a good guess. Anything else (a deletion, another repository) starts
 * empty rather than showing results for something that wasn't asked.
 */
export function keepWhileExtending(
  key: readonly unknown[],
  previousKey: readonly unknown[] | undefined,
): boolean {
  if (previousKey?.length !== key.length) return false;
  for (let index = 0; index < key.length - 1; index += 1) {
    if (previousKey[index] !== key[index]) return false;
  }
  const q = key.at(-1);
  const previous = previousKey.at(-1);
  return typeof q === "string" && typeof previous === "string" && q.startsWith(previous);
}

/** The query's characters appear in order in `text` (what both searches require). */
export function inOrder(query: string, text: string): boolean {
  const needle = query.toLowerCase().replace(/\s+/g, "");
  const haystack = text.toLowerCase();
  let at = 0;
  for (const char of haystack) if (char === needle[at]) at += 1;
  return at >= needle.length;
}

export interface ServerState<T> {
  hits: T[];
  error: unknown;
  /** Asked for and not answered yet. */
  pending: boolean;
}

interface Scope {
  org: string;
  repo: string | null;
  ref: string | null;
}

/**
 * Files and symbols for what's typed, in the current repository only: debounced, the request in
 * flight aborted (through the query's `signal`) on each keystroke, one failing without the other.
 */
export function useServerResults({ org, repo, ref }: Scope, query: string) {
  const typed = query.trim();
  const q = useDebouncedValue(typed, SERVER_DEBOUNCE_MS);
  const queryClient = useQueryClient();

  // A keystroke makes the answer on its way stale: abort it now rather than wait for it.
  useEffect(() => {
    if (typed !== q) void queryClient.cancelQueries({ queryKey: paletteKeys.all });
  }, [typed, q, queryClient]);

  const filesEnabled = repo !== null && q.length >= FILES_MIN_LENGTH;
  const symbolsEnabled = repo !== null && q.length >= SYMBOLS_MIN_LENGTH;

  const fileKey = paletteKeys.search("files", org, repo ?? "", ref, q);
  const files = useQuery({
    queryKey: fileKey,
    queryFn: async ({ signal }): Promise<FileSearch["results"]> => {
      const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/files/search", {
        params: {
          path: { org, repo: repo ?? "" },
          query: { q, limit: FILES_LIMIT, ...(ref ? { ref } : {}) },
        },
        signal,
      });
      return data?.results ?? [];
    },
    enabled: filesEnabled,
    placeholderData: (previous, previousQuery) =>
      keepWhileExtending(fileKey, previousQuery?.queryKey) ? previous : undefined,
    retry: false,
  });

  const symbolKey = paletteKeys.search("symbols", org, repo ?? "", ref, q);
  const symbols = useQuery({
    queryKey: symbolKey,
    queryFn: async ({ signal }): Promise<SymbolHit[]> => {
      const { data } = await symbolsApi.GET("/api/v1/orgs/{org}/repos/{repo}/symbols", {
        params: {
          path: { org, repo: repo ?? "" },
          query: { q, limit: SYMBOLS_LIMIT, ...(ref ? { ref } : {}) },
        },
        signal,
      });
      return data?.results ?? [];
    },
    enabled: symbolsEnabled,
    placeholderData: (previous, previousQuery) =>
      keepWhileExtending(symbolKey, previousQuery?.queryKey) ? previous : undefined,
    retry: false,
  });

  // Typed back to what was being fetched when a keystroke aborted it: fetch it again.
  // (No answer of its own, only a placeholder or nothing, and nothing in flight.)
  const filesAborted =
    filesEnabled && (files.isPending || files.isPlaceholderData) && files.fetchStatus === "idle";
  const symbolsAborted =
    symbolsEnabled &&
    (symbols.isPending || symbols.isPlaceholderData) &&
    symbols.fetchStatus === "idle";
  const { refetch: refetchFiles } = files;
  const { refetch: refetchSymbols } = symbols;
  useEffect(() => {
    if (typed !== q) return;
    if (filesAborted) void refetchFiles();
    if (symbolsAborted) void refetchSymbols();
  }, [typed, q, filesAborted, symbolsAborted, refetchFiles, refetchSymbols]);

  // Results for another query (kept while typing on) only show what still matches.
  const fileState: ServerState<FileSearch["results"][number]> = {
    hits:
      typed.length >= FILES_MIN_LENGTH
        ? (files.data ?? []).filter(
            (hit) => (!files.isPlaceholderData && q === typed) || inOrder(typed, hit.path),
          )
        : [],
    error: typed.length >= FILES_MIN_LENGTH ? files.error : null,
    pending: typed.length >= FILES_MIN_LENGTH && (typed !== q || files.isFetching),
  };
  const symbolState: ServerState<SymbolHit> = {
    hits:
      typed.length >= SYMBOLS_MIN_LENGTH
        ? (symbols.data ?? []).filter(
            (hit) => (!symbols.isPlaceholderData && q === typed) || inOrder(typed, hit.name),
          )
        : [],
    error: typed.length >= SYMBOLS_MIN_LENGTH ? symbols.error : null,
    pending: typed.length >= SYMBOLS_MIN_LENGTH && (typed !== q || symbols.isFetching),
  };
  return { files: fileState, symbols: symbolState };
}
