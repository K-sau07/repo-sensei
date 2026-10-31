/**
 * Query keys, built by the same function the hooks use, so invalidation always matches.
 * Example: `queryClient.invalidateQueries({ queryKey: queryKeys.members("acme") })`.
 */
import { $api } from "@/lib/api/client";

const org = (slug: string) => ({ params: { path: { org: slug } } });

export const queryKeys = {
  me: () => $api.queryOptions("get", "/api/v1/me").queryKey,
  mySessions: () => $api.queryOptions("get", "/api/v1/me/sessions").queryKey,
  orgs: () => $api.queryOptions("get", "/api/v1/orgs").queryKey,
  org: (slug: string) => $api.queryOptions("get", "/api/v1/orgs/{org}", org(slug)).queryKey,
  members: (slug: string) =>
    $api.queryOptions("get", "/api/v1/orgs/{org}/members", org(slug)).queryKey,
  invitations: (slug: string) =>
    $api.queryOptions("get", "/api/v1/orgs/{org}/invitations", org(slug)).queryKey,
  repos: (slug: string) => $api.queryOptions("get", "/api/v1/orgs/{org}/repos", org(slug)).queryKey,
  repo: (slug: string, repo: string) =>
    $api.queryOptions("get", "/api/v1/orgs/{org}/repos/{repo}", {
      params: { path: { org: slug, repo } },
    }).queryKey,
  /** Every page and search of the "connect a repository" picker. */
  availableRepos: (slug: string) =>
    $api.queryOptions("get", "/api/v1/orgs/{org}/github/repos", org(slug)).queryKey.slice(0, 2),
  indexJobs: (slug: string, repo: string) =>
    $api.queryOptions("get", "/api/v1/orgs/{org}/repos/{repo}/index-jobs", {
      params: { path: { org: slug, repo } },
    }).queryKey,
  indexJob: (slug: string, repo: string, job: string) =>
    $api.queryOptions("get", "/api/v1/orgs/{org}/repos/{repo}/index-jobs/{job}", {
      params: { path: { org: slug, repo, job } },
    }).queryKey,
  /** A run's constellation (`job`), or the last index's (`job` null); pages gathered into one
   * picture, so it isn't an `$api` query. */
  constellation: (slug: string, repo: string, job: string | null) =>
    ["constellation", slug, repo, job] as const,
  /** The branches and pull requests the repository is read at (the ref switcher's). */
  refs: (slug: string, repo: string) =>
    $api.queryOptions("get", "/api/v1/orgs/{org}/repos/{repo}/refs", {
      params: { path: { org: slug, repo } },
    }).queryKey,
  /** Whether `cited` (`<sha>:<path>`, sorted) is still as it was at `ref`; without them, every
   * check of the org's (a prefix: a new index invalidates them all). */
  staleness: (slug: string, repo?: string, ref?: string | null, cited?: readonly string[]) =>
    ["staleness", slug, ...(repo === undefined ? [] : [repo, ref ?? null, cited ?? []])] as const,
  /** The code browser's directory listings and files at `ref` (null: what that means now,
   * `version`: the last index, or the first run's stage, so they are read again when it
   * changes). Their commit comes in a response header, so they aren't `$api` queries either. */
  codeTree: (
    slug: string,
    repo: string,
    ref: string | null,
    version: string | null,
    path: string,
  ) => ["code", "tree", slug, repo, ref ?? version, path] as const,
  codeFile: (
    slug: string,
    repo: string,
    ref: string | null,
    version: string | null,
    path: string,
  ) => ["code", "file", slug, repo, ref ?? version, path] as const,
  /** The code viewer's symbols: those around a line, a file's outline, and a node's neighbours
   * in the graph (`query`: direction, types, depth, limit), at the snapshot as above. */
  symbolsAt: (
    slug: string,
    repo: string,
    ref: string | null,
    version: string | null,
    path: string,
    line: number,
  ) => ["code", "symbols-at", slug, repo, ref ?? version, path, line] as const,
  symbolOutline: (
    slug: string,
    repo: string,
    ref: string | null,
    version: string | null,
    path: string,
  ) => ["code", "outline", slug, repo, ref ?? version, path] as const,
  graphNeighbors: (
    slug: string,
    repo: string,
    ref: string | null,
    version: string | null,
    symbol: string,
    query: object,
  ) => ["code", "neighbors", slug, repo, ref ?? version, symbol, query] as const,
  /** A repository's Ask conversations (the signed-in user's). */
  conversations: (slug: string, repo: string) => ["conversations", slug, repo] as const,
  /** One conversation, with its messages. */
  conversation: (slug: string, id: string) => ["conversation", slug, id] as const,
  /** A repository's agent runs, and one run with its steps. */
  runs: (slug: string, repo: string) => ["runs", slug, repo] as const,
  run: (slug: string, id: string) => ["run", slug, id] as const,
  /** A trace from an entry (a node's key, a route, a file or a name), at the snapshot as above. */
  trace: (slug: string, repo: string, ref: string | null, version: string | null, entry: string) =>
    ["trace", slug, repo, ref ?? version, entry] as const,
  /** The trace page's entry picker: the symbols named like `q`. */
  traceEntries: (slug: string, repo: string, ref: string | null, q: string) =>
    ["trace-entries", slug, repo, ref, q] as const,
  /** A search's first page, and one lane's further pages from that page's cursor (POST bodies,
   * so not `$api` queries). */
  search: (slug: string, repo: string, body: object) => ["search", slug, repo, body] as const,
  searchLane: (slug: string, repo: string, body: object, lane: string, cursor: string | null) =>
    ["search", slug, repo, body, "lane", lane, cursor] as const,
  /** An exact (ripgrep) search. */
  grep: (slug: string, repo: string, body: object) => ["grep", slug, repo, body] as const,
  installations: (slug: string) =>
    $api.queryOptions("get", "/api/v1/orgs/{org}/github/installations", org(slug)).queryKey,
};
