"use client";

/**
 * The org's GitHub App installations (10.7.2): each account it is installed on, which of its
 * repositories it may read, whether GitHub has suspended it, and a link to change that on
 * GitHub; and installing it on another account (admins), which comes back here.
 */
import { ExternalLink } from "lucide-react";

import { GitHubMark } from "@/components/auth/github-mark";
import { ErrorState } from "@/components/feedback/error-state";
import { installUrl } from "@/components/repos/connect-repo";
import { InstallResult } from "@/components/repos/install-result";
import { buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api } from "@/lib/api/client";

const DAY = new Intl.DateTimeFormat("en", { dateStyle: "medium" });

export function GitHubInstallations() {
  const { slug, role } = useOrgRole();
  const admin = roleAtLeast(role, "admin");
  const installations = $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/github/installations",
    { params: { path: { org: slug } } },
    { enabled: role !== null },
  );
  return (
    <section aria-labelledby="github-heading" className="flex flex-col gap-4">
      <div>
        <h2 id="github-heading" className="flex items-center gap-1.5 text-lg font-semibold">
          <GitHubMark className="size-4" />
          GitHub
        </h2>
        <p className="text-sm text-fg-muted">
          The accounts the repo-sensei GitHub App is installed on: what it may read is chosen on
          GitHub.
        </p>
      </div>
      <InstallResult />
      {installations.isError ? (
        <ErrorState
          title="Couldn't load the installations"
          onRetry={() => void installations.refetch()}
        />
      ) : !installations.data ? (
        <Skeleton className="h-16" role="status" aria-label="Loading the installations" />
      ) : installations.data.length === 0 ? (
        <p className="text-sm text-fg-faint">Not installed on any account yet.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-line rounded-lg border border-line">
          {installations.data.map((found) => (
            <li key={found.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3">
              <span className="font-medium">{found.account_login}</span>
              <span className="text-xs text-fg-muted">
                {found.account_type === "Organization" ? "organization" : "personal account"} ·{" "}
                {found.repository_selection === "all" ? "every repository" : "chosen repositories"}{" "}
                · since {DAY.format(new Date(found.created_at))}
              </span>
              {found.suspended && (
                <span className="rounded-sm border border-danger/40 px-1.5 text-xs text-danger">
                  suspended on GitHub
                </span>
              )}
              {admin && (
                <a
                  href={found.manage_url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="ml-auto inline-flex items-center gap-1 text-sm text-fg-muted hover:text-fg"
                >
                  Change on GitHub <ExternalLink aria-hidden className="size-3.5" />
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
      {admin && (
        <div>
          <a
            href={installUrl(slug, "integrations")}
            className={buttonVariants({ variant: "outline" })}
          >
            <GitHubMark className="size-4" />
            {installations.data?.length ? "Install on another account" : "Install the GitHub App"}
          </a>
        </div>
      )}
    </section>
  );
}
