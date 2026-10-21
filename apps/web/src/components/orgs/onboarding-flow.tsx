"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Lock } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { parseAsInteger, parseAsString, useQueryStates } from "nuqs";
import { useState } from "react";

import { GitHubMark } from "@/components/auth/github-mark";
import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { DelayedSpinner } from "@/components/primitives/delayed-spinner";
import {
  ConnectRepo,
  connectProblem,
  EstimateCard,
  installUrl,
} from "@/components/repos/connect-repo";
import { Button, buttonVariants } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { $api, api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { AvailableRepo } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";
import { orgPath, repoPath } from "@/lib/routes";

import { CreateOrgForm } from "./create-org-form";
import { OnboardingSteps } from "./onboarding-steps";

/** Why the GitHub App step came back without access (the API adds `?github_error=`). */
const GITHUB_ERRORS: Record<string, string> = {
  installation_not_owned:
    "That installation belongs to an account you don't administer. Install it on your own account or an organization you're an admin of.",
  installation_unsupported: "Enterprise installations aren't supported yet.",
  github_unavailable: "GitHub didn't answer. Try again in a moment.",
};

function Heading({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <header className="flex flex-col gap-1">
      <h1 id="onboarding-heading" className="text-2xl font-semibold tracking-[-0.015em]">
        {title}
      </h1>
      <p className="text-sm text-fg-muted">{children}</p>
    </header>
  );
}

function InstallStep({ org, error }: { org: string; error: string | null }) {
  return (
    <>
      <Heading title="Install the GitHub App">
        Choose which repositories repo-sensei may read. It only ever reads: no code is changed
        without your approval, and you can change access on GitHub at any time.
      </Heading>
      {error && (
        <p role="alert" className="text-sm text-danger">
          {GITHUB_ERRORS[error] ?? "No access yet: the installation didn't finish."}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <a href={installUrl(org, "onboarding")} className={buttonVariants()}>
          <GitHubMark className="size-4" />
          Install the GitHub App
        </a>
        <Link
          href={`/onboarding?step=3&org=${encodeURIComponent(org)}&tab=url` as never}
          className={buttonVariants({ variant: "outline" })}
        >
          Skip, use a public URL
        </Link>
      </div>
      <Link
        href={orgPath(org)}
        className="w-fit text-sm text-fg-muted underline underline-offset-4"
      >
        Skip for now
      </Link>
    </>
  );
}

function PickRow({
  org,
  repo,
  checked,
  onChange,
}: {
  org: string;
  repo: AvailableRepo;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  const estimate = $api.useQuery(
    "post",
    "/api/v1/orgs/{org}/repos/estimate",
    { params: { path: { org } }, body: { github_repo_id: repo.github_repo_id } },
    { enabled: checked, retry: false, staleTime: 300_000 },
  );
  const id = `pick-${String(repo.github_repo_id)}`;
  return (
    <li className="flex flex-col gap-3 px-3 py-3">
      <div className="flex items-center gap-3">
        <Checkbox
          id={id}
          checked={checked || repo.connected}
          disabled={repo.connected}
          onCheckedChange={(value) => {
            onChange(value);
          }}
        />
        <label htmlFor={id} className="flex min-w-0 flex-1 flex-col">
          <span className="flex items-center gap-1.5 truncate text-sm font-medium">
            {repo.private && <Lock aria-label="Private" className="size-3.5 text-fg-muted" />}
            {repo.full_name}
          </span>
          <span className="truncate text-xs text-fg-muted">
            {repo.connected ? "Already connected" : (repo.language ?? "—")}
          </span>
        </label>
      </div>
      {checked && estimate.isPending && <Skeleton aria-label="Estimating" className="h-28" />}
      {checked && estimate.isSuccess && <EstimateCard estimate={estimate.data} />}
      {checked && estimate.isError && (
        <p className="text-xs text-danger">
          {estimate.error instanceof ApiError ? estimate.error.message : "Couldn't estimate."}
        </p>
      )}
    </li>
  );
}

/** The slug in a `repo_already_connected` error's details, if there is one. */
function connectedSlug(error: ApiError): string | null {
  const details = error.details;
  if (typeof details !== "object" || details === null || !("slug" in details)) return null;
  return typeof details.slug === "string" ? details.slug : null;
}

function PickStep({ org }: { org: string }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const available = $api.useQuery("get", "/api/v1/orgs/{org}/github/repos", {
    params: { path: { org }, query: { per_page: 100 } },
  });
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);

  // One at a time; each one connected leaves the selection, so after a failure, trying again
  // carries on with the rest (and one connected meanwhile counts as done).
  const connect = async () => {
    setBusy(true);
    setProblem(null);
    let first: string | null = null;
    try {
      for (const githubRepoId of selected) {
        try {
          const { data } = await api.POST("/api/v1/orgs/{org}/repos", {
            params: { path: { org } },
            body: { github_repo_id: githubRepoId },
          });
          first ??= data?.slug ?? null;
        } catch (error) {
          if (!(error instanceof ApiError && error.code === "repo_already_connected")) throw error;
          first ??= connectedSlug(error);
        }
        setSelected((current) => {
          const next = new Set(current);
          next.delete(githubRepoId);
          return next;
        });
      }
      router.push(first ? repoPath(org, first) : orgPath(org, "repos"));
    } catch (error) {
      setProblem(error);
      setBusy(false);
    } finally {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.repos(org) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.availableRepos(org) }),
      ]);
    }
  };

  const list = available.data?.items ?? [];
  return (
    <>
      <Heading title="Pick repositories">
        Each one starts indexing as soon as it is connected. You can connect more later.
      </Heading>
      {available.isPending && (
        <div role="status" aria-label="Loading repositories" className="flex flex-col gap-2">
          <Skeleton className="h-14" />
          <Skeleton className="h-14" />
        </div>
      )}
      {available.isError && (
        <ErrorState
          title="Couldn't list your GitHub repositories"
          description={available.error instanceof ApiError ? available.error.message : undefined}
          onRetry={() => void available.refetch()}
        />
      )}
      {available.isSuccess && list.length === 0 && (
        <EmptyState
          icon={<GitHubMark />}
          title="No repositories to pick"
          description="The GitHub App can't read any repositories yet. Give it access on GitHub, or connect a public repository by URL."
          action={
            <a
              href={installUrl(org, "onboarding")}
              className={buttonVariants({ variant: "outline" })}
            >
              Configure on GitHub
            </a>
          }
        />
      )}
      {list.length > 0 && (
        <ul
          aria-label="Repositories the GitHub App can read"
          className="flex flex-col divide-y divide-line rounded-lg border border-line"
        >
          {list.map((repo) => (
            <PickRow
              key={repo.github_repo_id}
              org={org}
              repo={repo}
              checked={selected.has(repo.github_repo_id)}
              onChange={(checked) => {
                setSelected((current) => {
                  const next = new Set(current);
                  if (checked) next.add(repo.github_repo_id);
                  else next.delete(repo.github_repo_id);
                  return next;
                });
              }}
            />
          ))}
        </ul>
      )}
      {problem !== null && (
        <p role="alert" className="text-sm text-danger">
          {connectProblem(org, problem).message}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Button disabled={selected.size === 0 || busy} onClick={() => void connect()}>
          {busy ? (
            <DelayedSpinner active label="Connecting" />
          ) : selected.size > 1 ? (
            `Connect ${String(selected.size)} repositories`
          ) : (
            "Connect and index"
          )}
        </Button>
        <Link href={orgPath(org)} className="text-sm text-fg-muted underline underline-offset-4">
          Skip for now
        </Link>
      </div>
    </>
  );
}

/** `/onboarding`: 1 name the org → 2 install the GitHub App (or use a public URL) → 3 pick
 * repositories (or paste a public URL), then the first repository's page. The step and org
 * are in the URL, so returning from GitHub lands on the right step. */
export function OnboardingFlow() {
  const [{ step, org, tab, github_error: githubError }] = useQueryStates({
    step: parseAsInteger.withDefault(1),
    org: parseAsString,
    tab: parseAsString,
    github_error: parseAsString,
  });
  const current = org ? Math.min(Math.max(step, 1), 3) : 1;
  return (
    <div className="mx-auto grid w-full max-w-4xl flex-1 gap-10 p-6 md:grid-cols-[14rem_1fr] md:p-12">
      <aside>
        <OnboardingSteps current={current - 1} />
      </aside>
      <section aria-labelledby="onboarding-heading" className="flex max-w-xl flex-col gap-6">
        {current === 1 && (
          <>
            <Heading title="Create your organization">
              Repositories, questions and fixes live in an organization. Invite your team once it
              exists.
            </Heading>
            <CreateOrgForm />
          </>
        )}
        {current === 2 && org && <InstallStep org={org} error={githubError} />}
        {current === 3 && org && tab === "url" && (
          <>
            <Heading title="Connect a public repository">
              Paste the URL of any public GitHub repository to see what indexing it takes.
            </Heading>
            <ConnectRepo org={org} />
          </>
        )}
        {current === 3 && org && tab !== "url" && <PickStep org={org} />}
      </section>
    </div>
  );
}
