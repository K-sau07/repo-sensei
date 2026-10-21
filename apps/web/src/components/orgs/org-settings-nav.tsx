"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useOrgSlug } from "@/components/shell/use-org-slug";
import { orgPath } from "@/lib/routes";

const ITEMS = [
  { section: "settings/general", label: "General" },
  { section: "settings/members", label: "Members" },
] as const;

export function OrgSettingsNav() {
  const slug = useOrgSlug();
  const pathname = usePathname();
  return (
    <nav aria-label="Organization settings" className="flex flex-row gap-1 md:flex-col">
      {ITEMS.map((item) => {
        const href = orgPath(slug, item.section);
        const active = pathname === href;
        return (
          <Link
            key={item.section}
            href={href}
            aria-current={active ? "page" : undefined}
            className="rounded-sm px-2 py-1.5 text-sm text-fg-muted hover:bg-surface-3 hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none aria-[current=page]:bg-surface-3 aria-[current=page]:text-fg"
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
