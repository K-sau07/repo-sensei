"use client";

import { cn } from "cn";
import { FolderGit2, PanelLeft, Search } from "lucide-react";
import Link from "next/link";

import { KeyHint } from "@/components/primitives/key-hint";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { orgPath } from "@/lib/routes";
import { shortcut } from "@/lib/shortcut-registry";

import { PRIMARY_NAV, SECONDARY_NAV } from "./nav-items";
import { NavLink } from "./nav-link";
import { OrgSwitcher } from "./org-switcher";
import { useShellStore } from "./shell-store";
import { SidebarResizer } from "./sidebar-resizer";
import { useOrgSlug } from "./use-org-slug";
import { UserMenu } from "./user-menu";

interface SidebarContentProps {
  org: string;
  /** Called after a link is followed (closes the mobile sheet). */
  onNavigate?: () => void;
}

/** What the sidebar shows; shared by the desktop sidebar and the mobile sheet. */
export function SidebarContent({ org, onNavigate }: SidebarContentProps) {
  const setCommandOpen = useShellStore((state) => state.setCommandOpen);
  return (
    <div className="flex h-full min-h-0 flex-col gap-1 p-2">
      <OrgSwitcher />
      <button
        type="button"
        onClick={() => {
          setCommandOpen(true);
        }}
        className="mt-1 flex h-control-md items-center gap-2 rounded-sm border border-line px-2 text-sm text-fg-muted hover:bg-surface-3 hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none in-data-[sidebar=collapsed]:justify-center in-data-[sidebar=collapsed]:px-0"
      >
        <Search aria-hidden className="size-4 shrink-0" />
        <span className="flex-1 text-left in-data-[sidebar=collapsed]:sr-only">Search or ask</span>
        <KeyHint
          shortcut={shortcut("command").keys}
          className="in-data-[sidebar=collapsed]:hidden"
        />
      </button>

      <nav aria-label="Main" className="mt-2 flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto">
        {PRIMARY_NAV.map((item) => (
          <NavLink key={item.label} org={org} item={item} {...(onNavigate ? { onNavigate } : {})} />
        ))}

        <section aria-labelledby="sidebar-repos" className="mt-4 flex flex-col gap-0.5">
          <h2
            id="sidebar-repos"
            className="px-2 pb-1 text-xs font-medium text-fg-faint in-data-[sidebar=collapsed]:sr-only"
          >
            Repositories
          </h2>
          {/* Filled with live index status in P1. */}
          <Link
            href={orgPath(org, "repos")}
            className="flex h-control-md items-center gap-2.5 rounded-sm px-2 text-sm text-fg-faint hover:bg-surface-3 hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none in-data-[sidebar=collapsed]:justify-center in-data-[sidebar=collapsed]:px-0"
          >
            <FolderGit2 aria-hidden className="size-4 shrink-0" />
            <span className="in-data-[sidebar=collapsed]:sr-only">Connect a repository</span>
          </Link>
        </section>

        <div className="mt-auto flex flex-col gap-0.5 pt-4">
          {SECONDARY_NAV.map((item) => (
            <NavLink
              key={item.label}
              org={org}
              item={item}
              {...(onNavigate ? { onNavigate } : {})}
            />
          ))}
        </div>
      </nav>

      <div className="border-t border-line pt-2">
        <UserMenu />
      </div>
    </div>
  );
}

export function Sidebar({ className }: { className?: string }) {
  const org = useOrgSlug();
  const collapsed = useShellStore((state) => state.sidebarCollapsed);
  const toggleSidebar = useShellStore((state) => state.toggleSidebar);
  return (
    <aside
      aria-label="Sidebar"
      className={cn(
        "relative shrink-0 flex-col border-r border-line bg-surface-1",
        "w-(--sidebar-width) in-data-[sidebar=collapsed]:w-14",
        className,
      )}
    >
      <div className="flex h-10 items-center justify-end px-2 in-data-[sidebar=collapsed]:justify-center">
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
                aria-expanded={!collapsed}
                onClick={toggleSidebar}
              />
            }
          >
            <PanelLeft aria-hidden />
          </TooltipTrigger>
          <TooltipContent>
            {collapsed ? "Expand sidebar" : "Collapse sidebar"}
            <KeyHint shortcut={shortcut("toggle-sidebar").keys} />
          </TooltipContent>
        </Tooltip>
      </div>
      <div className="min-h-0 flex-1">
        <SidebarContent org={org} />
      </div>
      <SidebarResizer />
    </aside>
  );
}
