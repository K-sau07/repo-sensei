import { Check } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";
import { SITE_CARD } from "@/lib/site/author";

const DESCRIPTION =
  "Try repo-sensei free on one repository. Pro, for more repositories, is coming soon.";

export const metadata: Metadata = {
  title: "Pricing",
  description: DESCRIPTION,
  alternates: { canonical: "/pricing" },
  openGraph: {
    title: "Pricing · repo-sensei",
    description: DESCRIPTION,
    url: "/pricing",
    images: [SITE_CARD],
  },
};

const FREE = [
  "One repository of up to 50,000 lines, private through the GitHub App or public by URL",
  "Two questions answered with citations, and two diagrams drawn by the model",
  "Search 50 times a day; trace, dependencies, impact and the schema diagram as much as you like",
  "MCP for your editor, and the command line, within the same limits",
  "Re-index it twice by hand (pushes don't re-index on the trial)",
] as const;

export default function PricingPage() {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-10 px-6 py-12 sm:py-16">
      <header className="flex max-w-2xl flex-col gap-3">
        <h1 className="text-4xl font-semibold tracking-[-0.02em]">Pricing</h1>
        <p className="text-lg text-fg-muted">{DESCRIPTION}</p>
      </header>
      <div className="grid gap-4 md:grid-cols-2">
        <section
          aria-labelledby="plan-free"
          className="flex flex-col gap-4 rounded-2xl border border-line-strong bg-surface-1 p-6"
        >
          <div className="flex flex-col gap-1">
            <h2 id="plan-free" className="text-xl font-semibold">
              Free trial
            </h2>
            <p className="text-3xl font-semibold">Free</p>
            <p className="text-sm text-fg-muted">
              Enough to see every part of it work. The trial is yours once: disconnecting the
              repository or making a new organization doesn&apos;t start another.
            </p>
          </div>
          <ul className="flex flex-col gap-2 text-sm">
            {FREE.map((item) => (
              <li key={item} className="flex gap-2">
                <Check aria-hidden className="mt-0.5 size-4 shrink-0 text-ok" />
                {item}
              </li>
            ))}
          </ul>
          <Link href="/login" className={buttonVariants({ className: "mt-auto w-fit" })}>
            Get started
          </Link>
        </section>
        <section
          aria-labelledby="plan-pro"
          className="flex flex-col gap-4 rounded-2xl border border-dashed border-line p-6"
        >
          <div className="flex flex-col gap-1">
            <h2 id="plan-pro" className="text-xl font-semibold">
              Pro
            </h2>
            <p className="text-3xl font-semibold text-fg-muted">Coming soon</p>
            <p className="text-sm text-fg-muted">
              More repositories, for you and your team. Its price and what it includes will be
              announced here. Until then, to index more than one repository, open an issue on GitHub
              and ask.
            </p>
          </div>
        </section>
      </div>
      <p className="text-sm text-fg-muted">
        Questions? See the{" "}
        <Link href="/docs/faq" className="underline">
          FAQ
        </Link>
        , or open an issue on{" "}
        <a
          href="https://github.com/K-sau07/repo-sensei/issues"
          className="underline"
          rel="noreferrer noopener"
        >
          GitHub
        </a>
        .
      </p>
    </div>
  );
}
