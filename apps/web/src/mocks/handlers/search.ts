/**
 * The mock `POST …/search` (over mocks/search.ts) and `POST …/grep`, with the API's checks in its
 * order: the body, then the snapshot (404 `repo_not_indexed` before any index), then whether it
 * can be searched yet (409 `not_indexed` during a first run before its chunks stage; partial,
 * keyword and symbols only, before its embeddings).
 *
 * Situations worth seeing, from the browser:
 *   localStorage.setItem("sensei.mock.search", "partial")
 * "partial" (embeddings pending), "error" (a 500), "slow" (1.5 s per search) or "rate-limited"
 * (429, retry in 30 s).
 */
import { delay, http, HttpResponse } from "msw";

import type { SearchResponse } from "@/lib/api/search";
import type { GrepResult } from "@/lib/api/types";
import { contentFiles } from "@/mocks/browse";
import type { MockRepo } from "@/mocks/db";
import { firstRunSnapshot, isActive, jobsOf, jobView } from "@/mocks/jobs";
import { checkBody, grep, indexFiles, search, SearchError, type Indexed } from "@/mocks/search";

import { resolveCommit } from "./browse";
import { apiError } from "./errors";
import { repoOf } from "./repos";

export const SEARCH_SCENARIO_KEY = "sensei.mock.search";
export type SearchScenario = "partial" | "error" | "slow" | "rate-limited";
const SCENARIOS = new Set<string>(["partial", "error", "slow", "rate-limited"]);
const SLOW_MS = 1500;
const RETRY_AFTER_S = 30;

export function searchScenario(): SearchScenario | null {
  if (typeof localStorage === "undefined") return null;
  const value = localStorage.getItem(SEARCH_SCENARIO_KEY);
  return value !== null && SCENARIOS.has(value) ? (value as SearchScenario) : null;
}

let indexed: Promise<Indexed[]> | null = null;
const searchable = () => (indexed ??= contentFiles().then(indexFiles));

interface Params {
  org: string;
  repo: string;
}

function failure(error: unknown) {
  if (!(error instanceof SearchError)) throw error;
  return apiError(
    error.status,
    error.code,
    error.message,
    error.field
      ? [{ loc: ["body", error.field], msg: error.message, type: "value_error" }]
      : undefined,
  );
}

async function bodyOf(request: Request): Promise<unknown> {
  try {
    return (await request.json()) as unknown;
  } catch {
    return null;
  }
}

/** What the repository's snapshot can be searched with: an index has everything; a first run
 * has keyword search from its chunks stage and semantic search from its embeddings. */
function readiness(repo: MockRepo): { keyword: boolean; semantic: boolean } {
  if (repo.last_indexed_sha) return { keyword: true, semantic: true };
  const latest = jobsOf(repo.id).at(-1);
  const job = latest ? jobView(latest) : null;
  if (!job || !isActive(job) || !firstRunSnapshot(repo)) return { keyword: false, semantic: false };
  const stages = job.stages as Record<string, { status?: string } | undefined>;
  return {
    keyword: stages.chunks?.status === "done",
    semantic: stages.embeddings?.status === "done",
  };
}

const headers = (commit: string) => ({
  "Cache-Control": "private, no-store",
  "X-Commit-Sha": commit,
});

/** A scenario that answers before the search does. */
async function scenarioResponse(): Promise<Response | null> {
  const scenario = searchScenario();
  if (scenario === "slow") await delay(SLOW_MS);
  if (scenario === "error") {
    return apiError(
      500,
      "internal_error",
      "Search failed on our side. Trying again usually works.",
    );
  }
  if (scenario === "rate-limited") {
    const response = apiError(429, "rate_limited", "Too many searches. Wait a moment.");
    response.headers.set("Retry-After", String(RETRY_AFTER_S));
    return response;
  }
  return null;
}

export const searchHandlers = [
  http.post<Params>("*/api/v1/orgs/:org/repos/:repo/search", async ({ params, request }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    let body;
    try {
      body = checkBody(await bodyOf(request));
    } catch (error) {
      return failure(error);
    }
    const early = await scenarioResponse();
    if (early) return early;
    const commit = resolveCommit(repo, body.ref ?? null);
    if (commit instanceof Response) return commit;
    const ready = readiness(repo);
    if (!ready.keyword) {
      return apiError(
        409,
        "not_indexed",
        "Search appears once this commit's keyword index is built.",
      );
    }
    try {
      const result = search(await searchable(), body, {
        semanticReady: ready.semantic && searchScenario() !== "partial",
        commit,
      });
      return HttpResponse.json<SearchResponse>(result, { headers: headers(commit) });
    } catch (error) {
      return failure(error);
    }
  }),

  http.post<Params>("*/api/v1/orgs/:org/repos/:repo/grep", async ({ params, request }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const body = await bodyOf(request);
    const ref =
      typeof body === "object" && body !== null && "ref" in body && typeof body.ref === "string"
        ? body.ref
        : null;
    const early = await scenarioResponse();
    if (early) return early;
    const commit = resolveCommit(repo, ref);
    if (commit instanceof Response) return commit;
    try {
      return HttpResponse.json<GrepResult>(grep(await searchable(), body, commit), {
        headers: headers(commit),
      });
    } catch (error) {
      return failure(error);
    }
  }),
];
