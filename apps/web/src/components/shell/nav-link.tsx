"use client";

import { cn } from "cn";
import Link from "next/link";
import { usePathname } from "next/navigation";

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
        "in-data-[sidebar=collapsed]:justify-center in-data-[sidebar=collapsed]:px-0",
      )}
    >
      <Icon aria-hidden className="size-4 shrink-0" />
      <span className="truncate in-data-[sidebar=collapsed]:sr-only">{item.label}</span>
    </Link>
  );
}
