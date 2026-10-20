import type { Route } from "next";

export type OrgSection =
  | ""
  | "inbox"
  | "activity"
  | "insights"
  | "repos"
  | "repos/new"
  | "settings"
  | "settings/general"
  | "settings/members";

/**
 * Org-scoped paths. Typed routes can't check a runtime slug, so the cast lives here, once, and the
 * section names above are the only ones accepted.
 */
export function orgPath(org: string, section: OrgSection = ""): Route {
  const base = `/${encodeURIComponent(org)}`;
  return (section ? `${base}/${section}` : base) as Route;
}

/** A repository's pages: `/[org]/[repo]` and its tabs. */
export function repoPath(org: string, repo: string, section = ""): Route {
  const base = `/${encodeURIComponent(org)}/${encodeURIComponent(repo)}`;
  return (section ? `${base}/${section}` : base) as Route;
}
