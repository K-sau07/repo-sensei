/**
 * A product page's frame (10.3): what the feature is for, the points that make it trustworthy,
 * an example made by the real engine (or the live product), and where to go next.
 */
import type { Route } from "next";
import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";

export interface ProductLink {
  href: Route;
  title: string;
  summary: string;
}

/** The product pages, in the order they are read. */
export const PRODUCT_PAGES: readonly ProductLink[] = [
  {
    href: "/product/understand",
    title: "Understand",
    summary: "Ask about the code; each claim cites the lines it stands on.",
  },
  {
    href: "/product/trace",
    title: "Trace",
    summary: "Follow a feature across UI, API, services and storage.",
  },
  {
    href: "/product/diagrams",
    title: "Diagrams",
    summary: "Architecture drawn from the code's graph, each edge checked.",
  },
  {
    href: "/product/fix",
    title: "Fix",
    summary: "From a stack trace to a patch the tests prove, with your approval.",
  },
  {
    href: "/product/mcp",
    title: "MCP",
    summary: "The same index from your editor's coding agent.",
  },
];

export function ProductPage({
  title,
  lead,
  points,
  example,
  children,
}: {
  title: string;
  lead: string;
  points: readonly string[];
  /** What the example is, said above it. */
  example: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-10 px-6 py-12 sm:py-16">
      <header className="flex max-w-3xl flex-col gap-3">
        <p className="text-sm text-fg-muted">
          <Link href="/product" className="hover:text-fg hover:underline">
            Product
          </Link>
        </p>
        <h1 className="text-4xl font-semibold tracking-[-0.02em]">{title}</h1>
        <p className="text-lg text-fg-muted">{lead}</p>
      </header>
      <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {points.map((point) => (
          <li key={point} className="flex gap-2 text-sm">
            <span aria-hidden className="mt-2 size-1.5 shrink-0 rounded-full bg-signal" />
            <span>{point}</span>
          </li>
        ))}
      </ul>
      <section aria-labelledby="example-title" className="flex flex-col gap-3">
        <h2 id="example-title" className="text-sm font-medium text-fg-muted">
          {example}
        </h2>
        {children}
      </section>
      <NextPages current={title} />
    </div>
  );
}

function NextPages({ current }: { current: string }) {
  return (
    <nav aria-label="More of the product" className="flex flex-col gap-4 border-t border-line pt-8">
      <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {PRODUCT_PAGES.filter((page) => page.title !== current).map((page) => (
          <li key={page.href}>
            <Link
              href={page.href}
              className="flex h-full flex-col gap-1 rounded-lg border border-line p-4 hover:border-line-strong hover:bg-muted/40"
            >
              <span className="font-medium">{page.title}</span>
              <span className="text-sm text-fg-muted">{page.summary}</span>
            </Link>
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        <Link href="/login" className={buttonVariants()}>
          Get started
        </Link>
        <Link href="/explore" className={buttonVariants({ variant: "outline" })}>
          Explore repositories
        </Link>
      </div>
    </nav>
  );
}
