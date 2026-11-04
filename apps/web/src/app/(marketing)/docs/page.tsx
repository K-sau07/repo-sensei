import type { Metadata } from "next";
import Link from "next/link";

import { DOC_SECTIONS, DOCS, docPath } from "@/content/docs-index";

const DESCRIPTION =
  "How to connect repositories, ask, trace, and use repo-sensei from your editor.";

export const metadata: Metadata = {
  title: "Docs",
  description: DESCRIPTION,
  alternates: { canonical: "/docs" },
  openGraph: { title: "Docs · repo-sensei", description: DESCRIPTION, url: "/docs" },
};

export default function DocsIndexPage() {
  return (
    <div className="flex flex-col gap-8">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight">Docs</h1>
        <p className="text-fg-muted">{DESCRIPTION}</p>
      </header>
      {DOC_SECTIONS.map((section) => (
        <section key={section} aria-labelledby={`docs-${section}`} className="flex flex-col gap-3">
          <h2 id={`docs-${section}`} className="text-sm font-medium text-fg-muted">
            {section}
          </h2>
          <ul className="grid gap-3 sm:grid-cols-2">
            {DOCS.filter((doc) => doc.section === section).map((doc) => (
              <li key={doc.slug}>
                <Link
                  href={docPath(doc.slug)}
                  className="flex h-full flex-col gap-1 rounded-lg border border-line p-4 hover:border-line-strong hover:bg-muted/40"
                >
                  <span className="font-medium">{doc.title}</span>
                  <span className="text-sm text-fg-muted">{doc.description}</span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
