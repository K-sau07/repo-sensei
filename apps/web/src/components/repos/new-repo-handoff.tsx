"use client";

/**
 * `/new?url=…` (10.3): a repository URL from the landing page, handed to where it is indexed:
 * the URL tab of the last organization's Connect page, filled in, or onboarding (with the URL,
 * for after the organization exists) for someone with none. Signed out, sign-in comes first.
 */
import type { Route } from "next";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { DelayedSpinner } from "@/components/primitives/delayed-spinner";
import { useMe } from "@/hooks/use-me";
import type { Me } from "@/lib/api/types";
import { orgPath } from "@/lib/routes";

/** Where a URL goes to be connected, for this person. */
export function handoffPath(me: Me, url: string | null): Route {
  const lastOrg = me.orgs.find((org) => org.slug === me.last_org_slug) ?? me.orgs[0];
  const query = url ? `&url=${encodeURIComponent(url)}` : "";
  if (!lastOrg) return url ? `/onboarding?url=${encodeURIComponent(url)}` : "/onboarding";
  return `${orgPath(lastOrg.slug, "repos/new")}?tab=url${query}` as Route;
}

export function NewRepoHandoff() {
  const router = useRouter();
  const params = useSearchParams();
  const me = useMe();
  const url = params.get("url");

  useEffect(() => {
    if (me.data) router.replace(handoffPath(me.data, url));
  }, [me.data, url, router]);

  if (me.isError) {
    return (
      <ErrorState
        title="Couldn't load your account"
        onRetry={() => void me.refetch()}
        className="m-auto w-full max-w-md"
      />
    );
  }
  return (
    <div className="m-auto flex flex-col items-center gap-3 text-sm text-fg-muted">
      <DelayedSpinner active label="Opening" />
    </div>
  );
}
