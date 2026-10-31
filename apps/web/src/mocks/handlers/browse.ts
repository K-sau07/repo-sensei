/** The mock `GET …/tree`, `…/file` and `…/files/search`, over the snapshot in mocks/browse.ts,
 * with the real API's checks, errors and headers. */
import { http, HttpResponse } from "msw";

import type { FileContent, FileSearch, TreeListing } from "@/lib/api/types";
import {
  dirAt,
  fileAt,
  listing,
  MAX_SERVED_BYTES,
  searchFiles,
  textOf,
  type MockFile,
} from "@/mocks/browse";
import type { MockRepo } from "@/mocks/db";
import { firstRunSnapshot } from "@/mocks/jobs";

import { apiError } from "./errors";
import { repoOf } from "./repos";

const SEARCH_LIMIT = 50;
const MAX_PATH = 4096;
const MAX_REF = 255;

interface Params {
  org: string;
  repo: string;
}

export function validationFailed(field: string, message: string) {
  return apiError(422, "validation_failed", "Request validation failed.", [
    { loc: ["query", field], msg: message, type: "value_error" },
  ]);
}

/** The commit `ref` means: the last index (or the first run's, from its discovery on), a prefix
 * of its commit, or a branch it tracks. (Mock repositories index one commit, which their tracked
 * branches share, so nothing else resolves.) */
export function resolveCommit(repo: MockRepo, ref: string | null): string | Response {
  const indexed = repo.last_indexed_sha;
  if (ref === null) {
    // Before the first index: the first run's snapshot, once its discovery is done.
    return (
      indexed ??
      firstRunSnapshot(repo)?.commit ??
      apiError(404, "repo_not_indexed", "This repository hasn't been indexed yet.")
    );
  }
  if (ref.length > MAX_REF)
    return validationFailed("ref", "String should have at most 255 characters");
  if (!ref.trim()) return apiError(422, "invalid_ref", "That isn't a branch or commit.");
  if (
    indexed &&
    (ref === repo.default_branch ||
      (repo.settings.tracked_branches ?? []).includes(ref) ||
      (/^[0-9a-f]{7,64}$/.test(ref) && indexed.startsWith(ref)))
  ) {
    return indexed;
  }
  return apiError(404, "ref_not_indexed", `Nothing indexed at '${ref}'.`);
}

/** `check_path`: relative and normal ("" or "/" is the root; a trailing slash is dropped). */
export function checkPath(raw: string): string | Response {
  if (raw.length > MAX_PATH)
    return validationFailed("path", "String should have at most 4096 characters");
  if (raw === "" || raw === "/") return "";
  const cleaned = raw.replace(/\/+$/, "");
  const bad =
    raw.startsWith("/") ||
    cleaned.includes("\\") ||
    cleaned.includes("\0") ||
    cleaned.split("/").some((part) => part === "" || part === "." || part === "..");
  return bad ? apiError(400, "invalid_path", "That isn't a path inside the repository.") : cleaned;
}

function headers(etag: string | null, commit: string): Record<string, string> {
  return {
    ...(etag ? { ETag: etag } : {}),
    "Cache-Control": "private, no-cache",
    "X-Commit-Sha": commit,
  };
}

function metadata(file: MockFile, commit: string) {
  return {
    commit_sha: commit,
    path: file.path,
    language: file.language,
    role: file.role,
    size: file.size,
    loc: file.loc,
    flags: file.flags,
    sha: file.sha,
  };
}

/** The repository and the commit to read, or the error response. */
export function target(params: Params, request: Request) {
  const repo = repoOf(params.org, params.repo);
  if (repo instanceof Response) return repo;
  const url = new URL(request.url);
  const commit = resolveCommit(repo, url.searchParams.get("ref"));
  if (commit instanceof Response) return commit;
  return { repo, commit, url };
}

export const browseHandlers = [
  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/tree", ({ params, request }) => {
    const found = target(params, request);
    if (found instanceof Response) return found;
    const path = checkPath(found.url.searchParams.get("path") ?? "");
    if (path instanceof Response) return path;
    const dir = dirAt(path);
    if (!dir) {
      if (fileAt(path)) {
        return apiError(422, "not_a_directory", "That's a file, not a directory.");
      }
      return apiError(404, "path_not_found", "No such directory in this snapshot.");
    }
    // A first run before its Merkle stage: listed from the file rows, not cached.
    const early = found.url.searchParams.get("ref") === null && firstRunSnapshot(found.repo);
    return HttpResponse.json<TreeListing>(
      { path, entries: listing(path) },
      {
        headers:
          early && !early.treeReady
            ? { "Cache-Control": "no-store", "X-Commit-Sha": found.commit }
            : headers(dir.hash ? `"t1-${dir.hash}"` : null, found.commit),
      },
    );
  }),

  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/file", async ({ params, request }) => {
    const found = target(params, request);
    if (found instanceof Response) return found;
    const raw = found.url.searchParams.get("path");
    if (raw === null) return validationFailed("path", "Field required");
    const path = checkPath(raw);
    if (path instanceof Response) return path;
    if (!path) return apiError(400, "invalid_path", "Give the path of a file.");
    const file = fileAt(path);
    if (!file) return apiError(404, "path_not_found", "No such file in this snapshot.");
    if (file.flags.includes("binary")) {
      return apiError(415, "binary_file", "This file isn't text; download it instead.", {
        ...metadata(file, found.commit),
        download_url: `https://storage.mock.invalid/artifacts/${file.sha}?X-Amz-Expires=900`,
      });
    }
    if (file.size > MAX_SERVED_BYTES) {
      return apiError(
        413,
        "file_too_large",
        "This file is too large to show here; download it instead.",
        {
          ...metadata(file, found.commit),
          // A presigned link to the artifacts bucket in the real API; nothing serves it here.
          download_url: `https://storage.mock.invalid/artifacts/${file.sha}?X-Amz-Expires=900`,
        },
      );
    }
    return HttpResponse.json<FileContent>(
      {
        path: file.path,
        language: file.language,
        role: file.role,
        size: file.size,
        loc: file.loc,
        flags: file.flags,
        sha: file.sha,
        encoding: "utf-8",
        content: await textOf(file),
      },
      { headers: headers(`"f1-${file.content_hash}"`, found.commit) },
    );
  }),

  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/files/search", ({ params, request }) => {
    const found = target(params, request);
    if (found instanceof Response) return found;
    const q = found.url.searchParams.get("q");
    if (q === null) return validationFailed("q", "Field required");
    if (q.length === 0) return validationFailed("q", "String should have at least 1 character");
    if (q.length > 200) return validationFailed("q", "String should have at most 200 characters");
    const limit = Number(found.url.searchParams.get("limit") ?? SEARCH_LIMIT);
    if (!Number.isInteger(limit) || limit < 1 || limit > SEARCH_LIMIT) {
      return validationFailed("limit", "Input should be between 1 and 50");
    }
    const query = q.trim();
    if (!query) return apiError(422, "invalid_query", "Type something to search for.");
    return HttpResponse.json<FileSearch>({
      commit_sha: found.commit,
      query,
      results: searchFiles(query, limit),
    });
  }),
];
