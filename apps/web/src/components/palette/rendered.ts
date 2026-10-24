/**
 * What the code browser has on screen, read from its query cache (the palette sits outside the
 * code browser): the commit a path was rendered from, and whether that path is a file.
 */
import type { QueryClient } from "@tanstack/react-query";

import { ApiError } from "@/lib/api/errors";
import { looksLikeFile } from "@/lib/palette/locations";

interface Shown {
  org: string;
  repo: string;
  path: string;
  /** The `?ref=` in the URL (null: whatever no ref means now). */
  ref: string | null;
  /** What no ref means now, as the code browser keys it (`useCodeLocation`'s `version`); reads
   * made under another ref don't count then. */
  version?: string | null;
}

// See `queryKeys.codeTree` / `queryKeys.codeFile`: ["code", "tree" | "file", org, repo, ref or
// version, path].
function readsOf(
  client: QueryClient,
  kind: "tree" | "file",
  { org, repo, path, ref, version }: Shown,
) {
  return client
    .getQueryCache()
    .findAll({ queryKey: ["code", kind, org, repo] })
    .filter((query) => {
      const key = query.queryKey;
      const at = ref ?? version;
      return key.at(-1) === path && (at === null || at === undefined || key[4] === at);
    });
}

/** The commit (`X-Commit-Sha`) of the listing or file last shown at the path, if any. A file
 * shown as "too large" or "not text" names it in its error's details. */
export function renderedCommit(client: QueryClient, shown: Shown): string | null {
  let best: { at: number; commit: string } | null = null;
  const consider = (commit: unknown, at: number) => {
    if (typeof commit === "string" && commit && (!best || at > best.at)) best = { at, commit };
  };
  for (const kind of ["file", "tree"] as const) {
    for (const query of readsOf(client, kind, shown)) {
      const { data, dataUpdatedAt, error, errorUpdatedAt } = query.state;
      consider((data as { commit?: unknown } | undefined)?.commit, dataUpdatedAt);
      if (error instanceof ApiError) {
        consider((error.details as { commit_sha?: unknown } | null)?.commit_sha, errorUpdatedAt);
      }
    }
  }
  return (best as { commit: string } | null)?.commit ?? null;
}

/** Whether the path open in the code browser is a file: the browser read it as one (or as a
 * directory); before it has, its name decides. */
export function isFilePath(client: QueryClient, shown: Shown): boolean {
  if (readsOf(client, "file", shown).some((query) => query.state.data !== undefined)) return true;
  if (readsOf(client, "tree", shown).some((query) => query.state.data !== undefined)) return false;
  return shown.path !== "" && looksLikeFile(shown.path);
}
