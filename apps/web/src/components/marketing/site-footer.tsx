/** The public site's footer (10.3, 10.5): the header's links, the rest of the public pages,
 * and where the code is. */
import type { Route } from "next";
import Link from "next/link";

import { Brand } from "@/components/brand/logo";

import { SITE_LINKS } from "./site-header";

const MORE: readonly { href: Route; label: string }[] = [
  { href: "/blog", label: "Blog" },
  { href: "/changelog", label: "Changelog" },
  { href: "/security", label: "Security" },
  { href: "/privacy", label: "Privacy" },
  { href: "/terms", label: "Terms" },
];

export function SiteFooter() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-4 px-6 py-8 text-sm text-fg-muted">
        <div className="flex flex-col gap-2">
          <Brand />
          <p>Understand any codebase, with answers you can verify.</p>
        </div>
        <nav aria-label="Footer">
          <ul className="flex flex-wrap items-center gap-5">
            {[...SITE_LINKS, ...MORE].map((link) => (
              <li key={link.href}>
                <Link href={link.href} className="hover:text-fg">
                  {link.label}
                </Link>
              </li>
            ))}
            <li>
              <a
                href="https://github.com/K-sau07/repo-sensei"
                className="hover:text-fg"
                rel="noreferrer noopener"
              >
                GitHub
              </a>
            </li>
          </ul>
        </nav>
      </div>
    </footer>
  );
}
