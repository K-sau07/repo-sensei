"use client";

import { cn } from "cn";
import { House, Inbox, Menu, MessageSquare } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { orgPath } from "@/lib/routes";

import { useShellStore } from "./shell-store";
import { SidebarContent } from "./sidebar";
import { useOrgSlug } from "./use-org-slug";
import { useShellData } from "./use-shell-data";

/** Below `lg`: a top bar that opens the sidebar as a sheet. */
export function MobileTopBar({ className }: { className?: string }) {
  const org = useOrgSlug();
  const open = useShellStore((state) => state.mobileNavOpen);
  const setOpen = useShellStore((state) => state.setMobileNavOpen);
  const { currentOrg } = useShellData();
  return (
    <header
      className={cn(
        "flex h-12 items-center gap-2 border-b border-line bg-surface-1 px-2",
        className,
      )}
    >
      <Button
        variant="ghost"
        size="icon"
        aria-label="Open navigation"
        onClick={() => {
          setOpen(true);
        }}
      >
        <Menu aria-hidden />
      </Button>
      <span className="truncate text-sm font-medium">{currentOrg?.name ?? org}</span>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent
          side="left"
          className="w-72 p-0"
          showCloseButton={false}
          data-sidebar="expanded"
        >
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <SidebarContent
            org={org}
            onNavigate={() => {
              setOpen(false);
            }}
          />
        </SheetContent>
      </Sheet>
    </header>
  );
}

/** Below `lg`: the three things people do on a phone. */
export function MobileBottomBar({ className }: { className?: string }) {
  const org = useOrgSlug();
  const setCommandOpen = useShellStore((state) => state.setCommandOpen);
  const item =
    "flex flex-1 flex-col items-center justify-center gap-0.5 text-xs text-fg-muted hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none";
  return (
    <nav
      aria-label="Quick"
      className={cn(
        "sticky bottom-0 flex h-14 border-t border-line bg-surface-1 pb-[env(safe-area-inset-bottom)]",
        className,
      )}
    >
      <Link href={orgPath(org)} className={item}>
        <House aria-hidden className="size-5" />
        Home
      </Link>
      <Link href={orgPath(org, "inbox")} className={item}>
        <Inbox aria-hidden className="size-5" />
        Inbox
      </Link>
      <button
        type="button"
        className={item}
        onClick={() => {
          setCommandOpen(true);
        }}
      >
        <MessageSquare aria-hidden className="size-5" />
        Ask
      </button>
    </nav>
  );
}
