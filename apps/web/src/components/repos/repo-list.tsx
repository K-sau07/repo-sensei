"use client";

import { FolderGit2, Plus } from "lucide-react";
import Link from "next/link";
import { parseAsString, parseAsStringLiteral, useQueryState } from "nuqs";
import { useMemo, useRef } from "react";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { RelativeTime } from "@/components/primitives/relative-time";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { useShortcut } from "@/hooks/use-shortcut";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { Repo } from "@/lib/api/types";
import { orgPath, repoPath } from "@/lib/routes";

import { LanguageBar, languageShares } from "./language-bar";
import { RepoStatus } from "./repo-status";

const STATUS_FILTERS = ["all", "ready", "indexing", "attention"] as const;
type StatusFilter = (typeof STATUS_FILTERS)[number];
const STATUS_LABELS: Record<StatusFilter, string> = {
  all: "All statuses",
  ready: "Ready",
  indexing: "Indexing",
  attention: "Needs attention",
};

export function matchesStatus(repo: Repo, filter: StatusFilter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "ready":
      return repo.status === "ready" && !repo.active_job;
    case "indexing":
      return repo.active_job !== null || repo.status === "indexing" || repo.status === "pending";
    case "attention":
      return repo.status === "error" || repo.status === "access_lost";
  }
}

export function topLanguage(repo: Repo): string | null {
  return languageShares(repo.languages)[0]?.language ?? null;
}

const formatLoc = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });

function RepoRow({ org, repo }: { org: string; repo: Repo }) {
  return (
    <li>
      <Link
        href={repoPath(org, repo.slug)}
        data-repo-row
        className="grid grid-cols-[minmax(0,2fr)_minmax(0,1.4fr)_5rem_8.5rem] items-center gap-4 rounded-md px-3 py-3 text-sm hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
      >
        <span className="flex min-w-0 flex-col">
          <span className="truncate font-medium">{repo.name}</span>
          <span className="truncate text-xs text-fg-muted">
            {repo.full_name} · {repo.visibility}
          </span>
        </span>
        <LanguageBar languages={repo.languages} />
        <span className="text-right text-xs text-fg-muted tabular-nums">
          {repo.loc == null ? "–" : `${formatLoc.format(repo.loc)} lines`}
        </span>
        <span className="flex flex-col items-end gap-0.5">
          <RepoStatus repo={repo} showLabel />
          <span className="text-xs text-fg-faint">
            {repo.last_indexed_at ? (
              <>
                Indexed <RelativeTime date={repo.last_indexed_at} />
              </>
            ) : (
              "Never indexed"
            )}
          </span>
        </span>
      </Link>
    </li>
  );
}

function RowsSkeleton() {
  return (
    <div role="status" aria-label="Loading repositories" className="flex flex-col gap-2">
      {[0, 1, 2].map((key) => (
        <Skeleton key={key} className="h-14" />
      ))}
    </div>
  );
}

/** `/[org]/repos`: the org's connected repositories, filterable by status and language
 * (filters live in the URL), with j/k to move between rows. */
export function RepoList({ org }: { org: string }) {
  const { role } = useOrgRole();
  const isAdmin = roleAtLeast(role, "admin");
  const repos = $api.useQuery("get", "/api/v1/orgs/{org}/repos", {
    params: { path: { org } },
  });
  const [status, setStatus] = useQueryState(
    "status",
    parseAsStringLiteral(STATUS_FILTERS).withDefault("all"),
  );
  const [language, setLanguage] = useQueryState("language", parseAsString.withDefault(""));
  const listRef = useRef<HTMLUListElement>(null);

  const all = useMemo(() => repos.data ?? [], [repos.data]);
  const languages = useMemo(
    () => [...new Set(all.flatMap((repo) => topLanguage(repo) ?? []))].sort(),
    [all],
  );
  const shown = all.filter(
    (repo) => matchesStatus(repo, status) && (!language || topLanguage(repo) === language),
  );

  const move = (step: 1 | -1) => {
    const rows = [...(listRef.current?.querySelectorAll<HTMLElement>("[data-repo-row]") ?? [])];
    if (rows.length === 0) return;
    const index = rows.findIndex((row) => row === document.activeElement);
    const next = index === -1 ? (step === 1 ? 0 : rows.length - 1) : index + step;
    rows[Math.max(0, Math.min(rows.length - 1, next))]?.focus();
  };
  useShortcut("list-next", () => {
    move(1);
  });
  useShortcut("list-prev", () => {
    move(-1);
  });

  const connect = isAdmin && (
    <Link href={orgPath(org, "repos/new")} className={buttonVariants()}>
      <Plus aria-hidden data-icon="inline-start" />
      Connect repository
    </Link>
  );

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-8">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold tracking-[-0.015em]">Repositories</h1>
        {all.length > 0 && connect}
      </header>

      {repos.isPending && <RowsSkeleton />}
      {repos.isError && (
        <ErrorState
          title="Couldn't load repositories"
          description={repos.error instanceof ApiError ? repos.error.message : undefined}
          onRetry={() => void repos.refetch()}
          {...(repos.error instanceof ApiError && repos.error.requestId
            ? { requestId: repos.error.requestId }
            : {})}
        />
      )}
      {repos.isSuccess && all.length === 0 && (
        <EmptyState
          icon={<FolderGit2 />}
          title="No repositories yet"
          description={
            isAdmin
              ? "Connect a repository from GitHub, or import a public one by URL. Indexing starts right away."
              : "An admin of this organization can connect repositories."
          }
          action={connect || undefined}
        />
      )}
      {repos.isSuccess && all.length > 0 && (
        <section aria-labelledby="repo-list-heading" className="flex flex-col gap-3">
          <h2 id="repo-list-heading" className="sr-only">
            Connected repositories
          </h2>
          <div className="flex flex-wrap items-center gap-2">
            <Select value={status} onValueChange={(value) => void setStatus(value)}>
              <SelectTrigger aria-label="Filter by status" size="sm" className="w-44">
                <SelectValue>{STATUS_LABELS[status]}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                {STATUS_FILTERS.map((value) => (
                  <SelectItem key={value} value={value}>
                    {STATUS_LABELS[value]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={language || "any"}
              onValueChange={(value) => void setLanguage(value === "any" ? null : String(value))}
            >
              <SelectTrigger aria-label="Filter by language" size="sm" className="w-44">
                <SelectValue>{language || "All languages"}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="any">All languages</SelectItem>
                {languages.map((value) => (
                  <SelectItem key={value} value={value}>
                    {value}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <span className="ml-auto text-xs text-fg-muted" aria-live="polite">
              {shown.length === all.length
                ? `${all.length} ${all.length === 1 ? "repository" : "repositories"}`
                : `${shown.length} of ${all.length}`}
            </span>
          </div>
          {shown.length === 0 ? (
            <EmptyState
              title="Nothing matches these filters"
              action={
                <Button
                  variant="outline"
                  onClick={() => {
                    void setStatus(null);
                    void setLanguage(null);
                  }}
                >
                  Clear filters
                </Button>
              }
            />
          ) : (
            <ul
              ref={listRef}
              className="flex flex-col divide-y divide-line rounded-lg border border-line"
            >
              {shown.map((repo) => (
                <RepoRow key={repo.id} org={org} repo={repo} />
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
