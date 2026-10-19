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
};
