import type { Route } from "next";

export type OrgSection = "" | "inbox" | "activity" | "insights" | "settings" | "repos";

/**
 * Org-scoped paths. Typed routes can't check a runtime slug, so the cast lives here, once, and the
 * section names above are the only ones accepted.
 */
export function orgPath(org: string, section: OrgSection = ""): Route {
  const base = `/${encodeURIComponent(org)}`;
  return (section ? `${base}/${section}` : base) as Route;
}
