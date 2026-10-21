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

/** A file or directory in the code browser ("" is the root). Each path segment is encoded;
 * `ref` pins a commit and `lines` is a range as `?L=` writes it ("10-42"). */
export function codePath(
  org: string,
  repo: string,
  path = "",
  { ref, lines }: { ref?: string | null; lines?: string | null } = {},
): Route {
  const segments = path.split("/").filter(Boolean).map(encodeURIComponent);
  const query = new URLSearchParams();
  if (ref) query.set("ref", ref);
  if (lines) query.set("L", lines);
  const search = query.size > 0 ? `?${query.toString()}` : "";
  return `${repoPath(org, repo, ["code", ...segments].join("/"))}${search}` as Route;
}
