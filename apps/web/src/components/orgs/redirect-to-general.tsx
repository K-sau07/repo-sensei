"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useOrgSlug } from "@/components/shell/use-org-slug";
import { orgPath } from "@/lib/routes";

export function RedirectToGeneral() {
  const slug = useOrgSlug();
  const router = useRouter();
  useEffect(() => {
    router.replace(orgPath(slug, "settings/general"));
  }, [router, slug]);
  return null;
}
