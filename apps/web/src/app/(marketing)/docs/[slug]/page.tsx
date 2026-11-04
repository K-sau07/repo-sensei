import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { DOCS, docBySlug, docPath } from "@/content/docs-index";
import { DOC_PAGES } from "@/content/docs-pages";

type Props = PageProps<"/docs/[slug]">;

// Each page is known at build (generateStaticParams), and one that isn't a doc answers 404, not
// a page that turns out empty.
export const instant = false;

export function generateStaticParams() {
  return DOCS.map((doc) => ({ slug: doc.slug }));
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const doc = docBySlug((await params).slug);
  if (!doc) return { title: "Not found", robots: { index: false } };
  return {
    title: doc.title,
    description: doc.description,
    alternates: { canonical: docPath(doc.slug) },
    openGraph: { title: `${doc.title} · repo-sensei docs`, description: doc.description },
  };
}

export default async function DocPage({ params }: Props) {
  const { slug } = await params;
  const doc = docBySlug(slug);
  const load = DOC_PAGES[slug];
  if (!doc || !load) notFound();
  const { default: Content } = await load();
  const at = DOCS.indexOf(doc);
  const previous = DOCS[at - 1];
  const next = DOCS[at + 1];
  return (
    <article className="flex flex-col gap-8">
      <header className="flex flex-col gap-2">
        <p className="text-sm text-fg-muted">{doc.section}</p>
        <h1 className="text-3xl font-semibold tracking-tight">{doc.title}</h1>
        <p className="text-lg text-fg-muted">{doc.description}</p>
      </header>
      <div className="prose-sensei">
        <Content />
      </div>
      <nav
        aria-label="More docs"
        className="flex justify-between gap-4 border-t border-line pt-6 text-sm"
      >
        {previous ? (
          <Link href={docPath(previous.slug)} className="text-fg-muted hover:text-fg">
            ← {previous.title}
          </Link>
        ) : (
          <span />
        )}
        {next && (
          <Link href={docPath(next.slug)} className="text-fg-muted hover:text-fg">
            {next.title} →
          </Link>
        )}
      </nav>
    </article>
  );
}
