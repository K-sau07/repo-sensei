/** The mock `GET …/refs` and `…/staleness` (5.5, 5.6), with the real API's checks and errors.
 * Mock repositories keep one snapshot, so a tracked branch is indexed at the default branch's
 * commit (a branch made from it, as 5.4 points one at the snapshot it shares); a pull request
 * the repository indexes has its overlay still being built. */
import { http, HttpResponse } from "msw";

import type { RepoRef, Staleness } from "@/lib/api/refs";
import { fileAt } from "@/mocks/browse";
import type { MockRepo } from "@/mocks/db";
import { mockPushed, PUSHED_SHA, staleReason } from "@/mocks/push";

import { resolveCommit } from "./browse";
import { apiError } from "./errors";
import { repoOf } from "./repos";

/** The pull requests a repository that indexes them has open. */
export const MOCK_PULLS = [7];
const MAX_CITATIONS = 200;

interface Params {
  org: string;
  repo: string;
}

function defaultStatus(repo: MockRepo): RepoRef["status"] {
  if (repo.active_job?.ref === null) return "indexing";
  if (repo.last_indexed_sha) return "indexed";
  return repo.status === "error" ? "failed" : "not_indexed";
}

export function refsOf(repo: MockRepo): RepoRef[] {
  const sha = repo.last_indexed_sha;
  const indexedAt = repo.last_indexed_at ?? null;
  const settings = repo.settings;
  const tracked = [...new Set(settings.tracked_branches ?? [])]
    .filter((name) => name !== repo.default_branch)
    .sort();
  return [
    {
      name: repo.default_branch,
      kind: "branch",
      default: true,
      status: defaultStatus(repo),
      sha,
      indexed_at: indexedAt,
      overlay_of: null,
    },
    ...tracked.map((name): RepoRef => ({
      name,
      kind: "branch",
      default: false,
      status: sha ? "indexed" : "not_indexed",
      sha,
      indexed_at: indexedAt,
      overlay_of: null,
    })),
    ...(settings.index_pull_requests ? MOCK_PULLS : []).map((n): RepoRef => ({
      name: `pull/${String(n)}`,
      kind: "pull_request",
      default: false,
      status: "indexing",
      sha: null,
      indexed_at: null,
      overlay_of: sha,
    })),
  ];
}

export const refHandlers = [
  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/refs", ({ params }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    return HttpResponse.json({ refs: refsOf(repo) });
  }),

  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/staleness", ({ params, request }) => {
    const repo = repoOf(params.org, params.repo);
    if (repo instanceof Response) return repo;
    const url = new URL(request.url);
    const indexed = resolveCommit(repo, url.searchParams.get("ref"));
    if (indexed instanceof Response) return indexed;
    const cited = url.searchParams.getAll("citations");
    if (cited.length === 0 || cited.length > MAX_CITATIONS) {
      return apiError(422, "validation_failed", "Request validation failed.", [
        { loc: ["query", "citations"], msg: "Give 1 to 200 citations", type: "value_error" },
      ]);
    }
    const citations: Staleness["citations"] = [];
    for (const one of cited) {
      const at = one.indexOf(":");
      const sha = (at < 0 ? one : one.slice(0, at)).toLowerCase();
      const path = at < 0 ? "" : one.slice(at + 1);
      if (sha.length < 7 || sha.length > 64 || !path) {
        return apiError(422, "invalid_citation", `'${one.slice(0, 80)}' isn't a citation.`);
      }
      const reason = staleReason(sha, path, indexed, (p) => fileAt(p) !== undefined);
      const stale = reason === "unknown" ? null : reason !== "unchanged";
      citations.push({ sha, path, stale, reason });
    }
    return HttpResponse.json<Staleness>({
      sha: mockPushed() ? PUSHED_SHA : indexed,
      citations,
    });
  }),
];
