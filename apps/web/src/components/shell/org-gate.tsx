"use client";

import { SearchX } from "lucide-react";
import Link from "next/link";
import { Suspense, type ReactNode } from "react";

import { EmptyState } from "@/components/feedback/empty-state";
import { buttonVariants } from "@/components/ui/button";
import { useMe } from "@/hooks/use-me";

import { useOrgSlug } from "./use-org-slug";

function OrgNotFound() {
  return (
    <div className="mx-auto w-full max-w-xl p-8">
      <EmptyState
        icon={<SearchX />}
        title="Organization not found"
        headingLevel={1}
        description="It doesn't exist, or you don't have access to it. Ask an admin of the organization for an invitation."
        action={
          <Link href="/auth/callback" className={buttonVariants({ variant: "outline" })}>
            Go to your organizations
          </Link>
        }
      />
    </div>
  );
}

function Gate({ children }: { children: ReactNode }) {
  const slug = useOrgSlug();
  const me = useMe();
  // Same answer whether the org is missing or private, so its existence never leaks.
  if (me.data && !me.data.orgs.some((org) => org.slug === slug)) return <OrgNotFound />;
  return children;
}

/**
 * Shows "not found" instead of the page when the user isn't a member of the org in the URL.
 * The page itself is the fallback, so it stays part of the static shell (instant navigation).
 */
export function OrgGate({ children }: { children: ReactNode }) {
  return (
    <Suspense fallback={children}>
      <Gate>{children}</Gate>
    </Suspense>
  );
}
