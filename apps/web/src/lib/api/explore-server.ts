/**
 * The public gallery's data, read on the server (10.2) so its pages render whole for anyone and
 * any crawler: kept a few minutes (`use cache`, the "minutes" profile: served while it is fetched
 * again after a minute), as a published repository only changes when it is indexed again.
 *
 * The API is called directly: `SENSEI_API_PROXY_ORIGIN` (what /api/* is forwarded to), else the
 * site's own origin (`SENSEI_WEB_ORIGIN`, whose /api/* the load balancer routes), else the local
 * API in development. In mock mode there is no API: the mock gallery answers.
 */
import { cacheLife } from "next/cache";

import type { HumanCheck } from "@/components/explore/explore-ask";

import type { FeatureMapList, PublicRepo, PublicRepoPage } from "./explore";

const MOCKS = process.env.NEXT_PUBLIC_MOCKS === "1";
// A page waits for these before it sends anything: an API that hangs fails it soon instead.
const READ_TIMEOUT_MS = 8_000;

function apiOrigin(): string {
  return (
    process.env.SENSEI_API_PROXY_ORIGIN ??
    process.env.SENSEI_WEB_ORIGIN ??
    (process.env.NODE_ENV === "development" ? "http://localhost:8000" : "http://localhost:3000")
  );
}

/** The site's own origin, for links and images that leave it (sitemaps, social cards). */
export function siteOrigin(): string {
  return process.env.SENSEI_WEB_ORIGIN ?? "http://localhost:3000";
}

/** How a public page's questions prove a person asks them: Turnstile's check with the site's
 * key (`NEXT_PUBLIC_TURNSTILE_SITE_KEY`), none in mock mode, and no questions without a key. */
export function humanCheck(): HumanCheck {
  if (MOCKS) return { kind: "mock" };
  const siteKey = process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY;
  return siteKey ? { kind: "widget", siteKey } : { kind: "off" };
}

/** The API's answer, or null when it says not found; anything else is an error (the page's
 * error boundary says so). */
async function read<T>(path: string): Promise<T | null> {
  const response = await fetch(`${apiOrigin()}/api/v1${path}`, {
    headers: { accept: "application/json" },
    signal: AbortSignal.timeout(READ_TIMEOUT_MS),
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`The API answered ${response.status} for ${path}.`);
  return (await response.json()) as T;
}

function named(owner: string, name: string): string {
  return `${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;
}

export async function publicRepos(): Promise<PublicRepo[]> {
  "use cache";
  cacheLife("minutes");
  if (MOCKS) return (await import("@/mocks/explore")).MOCK_PUBLIC_REPOS;
  return (await read<{ items: PublicRepo[] }>("/public/repos"))?.items ?? [];
}

export async function publicRepo(owner: string, name: string): Promise<PublicRepoPage | null> {
  "use cache";
  cacheLife("minutes");
  if (MOCKS) return (await import("@/mocks/explore")).mockPublicPage(owner, name);
  return read<PublicRepoPage>(`/public/repos/${named(owner, name)}`);
}

export async function publicMaps(owner: string, name: string): Promise<FeatureMapList | null> {
  "use cache";
  cacheLife("minutes");
  if (MOCKS) return (await import("@/mocks/explore")).mockPublicMaps(owner, name);
  return read<FeatureMapList>(`/public/repos/${named(owner, name)}/feature-maps`);
}

/** The repository the landing page's demo asks about: `SENSEI_FEATURED_REPO` ("owner/name")
 * when it is published, else the first published; null when none is (or the API is down: the
 * landing page shows the rest). */
export async function featuredRepo(): Promise<PublicRepoPage | null> {
  try {
    const chosen = process.env.SENSEI_FEATURED_REPO?.split("/");
    if (chosen?.length === 2 && chosen[0] && chosen[1]) {
      const page = await publicRepo(chosen[0], chosen[1]);
      if (page) return page;
    }
    const first = (await publicRepos())[0];
    return first ? await publicRepo(first.owner, first.name) : null;
  } catch {
    return null;
  }
}
