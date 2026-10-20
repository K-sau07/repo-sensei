"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";

import { PermissionState } from "@/components/feedback/permission-state";
import { useOrgSlug } from "@/components/shell/use-org-slug";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { orgPath } from "@/lib/routes";

import { ConnectRepo } from "./connect-repo";
import { RepoList } from "./repo-list";

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
