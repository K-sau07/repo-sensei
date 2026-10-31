"use client";

/**
 * The ref a repository's pages are read at (5.6): `?ref=` in the URL (a branch, a pull request's
 * `pull/12`, or a commit; none: the default branch's last index), the same parameter the code
 * browser, search and trace already read. The tabs carry it, so a branch picked stays picked.
 */
import { parseAsString, useQueryState } from "nuqs";

import { $api } from "@/lib/api/client";

import { useRepoSlug } from "./use-repo";

export function useViewRef() {
  return useQueryState("ref", parseAsString.withOptions({ history: "push", scroll: false }));
}

/** The repository's refs (the default branch first), with what each one's index is. */
export function useRefs(enabled = true) {
  const { org, repo } = useRepoSlug();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/refs",
    { params: { path: { org, repo } } },
    { enabled, staleTime: 30_000 },
  );
}

/** `href` with `?ref=` kept (a tab's link, while a branch is picked). */
export function withRef<T extends string>(href: T, ref: string | null): T {
  if (!ref) return href;
  const [path, query = ""] = href.split("?", 2) as [string, string?];
  const params = new URLSearchParams(query);
  params.set("ref", ref);
  return `${path}?${params.toString()}` as T;
}
