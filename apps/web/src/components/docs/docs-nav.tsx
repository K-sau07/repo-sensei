"use client";

/** The docs' sidebar (10.5): each page by section, the one open marked. */
import { cn } from "cn";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { DOC_SECTIONS, DOCS, docPath } from "@/content/docs-index";

export function DocsNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Docs" className="flex flex-col gap-5 text-sm">
      {DOC_SECTIONS.map((section) => (
        <div key={section} className="flex flex-col gap-1.5">
          <p className="text-xs font-medium text-fg-faint">{section}</p>
          <ul className="flex flex-col gap-0.5">
            {DOCS.filter((doc) => doc.section === section).map((doc) => {
              const href = docPath(doc.slug);
              const current = pathname === href;
              return (
                <li key={doc.slug}>
                  <Link
                    href={href}
                    aria-current={current ? "page" : undefined}
                    className={cn(
                      "block rounded-md px-2 py-1 hover:bg-muted hover:text-fg",
                      current ? "bg-muted font-medium text-fg" : "text-fg-muted",
                    )}
                  >
                    {doc.title}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}
