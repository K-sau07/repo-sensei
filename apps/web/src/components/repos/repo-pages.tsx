"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { PermissionState } from "@/components/feedback/permission-state";
import { RelativeTime } from "@/components/primitives/relative-time";
import { useOrgSlug } from "@/components/shell/use-org-slug";
import { Progress } from "@/components/ui/progress";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api } from "@/lib/api/client";
import { orgPath } from "@/lib/routes";

import { ConnectRepo } from "./connect-repo";
import { LanguageBar } from "./language-bar";
import { RepoList } from "./repo-list";
import { describeStatus } from "./repo-status";

export function RepoListPage() {
  return <RepoList org={useOrgSlug()} />;
}

/** `/[org]/repos/new` (admins). */
export function ConnectRepoPage() {
  const org = useOrgSlug();
  const { role, org: current } = useOrgRole();
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-8">
      <Link
        href={orgPath(org, "repos")}
        className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted hover:text-fg"
      >
        <ArrowLeft aria-hidden className="size-4" />
        Repositories
      </Link>
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-[-0.015em]">Connect a repository</h1>
        <p className="text-sm text-fg-muted">
          From your GitHub App installation, or any public repository by URL. Indexing starts as
          soon as it is connected.
        </p>
      </header>
      {current && !roleAtLeast(role, "admin") ? (
        <PermissionState requiredRole="admin" {...(role ? { yourRole: role } : {})} />
      ) : (
        <ConnectRepo org={org} />
      )}
    </div>
  );
}

/** The overview tab until the live indexing view (1.13): progress while indexing, then the
 * repository's numbers. The header and tabs come from the repository layout. */
export function RepoLandingPage() {
  const { org, repo: slug } = useRepoParams();
  const repo = $api.useQuery("get", "/api/v1/orgs/{org}/repos/{repo}", {
    params: { path: { org, repo: slug } },
  });
  if (!repo.data) return null; // the layout shows loading and errors
  const data = repo.data;
  const job = data.active_job;
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 p-6">
      {job && (
        <section
          aria-label="Indexing"
          className="flex flex-col gap-2 rounded-lg border border-line p-4"
        >
          <div className="flex items-center justify-between text-sm">
            <span>{describeStatus(data).label}</span>
            {job.stage && <span className="text-fg-muted">Stage: {job.stage}</span>}
          </div>
          <Progress value={Math.round(job.progress * 100)} aria-label="Indexing progress" />
        </section>
      )}
      <LanguageBar languages={data.languages} legend />
      {data.last_indexed_at && (
        <p className="text-sm text-fg-muted">
          Last indexed <RelativeTime date={data.last_indexed_at} />
          {data.file_count !== null && data.file_count !== undefined && (
            <> · {data.file_count.toLocaleString("en")} files</>
          )}
        </p>
      )}
    </div>
  );
}

function useRepoParams() {
  const org = useOrgSlug();
  const { repo } = useParams<{ repo: string }>();
  return { org, repo };
}
