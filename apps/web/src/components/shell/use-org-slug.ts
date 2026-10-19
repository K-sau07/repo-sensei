"use client";

import { useParams } from "next/navigation";

/** The org in the URL. Reading it is request data, so callers sit inside a <Suspense>. */
export function useOrgSlug(): string {
  const { org } = useParams<{ org: string }>();
  return org;
}
