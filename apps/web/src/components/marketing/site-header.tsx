/**
 * The public site's header (10.3): the name home, the public pages, and sign-in. Server-rendered
 * and the same for everyone, so the landing page stays static; signing in decides the rest.
 */
import type { Route } from "next";
import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";

export const SITE_LINKS: readonly { href: Route; label: string }[] = [
  { href: "/explore", label: "Explore" },
];

export function SiteHeader() {
  return (
    <header className="sticky top-0 z-40 border-b border-line bg-bg/85 backdrop-blur supports-[backdrop-filter]:bg-bg/70">
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center gap-6 px-6">
        <Link href="/" className="font-mono text-sm font-medium tracking-tight">
          repo-sensei
        </Link>
        <nav aria-label="Site" className="hidden flex-1 sm:block">
          <ul className="flex items-center gap-5 text-sm text-fg-muted">
            {SITE_LINKS.map((link) => (
              <li key={link.href}>
                <Link href={link.href} className="hover:text-fg">
                  {link.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <div className="ml-auto flex items-center gap-2">
          <Link href="/login" className={buttonVariants({ variant: "ghost", size: "sm" })}>
            Sign in
          </Link>
          <Link href="/login" className={buttonVariants({ size: "sm" })}>
            Get started
          </Link>
        </div>
      </div>
    </header>
  );
}
