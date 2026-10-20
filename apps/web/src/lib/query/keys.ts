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
  installations: (slug: string) =>
    $api.queryOptions("get", "/api/v1/orgs/{org}/github/installations", org(slug)).queryKey,
};
