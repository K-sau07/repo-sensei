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
  /** A search's first page, and one lane's further pages (POST bodies, so not `$api` queries). */
  search: (slug: string, repo: string, body: object) => ["search", slug, repo, body] as const,
  searchLane: (slug: string, repo: string, body: object, lane: string) =>
    ["search", slug, repo, body, "lane", lane] as const,
  /** An exact (ripgrep) search. */
  grep: (slug: string, repo: string, body: object) => ["grep", slug, repo, body] as const,
  installations: (slug: string) =>
    $api.queryOptions("get", "/api/v1/orgs/{org}/github/installations", org(slug)).queryKey,
};
