"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useMe } from "@/hooks/use-me";
import { orgPath } from "@/lib/routes";

const ITEMS = [
  { href: "/settings/profile", label: "Profile" },
  { href: "/settings/sessions", label: "Sessions" },
] as const;

/** Personal settings sit outside any org; this header leads back to the last one. */
export function SettingsNav() {
  const pathname = usePathname();
  const me = useMe();
  const back = me.data?.orgs.find((org) => org.slug === me.data.last_org_slug) ?? me.data?.orgs[0];
  return (
    <header className="border-b border-line bg-surface-1">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-6 pt-4">
        <Link
          href={back ? orgPath(back.slug) : "/onboarding"}
          className="inline-flex w-fit items-center gap-1.5 rounded-sm text-sm text-fg-muted hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
        >
          <ArrowLeft aria-hidden className="size-4" />
          {back ? `Back to ${back.name}` : "Back"}
        </Link>
        <h1 className="text-xl font-semibold tracking-[-0.015em]">Your settings</h1>
        <nav aria-label="Settings" className="flex gap-4">
          {ITEMS.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className="-mb-px border-b-2 border-transparent pb-2 text-sm text-fg-muted hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none aria-[current=page]:border-fg aria-[current=page]:text-fg"
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
