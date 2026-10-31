/**
 * Refs and staleness (5.6): the branches and pull requests a repository's pages can be read at
 * (`GET …/refs`, the ref switcher's list), and whether what an answer cited is still as it was
 * (`GET …/staleness`, its stale badges).
 */
import type { Schemas } from "./types";

export type RepoRef = Schemas["RefOut"];
export type RefStatus = RepoRef["status"];
export type Staleness = Schemas["StalenessOut"];
export type StaleReason = Schemas["CitedOut"]["reason"];

/** A whole commit id: SHA-1 (40 hex) or SHA-256 (64 hex). */
export const isCommit = (ref: string) => /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(ref);

/** What the switcher's button says the page is read at: the branch, the pull request (`#12`), or
 * a pinned commit's first 7 characters. */
export function refLabel(ref: string | null, defaultBranch: string): string {
  if (ref === null) return defaultBranch;
  if (isCommit(ref)) return ref.slice(0, 7);
  const pull = /^pull\/(\d+)$/.exec(ref);
  return pull ? `#${pull[1] ?? ""}` : ref;
}

/** Whether a ref can be read at: it has an index (a failed or running run keeps the last one). */
export const readable = (ref: RepoRef) => ref.sha !== null;

/** What a ref's index is, in a few words. */
export function refStatusLabel(ref: RepoRef): string {
  switch (ref.status) {
    case "indexed":
      return ref.sha ? `Indexed at ${ref.sha.slice(0, 7)}` : "Indexed";
    case "indexing":
      return ref.sha ? `Updating from ${ref.sha.slice(0, 7)}` : "Indexing…";
    case "failed":
      return ref.sha ? `Last run failed · ${ref.sha.slice(0, 7)} still shown` : "Indexing failed";
    default:
      return "Not indexed";
  }
}

/** The ref to put in the URL for one picked: none for the default branch (what no ref means). */
export const refParam = (ref: RepoRef) => (ref.default ? null : ref.name);

/** Most citations the staleness check takes at once (the API's limit). */
export const MAX_CITED = 200;

/** A citation as the staleness check names it: `<sha>:<path>`. */
export const citedKey = (sha: string, path: string) => `${sha.toLowerCase()}:${path}`;

/** The distinct citations to check, in a stable order (they key the query). */
export function citedKeys(citations: readonly { sha: string; path: string }[]): string[] {
  const keys = new Set(citations.map((c) => citedKey(c.sha, c.path)));
  return [...keys].sort().slice(0, MAX_CITED);
}

/** Each checked citation's reason, by its key. */
export function reasons(result: Staleness | undefined): ReadonlyMap<string, StaleReason> {
  return new Map((result?.citations ?? []).map((c) => [citedKey(c.sha, c.path), c.reason]));
}

export const isStale = (reason: StaleReason | undefined) =>
  reason === "changed" || reason === "deleted";

/** The files of these citations that changed or went since (each once, in order). */
export function staleFiles(
  citations: readonly { sha: string; path: string }[],
  checked: ReadonlyMap<string, StaleReason>,
): string[] {
  const out: string[] = [];
  for (const c of citations) {
    if (isStale(checked.get(citedKey(c.sha, c.path))) && !out.includes(c.path)) out.push(c.path);
  }
  return out;
}

/** The stale badge's words: "1 cited file changed since" / "3 cited files changed since". */
export function staleLabel(files: readonly string[]): string {
  const n = files.length;
  return `${String(n)} cited file${n === 1 ? "" : "s"} changed since`;
}
