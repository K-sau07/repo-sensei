"use client";

/** The staff pages' header: the evals dashboard and the admin console. Staff only: to anyone
 * else the pages aren't there, nav included. */
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useMe } from "@/hooks/use-me";

const ITEMS = [
  { href: "/internal", label: "Evals" },
  { href: "/internal/admin", label: "Admin" },
] as const;

export function InternalNav() {
  const pathname = usePathname();
  const me = useMe();
  if (!me.data?.is_staff) return null;
  return (
    <nav aria-label="Staff" className="flex items-center gap-1 border-b border-line px-6 py-2">
      <span className="mr-3 text-sm font-medium">repo-sensei staff</span>
      {ITEMS.map((item) => {
        const active =
          item.href === "/internal"
            ? pathname === "/internal" || pathname.startsWith("/internal/evals")
            : pathname.startsWith(item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className="rounded-sm px-2 py-1 text-sm text-fg-muted hover:bg-surface-3 hover:text-fg aria-[current=page]:bg-surface-3 aria-[current=page]:text-fg"
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
