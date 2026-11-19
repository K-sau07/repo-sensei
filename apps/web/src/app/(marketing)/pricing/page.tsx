import { Check } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";

const DESCRIPTION =
  "Try repo-sensei free on one repository. Pro, for more repositories, is coming soon.";

export const metadata: Metadata = {
  title: "Pricing",
  description: DESCRIPTION,
  alternates: { canonical: "/pricing" },
  openGraph: { title: "Pricing · repo-sensei", description: DESCRIPTION, url: "/pricing" },
};

const FREE = [
  "One repository, private through the GitHub App or public by URL, kept up to date",
  "Ask, Trace, Diagrams, Dependencies and Impact",
  "Fixes and changes, verified in a sandbox",
  "MCP for your editor, and the command line",
  "Your team, with roles and an audit log",
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
              One repository, once: disconnecting it doesn&apos;t free up another. Within the limits
              this server sets: questions a day, a daily spending cap, the size of public imports.
              Your organization&apos;s Usage settings show them.
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
