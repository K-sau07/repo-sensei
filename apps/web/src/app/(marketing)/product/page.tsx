import type { Metadata } from "next";
import Link from "next/link";

import { PRODUCT_PAGES } from "@/components/marketing/product/product-page";

const DESCRIPTION =
  "One index of a codebase: ask it, trace features through it, draw it, fix it, and use it from your editor.";

export const metadata: Metadata = {
  title: "Product",
  description: DESCRIPTION,
  alternates: { canonical: "/product" },
  openGraph: { title: "Product · repo-sensei", description: DESCRIPTION, url: "/product" },
};

export default function ProductIndexPage() {
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-10 px-6 py-12 sm:py-16">
      <header className="flex max-w-3xl flex-col gap-3">
        <h1 className="text-4xl font-semibold tracking-[-0.02em]">Product</h1>
        <p className="text-lg text-fg-muted">{DESCRIPTION}</p>
      </header>
      <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {PRODUCT_PAGES.map((page) => (
          <li key={page.href}>
            <Link
              href={page.href}
              className="flex h-full flex-col gap-2 rounded-xl border border-line bg-surface-1 p-5 hover:border-line-strong"
            >
              <h2 className="font-medium">{page.title}</h2>
              <p className="text-sm text-fg-muted">{page.summary}</p>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
