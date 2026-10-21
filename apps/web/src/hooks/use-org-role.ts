import { useOrgSlug } from "@/components/shell/use-org-slug";
import type { Role } from "@/lib/api/types";

import { useMe } from "./use-me";

const RANK: Record<Role, number> = { viewer: 0, member: 1, admin: 2, owner: 3 };

export function roleAtLeast(role: Role | null | undefined, minimum: Role): boolean {
  return role !== null && role !== undefined && RANK[role] >= RANK[minimum];
}

/** The signed-in user's role in the org in the URL (call inside a <Suspense>). */
export function useOrgRole() {
  const slug = useOrgSlug();
  const me = useMe();
  const org = me.data?.orgs.find((o) => o.slug === slug) ?? null;
  return { slug, org, role: org?.role ?? null, me };
}
