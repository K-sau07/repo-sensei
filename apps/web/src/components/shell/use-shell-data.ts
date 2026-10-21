"use client";

import { useParams } from "next/navigation";

import { $api } from "@/lib/api/client";

/** The signed-in user (with their orgs) and the org in the URL. */
export function useShellData() {
  const params = useParams<{ org?: string }>();
  const me = $api.useQuery("get", "/api/v1/me");
  const slug = params.org ?? null;
  const currentOrg = me.data?.orgs.find((org) => org.slug === slug) ?? null;
  return { me, slug, currentOrg };
}
