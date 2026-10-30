"use client";

import { useQueries, useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { parseAsString, useQueryStates } from "nuqs";
import { useCallback } from "react";

import { useRepo, useRepoSlug } from "@/components/repo/use-repo";
import { api } from "@/lib/api/client";
import type { FileContent, Repo, TreeListing } from "@/lib/api/types";
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

export interface Snapshot {
  org: string;
  repo: string;
  ref: string | null;
  /** What no `ref` means right now (see `useCodeLocation`): it keys the reads, so they are made
   * again when it changes. */
  version: string | null;
}

/** While a read is made again for a new version, the same path keeps showing what it had: the
 * page doesn't blank (or lose its scroll and search) for a commit that is usually the same. A
 * different path never shows another's content. */
const samePath = (path: string, key: readonly unknown[] | undefined) => key?.at(-1) === path;

export function treeOptions({ org, repo, ref, version }: Snapshot, path: string) {
  return {
    queryKey: queryKeys.codeTree(org, repo, ref, version, path),
    queryFn: async ({ signal }: { signal: AbortSignal }): Promise<Listing> => {
      const { data, response } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/tree", {
        params: { path: { org, repo }, query: { path, ...(ref ? { ref } : {}) } },
        signal,
      });
      if (!data) throw new Error("empty listing");
      return { ...data, commit: commitOf(response) };
    },
    staleTime: STALE_MS,
    placeholderData: (previous: Listing | undefined, query?: { queryKey: readonly unknown[] }) =>
      samePath(path, query?.queryKey) ? previous : undefined,
  };
}

/** What no `ref` means for a repository right now, which keys the reads (see `useCodeLocation`). */
export function snapshotVersion(data: Repo | undefined): string | null {
  const running = data?.active_job;
  return (
    data?.last_indexed_sha ?? (running ? `run:${running.id}:${running.stage ?? "queued"}` : null)
  );
}

/** A path segment from the URL; a malformed escape ("%zz") is kept as typed. */
export function decodeSegment(segment: string): string {
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
  // Reads wait for the repository, which says what no `ref` means: its last index, or, before
  // the first one, the first run (from its discovery on, at each stage, so what was "not
  // indexed yet" is read again once discovery is done). A new index reads afresh.
  const repository = useRepo();
  const version = snapshotVersion(repository.data);
  return {
    org,
    repo,
    path,
    ref,
    lines,
    version,
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
        // Being tried again after an error: loading, not the error still.
        else if (result.isFetching) states.set(path, { status: "loading" });
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

/** A file's read, shared by the viewer and the previews that lead to it (search results), so
 * opening a previewed file needs no second read. */
export function fileOptions({ org, repo, ref, version }: Snapshot, path: string) {
  return {
    queryKey: queryKeys.codeFile(org, repo, ref, version, path),
    queryFn: async ({ signal }: { signal: AbortSignal }): Promise<OpenedFile> => {
      const { data, response } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/file", {
        params: { path: { org, repo }, query: { path, ...(ref ? { ref } : {}) } },
        signal,
      });
      if (!data) throw new Error("empty file");
      performance.mark(FILE_RECEIVED_MARK);
      return { ...data, commit: commitOf(response) };
    },
    staleTime: STALE_MS,
    placeholderData: (
      previous: OpenedFile | undefined,
      query?: { queryKey: readonly unknown[] },
    ) => (samePath(path, query?.queryKey) ? previous : undefined),
  };
}

export function useFile(path: string, enabled = true) {
  const location = useCodeLocation();
  return useQuery({ ...fileOptions(location, path), enabled: enabled && location.ready });
}

/** A file at a commit (an answer's citation: the code it cites, whatever the page is on). */
export function useFileAt(path: string, sha: string, enabled = true) {
  const location = useCodeLocation();
  return useQuery({
    ...fileOptions({ ...location, ref: sha }, path),
    enabled: enabled && location.ready && sha !== "",
  });
}
