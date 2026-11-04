/** The public site's footer (10.3): the same links as the header, and where the code is. */
import Link from "next/link";

import { SITE_LINKS } from "./site-header";

export function SiteFooter() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-4 px-6 py-8 text-sm text-fg-muted">
        <p>
          <span className="font-mono text-fg">repo-sensei</span>: understand any codebase, with
          answers you can verify.
        </p>
        <nav aria-label="Footer">
          <ul className="flex flex-wrap items-center gap-5">
            {SITE_LINKS.map((link) => (
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
