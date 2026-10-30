"use client";

/**
 * `/[org]`: what a member sees first (4.13): a question to ask of any of their repositories, the
 * agents running in them now, what they asked last, and the repositories themselves. There is
 * no inbox yet (nothing is recorded as waiting on anyone), so none is shown.
 */
import { Bot, FolderGit2, MessageSquare, Send } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { askHref } from "@/components/ask/ask-bar";
import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { RelativeTime } from "@/components/primitives/relative-time";
import { LanguageBar } from "@/components/repos/language-bar";
import { RepoStatus } from "@/components/repos/repo-status";
import { useOrgSlug } from "@/components/shell/use-org-slug";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { Repo } from "@/lib/api/types";
import { orgPath, repoPath } from "@/lib/routes";

const SHOWN = 6;

function AskAnything({ org, repos }: { org: string; repos: Repo[] }) {
  const router = useRouter();
  const askable = repos.filter((r) => r.last_indexed_sha);
  const [picked, setPicked] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const slug = picked ?? askable[0]?.slug ?? null;
  const chosen = askable.find((r) => r.slug === slug);
  if (askable.length === 0) return null;
  return (
    <form
      aria-label="Ask anything"
      className="flex flex-col gap-2 rounded-lg border border-line p-4 sm:flex-row sm:items-center"
      onSubmit={(event) => {
        event.preventDefault();
        const text = question.trim();
        if (!text || !slug) return;
        router.push(askHref(org, slug, text, null));
      }}
    >
      <Select
        value={slug}
        onValueChange={(value) => {
          if (typeof value === "string") setPicked(value);
        }}
      >
        <SelectTrigger aria-label="Repository to ask" className="w-full sm:w-48">
          <SelectValue>{chosen?.name ?? "Repository"}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          {askable.map((r) => (
            <SelectItem key={r.id} value={r.slug}>
              {r.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Input
        aria-label="Question"
        placeholder={`Ask anything about ${chosen?.name ?? "a repository"}…`}
        value={question}
        onChange={(event) => {
          setQuestion(event.target.value);
        }}
        className="flex-1"
      />
      <Button type="submit" disabled={!question.trim()}>
        <Send aria-hidden data-icon="inline-start" />
        Ask
      </Button>
    </form>
  );
}

export function OrgHome() {
  const slug = useOrgSlug();
  const { org, role } = useOrgRole();
  const home = $api.useQuery("get", "/api/v1/orgs/{org}/home", {
    params: { path: { org: slug } },
  });
  const admin = roleAtLeast(role, "admin");
  const list = home.data?.repos ?? [];
  const byId = new Map(list.map((r) => [r.id, r]));
  const live = home.data?.live ?? [];
  const recent = home.data?.recent ?? [];
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 p-8">
      <h1 className="text-2xl font-semibold tracking-[-0.015em]">{org?.name ?? "Home"}</h1>
      {home.isPending && <Skeleton className="h-32" />}
      {home.isError && (
        <ErrorState
          title="Couldn't load your repositories"
          description={home.error instanceof ApiError ? home.error.message : undefined}
          onRetry={() => void home.refetch()}
        />
      )}
      {home.isSuccess && list.length === 0 && (
        <EmptyState
          icon={<FolderGit2 />}
          title="Connect your first repository"
          description="repo-sensei indexes a repository so you can browse it, ask questions, trace requests across layers and get fixes with citations."
          action={
            admin ? (
              <Link href={orgPath(slug, "repos/new")} className={buttonVariants()}>
                Connect a repository
              </Link>
            ) : (
              <p className="text-sm text-fg-muted">
                An admin of this organization can connect one.
              </p>
            )
          }
        />
      )}
      {list.length > 0 && <AskAnything org={slug} repos={list} />}
      {live.length > 0 && (
        <section aria-labelledby="home-live" className="flex flex-col gap-3">
          <h2 id="home-live" className="text-sm font-medium text-fg-muted">
            Running now
          </h2>
          <ul className="flex flex-col divide-y divide-line rounded-lg border border-line">
            {live.map((run) => {
              const repo = byId.get(run.repo_id);
              if (!repo) return null;
              return (
                <li key={run.id}>
                  <Link
                    href={repoPath(slug, repo.slug, `runs/${run.id}`)}
                    className="flex items-center gap-3 p-3 text-sm hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                  >
                    <span className="relative flex size-2 shrink-0">
                      <span className="absolute inline-flex size-full animate-ping rounded-full bg-signal opacity-60 motion-reduce:animate-none" />
                      <span className="relative inline-flex size-2 rounded-full bg-signal" />
                    </span>
                    <Bot aria-hidden className="size-4 shrink-0 text-fg-muted" />
                    <span className="min-w-0 flex-1 truncate">{run.question || run.kind}</span>
                    <span className="shrink-0 text-xs text-fg-muted">
                      {repo.name}
                      {run.user ? ` · ${run.user}` : ""} · <RelativeTime date={run.created_at} />
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {recent.length > 0 && (
        <section aria-labelledby="home-recent" className="flex flex-col gap-3">
          <h2 id="home-recent" className="text-sm font-medium text-fg-muted">
            Your recent questions
          </h2>
          <ul className="flex flex-col divide-y divide-line rounded-lg border border-line">
            {recent.map((c) => {
              const repo = byId.get(c.repo_id);
              if (!repo) return null;
              return (
                <li key={c.id}>
                  <Link
                    href={repoPath(slug, repo.slug, `ask/${c.id}`)}
                    className="flex items-center gap-3 p-3 text-sm hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                  >
                    <MessageSquare aria-hidden className="size-4 shrink-0 text-fg-muted" />
                    <span className="min-w-0 flex-1 truncate">{c.title || "Untitled"}</span>
                    <span className="shrink-0 text-xs text-fg-muted">
                      {repo.name} · <RelativeTime date={c.updated_at} />
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {list.length > 0 && (
        <section aria-labelledby="home-repos" className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h2 id="home-repos" className="text-sm font-medium text-fg-muted">
              Your repositories
            </h2>
            <Link href={orgPath(slug, "repos")} className="text-sm underline underline-offset-4">
              All repositories
            </Link>
          </div>
          <ul className="grid gap-3 sm:grid-cols-2">
            {list.slice(0, SHOWN).map((repo) => (
              <li key={repo.id}>
                <Link
                  href={repoPath(slug, repo.slug)}
                  className="flex flex-col gap-3 rounded-lg border border-line p-4 hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate font-medium">{repo.name}</span>
                    <RepoStatus repo={repo} showLabel />
                  </span>
                  <LanguageBar languages={repo.languages} />
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
