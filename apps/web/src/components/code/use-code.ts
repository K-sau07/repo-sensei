"use client";

import { useQueries, useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { parseAsString, useQueryStates } from "nuqs";
import { useCallback } from "react";

import { useRepo, useRepoSlug } from "@/components/repo/use-repo";
import { api } from "@/lib/api/client";
import type { FileContent, TreeListing } from "@/lib/api/types";
import { parseAsLines } from "@/lib/code/lines";
import type { DirState } from "@/lib/code/tree";
import { queryKeys } from "@/lib/query/keys";

/** The commit a listing or file was read from (`X-Commit-Sha`, which a 304 keeps current). */
const commitOf = (response: Response) => response.headers.get("X-Commit-Sha");

export type Listing = TreeListing & { commit: string | null };
export type OpenedFile = FileContent & { commit: string | null };

/** Marks when a file's content arrived, for the paint budget (see `CodeViewer`). */
export const FILE_RECEIVED_MARK = "code-file:received";

// Listings and files never change for a commit, and the API revalidates them cheaply (ETag), so
// a minute keeps browsing back and forth free.
const STALE_MS = 60_000;

interface Snapshot {
  org: string;
  repo: string;
  ref: string | null;
  /** The last index's commit (null until there is one). */
  indexed: string | null;
}

export function treeOptions({ org, repo, ref, indexed }: Snapshot, path: string) {
  return {
    queryKey: queryKeys.codeTree(org, repo, ref, indexed, path),
    queryFn: async ({ signal }: { signal: AbortSignal }): Promise<Listing> => {
      const { data, response } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/tree", {
        params: { path: { org, repo }, query: { path, ...(ref ? { ref } : {}) } },
        signal,
      });
      if (!data) throw new Error("empty listing");
      return { ...data, commit: commitOf(response) };
    },
    staleTime: STALE_MS,
  };
}

/** A path segment from the URL; a malformed escape ("%zz") is kept as typed. */
function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/** The code browser's place in the URL: `/code/<path>` plus `?ref=` (a pinned commit, as
 * permalinks have) and `?L=` (the lines picked). */
export function useCodeLocation() {
  const { org, repo } = useRepoSlug();
  const { path: segments } = useParams<{ path?: string[] }>();
  const [{ ref, L: lines }, setQuery] = useQueryStates(
    { ref: parseAsString, L: parseAsLines },
    // Each range picked is a history entry, so Back returns to the one before.
    { history: "push", scroll: false },
  );
  // Next hands over each segment still percent-encoded.
  const path = (segments ?? []).map(decodeSegment).join("/");
  // Reads wait for the repository, whose last indexed commit keys them: when a run finishes,
  // what was "not indexed yet" (or an older index) is read again.
  const repository = useRepo();
  return {
    org,
    repo,
    path,
    ref,
    lines,
    indexed: repository.data?.last_indexed_sha ?? null,
    ready: repository.isSuccess,
    setLines: (next: typeof lines) => setQuery({ L: next }),
  };
}

/** One directory's listing. */
export function useListing(path: string, enabled = true) {
  const location = useCodeLocation();
  return useQuery({ ...treeOptions(location, path), enabled: enabled && location.ready });
}

export interface Listings {
  /** Each directory asked for: its entries once loaded. */
  states: ReadonlyMap<string, DirState>;
  errors: ReadonlyMap<string, Error>;
  refetch: (path: string) => void;
}

/** Several listings at once (every expanded directory of the tree), by path. The result only
 * changes when one of them does, so the rows built from it can be memoized. */
export function useListings(paths: readonly string[]): Listings {
  const location = useCodeLocation();
  const combine = useCallback(
    (results: UseQueryResult<Listing>[]): Listings => {
      const states = new Map<string, DirState>();
      const errors = new Map<string, Error>();
      const refetchers = new Map<string, () => unknown>();
      paths.forEach((path, index) => {
        const result = results[index];
        if (!result) return;
        refetchers.set(path, result.refetch);
        if (result.data) states.set(path, { status: "loaded", entries: result.data.entries });
        else if (result.error) {
          states.set(path, { status: "error" });
          errors.set(path, result.error);
        } else states.set(path, { status: "loading" });
      });
      return {
        states,
        errors,
        refetch: (path) => {
          void refetchers.get(path)?.();
        },
      };
    },
    [paths],
  );
  return useQueries({
    queries: paths.map((path) => ({ ...treeOptions(location, path), enabled: location.ready })),
    combine,
  });
}

export function useFile(path: string, enabled = true) {
  const { org, repo, ref, indexed, ready } = useCodeLocation();
  return useQuery({
    queryKey: queryKeys.codeFile(org, repo, ref, indexed, path),
    queryFn: async ({ signal }): Promise<OpenedFile> => {
      const { data, response } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/file", {
        params: { path: { org, repo }, query: { path, ...(ref ? { ref } : {}) } },
        signal,
      });
      if (!data) throw new Error("empty file");
      performance.mark(FILE_RECEIVED_MARK);
      return { ...data, commit: commitOf(response) };
    },
    staleTime: STALE_MS,
    enabled: enabled && ready,
  });
}
