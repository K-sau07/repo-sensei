"use client";

import type { Route } from "next";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { DelayedSpinner } from "@/components/primitives/delayed-spinner";
import { useMe } from "@/hooks/use-me";
import { ApiError } from "@/lib/api/errors";
import type { Me } from "@/lib/api/types";
import { orgPath } from "@/lib/routes";
import { safeNext } from "@/lib/security/auth-paths";

/** After sign-in: where they were going, else their last org, else any org, else onboarding. */
export function destinationAfterSignIn(me: Me, next: string | null): Route {
  const target = safeNext(next);
  // A validated same-site path; typed routes can't check runtime strings.
  if (target && target !== "/") return target as Route;
  const lastOrg = me.orgs.find((org) => org.slug === me.last_org_slug) ?? me.orgs[0];
  return lastOrg ? orgPath(lastOrg.slug) : "/onboarding";
}

export function SignInCallback() {
  const router = useRouter();
  const params = useSearchParams();
  const me = useMe();
  const next = params.get("next");

  useEffect(() => {
    if (me.data) router.replace(destinationAfterSignIn(me.data, next));
  }, [me.data, next, router]);

  if (me.isError) {
    const error = me.error as unknown;
    return (
      <ErrorState
        title="Sign-in didn't complete"
        description="We couldn't load your account. Please sign in again."
        {...(error instanceof ApiError && error.requestId ? { requestId: error.requestId } : {})}
        className="w-full max-w-md"
      />
    );
  }
  return (
    <div className="flex flex-col items-center gap-3 text-sm text-fg-muted">
      <DelayedSpinner active label="Signing you in" />
      <p>Signing you in…</p>
    </div>
  );
}
