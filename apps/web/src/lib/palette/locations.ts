/**
 * What a page of the app is, as a palette item: its id (the same one the providers give it, so a
 * visited file and a searched one are one row), kind, title and scope. Used to remember visits.
 */
import { ALL_TABS } from "@/components/repo/repo-tabs-config";

import type { HistoryInput } from "./history";

/** Org pages (`/[org]/<section>`), by section. */
export const ORG_PAGE_TITLES: Record<string, string> = {
  "": "Home",
  inbox: "Inbox",
  activity: "Activity",
  insights: "Insights",
  repos: "Repositories",
  "repos/new": "Connect a repository",
  "settings/general": "Organization settings",
  "settings/members": "Members",
};

/** Names a slug can't have, because org pages use them (`/[org]/repos` isn't a repository). */
const ORG_SECTIONS = new Set(["inbox", "activity", "insights", "repos", "settings"]);

export const pageId = (href: string) => `page:${href}`;
export const repoId = (org: string, repo: string) => `repo:${org}/${repo}`;
/** Files and directories share ids: a path is one place, however it was found. */
export const pathId = (org: string, repo: string, path: string) => `code:${org}/${repo}:${path}`;
export const symbolId = (org: string, repo: string, key: string) => `symbol:${org}/${repo}:${key}`;
export const actionId = (id: string) => `action:${id}`;

function decode(segment: string): string | null {
  try {
    return decodeURIComponent(segment);
  } catch {
    return null;
  }
}

/** A name with an extension (or one of the usual extensionless files) is a file; otherwise
 * we call it a directory. Only the icon depends on it. */
export function looksLikeFile(path: string): boolean {
  const name = path.slice(path.lastIndexOf("/") + 1);
  return /\.[^.]+$/.test(name) || /^(Dockerfile|Makefile|LICENSE|README|Procfile)$/i.test(name);
}

export const parentOf = (path: string) =>
  path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";

/** The palette item for a visited path, or null for pages it doesn't offer. */
export function describeLocation(pathname: string): HistoryInput | null {
  const parts = pathname.split("/").filter(Boolean).map(decode);
  if (parts.some((part) => part === null)) return null;
  const [org, second, ...rest] = parts as string[];
  if (!org || org === "settings") return null; // account pages aren't org-scoped
  const href = `/${parts.map((part) => encodeURIComponent(part ?? "")).join("/")}`;

  if (second === undefined || ORG_SECTIONS.has(second)) {
    const section = [second, ...rest].filter(Boolean).join("/");
    const title = ORG_PAGE_TITLES[section];
    if (title === undefined) return null;
    return { id: pageId(href), kind: "page", title, href, org };
  }

  const repo = second;
  const [tabSection, ...pathParts] = rest;
  if (tabSection === undefined) {
    return {
      id: repoId(org, repo),
      kind: "repo",
      title: repo,
      subtitle: "Repository",
      href,
      org,
      repo,
    };
  }
  if (tabSection === "code" && pathParts.length > 0) {
    const path = pathParts.join("/");
    const name = pathParts.at(-1) ?? path;
    const dir = parentOf(path);
    return {
      id: pathId(org, repo, path),
      kind: looksLikeFile(path) ? "file" : "dir",
      title: name,
      subtitle: dir ? `${repo} · ${dir}` : repo,
      href,
      org,
      repo,
      path,
    };
  }
  const tab = ALL_TABS.find((item) => item.section === tabSection && item.section !== "");
  if (!tab || pathParts.length > 0) return null;
  return {
    id: pageId(href),
    kind: "page",
    title: tab.label,
    subtitle: repo,
    href,
    org,
    repo,
  };
}
