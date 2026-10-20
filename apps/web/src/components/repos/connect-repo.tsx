"use client";

import { useQueryClient } from "@tanstack/react-query";
import { cn } from "cn";
import { ExternalLink, Lock, RefreshCw, Search } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { parseAsStringLiteral, useQueryState } from "nuqs";
import { useId, useState } from "react";

import { GitHubMark } from "@/components/auth/github-mark";
import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { DelayedSpinner } from "@/components/primitives/delayed-spinner";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { $api, api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { AvailableRepo, Estimate, Repo } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";
import { orgPath, repoPath } from "@/lib/routes";

import { LanguageBar } from "./language-bar";

const TABS = ["github", "url"] as const;
const minutes = (value: number) =>
  value < 1 ? "under a minute" : `about ${Math.round(value)} min`;
const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });

/** Where to send the browser to install the GitHub App (an API redirect, so a plain link). */
export function installUrl(org: string, from: "repos" | "onboarding"): string {
  return `/api/v1/github/install?org=${encodeURIComponent(org)}&from=${from}`;
}

/** What went wrong, as the next step for the user. */
export function connectProblem(org: string, error: unknown) {
  if (!(error instanceof ApiError)) {
    return { message: "Something went wrong. Please try again.", link: null };
  }
  if (error.code === "repo_already_connected") {
    const slug = (error.details as { slug?: string } | null)?.slug;
    return {
      message: error.message,
      link: slug ? { href: repoPath(org, slug), label: "Open it" } : null,
    };
  }
  if (error.code === "cost_cap_reached") {
    return {
      message: `${error.message} An admin can raise the limit.`,
      link: { href: orgPath(org, "settings"), label: "Organization settings" },
    };
  }
  return { message: error.message, link: null };
}

function Problem({ org, error }: { org: string; error: unknown }) {
  const { message, link } = connectProblem(org, error);
  return (
    <p role="alert" className="text-sm text-danger">
      {message}{" "}
      {link && (
        <Link href={link.href} className="text-fg underline underline-offset-4">
          {link.label}
        </Link>
      )}
    </p>
  );
}

/** Size, time and cost before connecting. */
export function EstimateCard({ estimate }: { estimate: Estimate }) {
  return (
    <section
      aria-label={`Estimate for ${estimate.full_name}`}
      className="flex flex-col gap-4 rounded-lg border border-line bg-surface-1 p-4"
    >
      <header className="flex items-center gap-2">
        <span className="font-medium">{estimate.full_name}</span>
        <Badge variant="secondary">{estimate.private ? "private" : "public"}</Badge>
      </header>
      <LanguageBar languages={estimate.languages} legend />
      <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        {[
          ["Files", `~${compact.format(estimate.files)}`],
          ["Tokens", `~${compact.format(estimate.tokens)}`],
          ["Indexing time", minutes(estimate.index_minutes)],
          ["First index", `~$${Number(estimate.cost_usd).toFixed(2)}`],
        ].map(([term, value]) => (
          <div key={term} className="flex flex-col gap-0.5">
            <dt className="text-xs text-fg-muted">{term}</dt>
            <dd className="tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      {estimate.heuristic && (
        <p className="text-xs text-fg-faint">
          Estimates from GitHub&apos;s numbers, not a measurement.
        </p>
      )}
    </section>
  );
}

function useConnected(org: string) {
  const queryClient = useQueryClient();
  const router = useRouter();
  return (repo: Repo) => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.repos(org) });
    void queryClient.invalidateQueries({ queryKey: queryKeys.availableRepos(org) });
    router.push(repoPath(org, repo.slug));
  };
}

function GitHubRow({ org, repo }: { org: string; repo: AvailableRepo }) {
  const connected = useConnected(org);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      const { data } = await api.POST("/api/v1/orgs/{org}/repos", {
        params: { path: { org } },
        body: { github_repo_id: repo.github_repo_id },
      });
      if (data) connected(data);
    } catch (problem) {
      setError(problem);
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="flex flex-col gap-1 px-3 py-3">
      <div className="flex items-center gap-3">
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="flex items-center gap-1.5 truncate text-sm font-medium">
            {repo.private && <Lock aria-label="Private" className="size-3.5 text-fg-muted" />}
            {repo.full_name}
          </span>
          <span className="truncate text-xs text-fg-muted">
            {[repo.language, repo.description].filter(Boolean).join(" · ") || "No description"}
          </span>
        </span>
        {repo.connected ? (
          <Badge variant="outline">Connected</Badge>
        ) : (
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => void connect()}
            aria-label={`Connect ${repo.full_name}`}
          >
            {busy ? <DelayedSpinner active label="Connecting" /> : "Connect"}
          </Button>
        )}
      </div>
      {error !== null && <Problem org={org} error={error} />}
    </li>
  );
}

function GitHubTab({ org }: { org: string }) {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const q = useDebouncedValue(query.trim(), 250);
  const installations = $api.useQuery("get", "/api/v1/orgs/{org}/github/installations", {
    params: { path: { org } },
  });
  const available = $api.useQuery("get", "/api/v1/orgs/{org}/github/repos", {
    params: { path: { org }, query: { q, per_page: 100 } },
  });
  const manage = installations.data?.[0]?.manage_url;

  if (installations.isSuccess && installations.data.length === 0) {
    return (
      <EmptyState
        icon={<GitHubMark />}
        title="Install the GitHub App first"
        description="Choose which repositories repo-sensei may read. You can change that on GitHub at any time."
        action={
          <a href={installUrl(org, "repos")} className={buttonVariants()}>
            <GitHubMark className="size-4" />
            Install the GitHub App
          </a>
        }
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-60 flex-1">
          <Search
            aria-hidden
            className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-faint"
          />
          <Input
            aria-label="Search repositories"
            placeholder="Search repositories"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
            }}
            className="pl-8"
          />
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() =>
            void queryClient.invalidateQueries({ queryKey: queryKeys.availableRepos(org) })
          }
        >
          <RefreshCw aria-hidden data-icon="inline-start" />
          Refresh
        </Button>
        {manage && (
          <a
            href={manage}
            target="_blank"
            rel="noreferrer"
            className={buttonVariants({ variant: "ghost", size: "sm" })}
          >
            Add more on GitHub
            <ExternalLink aria-hidden data-icon="inline-end" />
          </a>
        )}
      </div>
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
      {available.isSuccess && available.data.items.length === 0 && (
        <EmptyState
          title={q ? "No repositories match" : "No repositories to connect"}
          description={
            q
              ? "Try another search, or give the GitHub App access to more repositories."
              : "The GitHub App can't read any repositories you can read too. Give it access to more on GitHub."
          }
        />
      )}
      {available.isSuccess && available.data.items.length > 0 && (
        <ul className="flex flex-col divide-y divide-line rounded-lg border border-line">
          {available.data.items.map((repo) => (
            <GitHubRow key={repo.github_repo_id} org={org} repo={repo} />
          ))}
        </ul>
      )}
    </div>
  );
}

function UrlTab({ org }: { org: string }) {
  const id = useId();
  const connected = useConnected(org);
  const [url, setUrl] = useState("");
  const typed = useDebouncedValue(url.trim(), 400);
  const estimate = $api.useQuery(
    "post",
    "/api/v1/orgs/{org}/repos/estimate",
    { params: { path: { org } }, body: { url: typed } },
    { enabled: typed.length > 0, retry: false, staleTime: 60_000 },
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      const { data } = await api.POST("/api/v1/orgs/{org}/repos/import", {
        params: { path: { org } },
        body: { url: typed },
      });
      if (data) connected(data);
    } catch (problem) {
      setError(problem);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (estimate.isSuccess) void connect();
      }}
    >
      <div className="flex flex-col gap-1.5">
        <label htmlFor={id} className="text-sm font-medium">
          Public repository URL
        </label>
        <Input
          id={id}
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder="https://github.com/owner/name"
          value={url}
          onChange={(event) => {
            setUrl(event.target.value);
            setError(null);
          }}
          aria-invalid={estimate.isError || undefined}
          aria-describedby={`${id}-help`}
        />
        <p id={`${id}-help`} className="text-xs text-fg-muted">
          Any public GitHub repository. Private ones connect through the GitHub App.
        </p>
      </div>
      {estimate.isFetching && !estimate.data && (
        <Skeleton aria-label="Estimating" className="h-36" />
      )}
      {estimate.isError && <Problem org={org} error={estimate.error} />}
      {estimate.isSuccess && typed && <EstimateCard estimate={estimate.data} />}
      {error !== null && <Problem org={org} error={error} />}
      <div>
        <Button type="submit" disabled={!estimate.isSuccess || busy}>
          {busy ? <DelayedSpinner active label="Connecting" /> : "Connect and index"}
        </Button>
      </div>
    </form>
  );
}

/** `/[org]/repos/new`: connect from the org's GitHub installations, or import by URL. */
export function ConnectRepo({ org, className }: { org: string; className?: string }) {
  const [tab, setTab] = useQueryState("tab", parseAsStringLiteral(TABS).withDefault("github"));
  return (
    <Tabs
      value={tab}
      onValueChange={(value) => void setTab(value as (typeof TABS)[number])}
      className={cn("flex flex-col gap-4", className)}
    >
      <TabsList>
        <TabsTrigger value="github">GitHub</TabsTrigger>
        <TabsTrigger value="url">Public URL</TabsTrigger>
      </TabsList>
      <TabsContent value="github">
        <GitHubTab org={org} />
      </TabsContent>
      <TabsContent value="url">
        <UrlTab org={org} />
      </TabsContent>
    </Tabs>
  );
}
