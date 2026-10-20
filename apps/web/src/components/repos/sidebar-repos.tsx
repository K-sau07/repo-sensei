"use client";

import { FolderGit2, Plus } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api } from "@/lib/api/client";
import { orgPath, repoPath } from "@/lib/routes";

import { RepoStatus } from "./repo-status";

const SHOWN = 8;
const ROW =
  "flex h-control-md items-center gap-2.5 rounded-sm px-2 text-sm hover:bg-surface-3 hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none sidebar-collapsed:justify-center sidebar-collapsed:px-0";

/** The sidebar's repositories, each with its live status dot. */
export function SidebarRepos({ org, onNavigate }: { org: string; onNavigate?: () => void }) {
  const pathname = usePathname();
  const { role } = useOrgRole();
  const repos = $api.useQuery("get", "/api/v1/orgs/{org}/repos", {
    params: { path: { org } },
  });
  const list = repos.data ?? [];
  const navigate = () => onNavigate?.();
  return (
    <ul className="flex flex-col gap-0.5">
      {list.slice(0, SHOWN).map((repo) => {
        const href = repoPath(org, repo.slug);
        const active = pathname === href || pathname.startsWith(`${href}/`);
        return (
          <li key={repo.id}>
            <Link
              href={href}
              onClick={navigate}
              aria-current={active ? "page" : undefined}
              className={`${ROW} ${active ? "bg-surface-3 text-fg" : "text-fg-muted"}`}
            >
              <RepoStatus repo={repo} className="w-4 shrink-0 justify-center" />
              <span className="min-w-0 flex-1 truncate sidebar-collapsed:sr-only">{repo.name}</span>
            </Link>
          </li>
        );
      })}
      {list.length > SHOWN && (
        <li>
          <Link href={orgPath(org, "repos")} onClick={navigate} className={`${ROW} text-fg-faint`}>
            <FolderGit2 aria-hidden className="size-4 shrink-0" />
            <span className="sidebar-collapsed:sr-only">All {list.length} repositories</span>
          </Link>
        </li>
      )}
      {repos.isSuccess && list.length === 0 && (
        <li>
          <Link href={orgPath(org, "repos")} onClick={navigate} className={`${ROW} text-fg-faint`}>
            <FolderGit2 aria-hidden className="size-4 shrink-0" />
            <span className="sidebar-collapsed:sr-only">
              {roleAtLeast(role, "admin") ? "Connect a repository" : "No repositories yet"}
            </span>
          </Link>
        </li>
      )}
      {repos.isSuccess && list.length > 0 && roleAtLeast(role, "admin") && (
        <li>
          <Link
            href={orgPath(org, "repos/new")}
            onClick={navigate}
            className={`${ROW} text-fg-faint`}
          >
            <Plus aria-hidden className="size-4 shrink-0" />
            <span className="sidebar-collapsed:sr-only">Connect a repository</span>
          </Link>
        </li>
      )}
    </ul>
  );
}
