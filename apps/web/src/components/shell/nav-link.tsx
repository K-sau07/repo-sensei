"use client";

import { cn } from "cn";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useInboxUnread } from "@/components/inbox/use-inbox";
import { orgPath } from "@/lib/routes";

import type { NavItem } from "./nav-items";

interface NavLinkProps {
  org: string;
  item: NavItem;
  onNavigate?: () => void;
}

export function NavLink({ org, item, onNavigate }: NavLinkProps) {
  const pathname = usePathname();
  const href = orgPath(org, item.section);
  // Home matches exactly; sections also match their sub-pages.
  const active = item.section === "" ? pathname === href : pathname.startsWith(href);
  // The inbox shows how much is unread.
  const unread = useInboxUnread(item.section === "inbox" ? org : null).data ?? 0;
  const Icon = item.icon;
  return (
    <Link
      href={href}
      {...(onNavigate ? { onClick: onNavigate } : {})}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex h-control-md items-center gap-2.5 rounded-sm px-2 text-sm text-fg-muted",
        "transition-[color,background-color] duration-(--dur-fast) ease-out",
        "hover:bg-surface-3 hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
        "aria-[current=page]:bg-surface-3 aria-[current=page]:text-fg",
        "sidebar-collapsed:justify-center sidebar-collapsed:px-0",
      )}
    >
      <span className="relative shrink-0">
        <Icon aria-hidden className="size-4" />
        {unread > 0 && (
          <span
            aria-hidden
            className="absolute -top-0.5 -right-0.5 hidden size-1.5 rounded-full bg-signal sidebar-collapsed:block"
          />
        )}
      </span>
      <span className="truncate sidebar-collapsed:sr-only">{item.label}</span>
      {unread > 0 && (
        <span
          className="ml-auto rounded-full bg-surface-3 px-1.5 text-xs text-fg tabular-nums sidebar-collapsed:sr-only"
          data-testid="inbox-badge"
        >
          {unread > 99 ? "99+" : unread}
          <span className="sr-only"> unread</span>
        </span>
      )}
    </Link>
  );
}
