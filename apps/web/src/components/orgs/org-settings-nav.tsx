"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useOrgSlug } from "@/components/shell/use-org-slug";
import { useCurrentInView } from "@/hooks/use-current-in-view";
import { orgPath } from "@/lib/routes";

const ITEMS = [
  { section: "settings/general", label: "General" },
  { section: "settings/members", label: "Members" },
  { section: "settings/integrations", label: "Integrations" },
  { section: "settings/api-keys", label: "API keys" },
  { section: "settings/mcp", label: "Editors (MCP)" },
  { section: "settings/models", label: "Models" },
  { section: "settings/usage", label: "Usage" },
  { section: "settings/security", label: "Security" },
  { section: "settings/danger", label: "Danger zone" },
] as const;

export function OrgSettingsNav() {
  const slug = useOrgSlug();
  const pathname = usePathname();
  const row = useCurrentInView(pathname);
  return (
    <nav
      ref={row}
      aria-label="Organization settings"
      className="-mx-1 flex flex-row gap-1 overflow-x-auto px-1 md:mx-0 md:flex-col md:overflow-visible md:px-0"
    >
      {ITEMS.map((item) => {
        const href = orgPath(slug, item.section);
        const active = pathname === href;
        return (
          <Link
            key={item.section}
            href={href}
            aria-current={active ? "page" : undefined}
            className="shrink-0 rounded-sm px-2 py-1.5 text-sm whitespace-nowrap text-fg-muted hover:bg-surface-3 hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none focus-visible:ring-inset aria-[current=page]:bg-surface-3 aria-[current=page]:text-fg"
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
