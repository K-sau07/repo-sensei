"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { api } from "@/lib/api/client";
import { queryKeys } from "@/lib/query/keys";

import { useOrgSlug } from "./use-org-slug";
import { useShellData } from "./use-shell-data";

/** Opening an org makes it the one sign-in returns to next time. */
export function RememberLastOrg() {
  const slug = useOrgSlug();
  const queryClient = useQueryClient();
  const { me } = useShellData();
  const isMember = me.data?.orgs.some((org) => org.slug === slug) ?? false;
  const remembered = me.data?.last_org_slug;

  useEffect(() => {
    if (!isMember || remembered === slug) return;
    void api
      .PATCH("/api/v1/me", { body: { last_org_slug: slug } })
      .then(({ data }) => {
        if (data) queryClient.setQueryData(queryKeys.me(), data);
      })
      .catch(() => {
        // Only a preference; the next visit tries again.
      });
  }, [isMember, remembered, slug, queryClient]);

  return null;
}
