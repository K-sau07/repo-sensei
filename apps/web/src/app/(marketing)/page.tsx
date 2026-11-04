import type { Metadata } from "next";
import { connection } from "next/server";
import { Suspense } from "react";

import { LiveDemo } from "@/components/marketing/landing/live-demo";
import { RepoInput } from "@/components/marketing/landing/repo-input";
import {
  Closing,
  Editors,
  Features,
  ProofStrip,
  Safety,
} from "@/components/marketing/landing/sections";
import { Skeleton } from "@/components/ui/skeleton";
import { featuredRepo, humanCheck, publicRepos } from "@/lib/api/explore-server";

const DESCRIPTION =
  "Ask a codebase anything and get answers that cite the lines they stand on. Trace features across layers, draw verified diagrams, and fix bugs with tests that prove it.";

export const metadata: Metadata = {
  title: { absolute: "repo-sensei: understand any codebase" },
  description: DESCRIPTION,
  alternates: { canonical: "/" },
  openGraph: { title: "repo-sensei", description: DESCRIPTION, url: "/" },
};

export default function LandingPage() {
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-16 px-6 py-12 sm:py-20">
      <section aria-labelledby="hero-title" className="flex flex-col items-start gap-6">
        {/* The largest text on the page and its first paint (the LCP element): no client JS. */}
        <h1
          id="hero-title"
          className="max-w-3xl text-4xl font-semibold tracking-[-0.02em] text-balance sm:text-6xl"
        >
          Understand any codebase, with answers you can verify.
        </h1>
        <p className="max-w-2xl text-lg text-fg-muted">{DESCRIPTION}</p>
        <Suspense fallback={<RepoInput published={[]} />}>
          <Input />
        </Suspense>
      </section>
      <Suspense fallback={<Skeleton className="h-80 rounded-2xl" />}>
        <Demo />
      </Suspense>
      <ProofStrip />
      <Features />
      <Safety />
      <Editors />
      <Closing />
    </div>
  );
}

async function Input() {
  await connection(); // the API's, read when asked: never at build (there is no API then)
  const repos = await publicRepos().catch(() => []);
  return <RepoInput published={repos.map((repo) => repo.full_name)} />;
}

async function Demo() {
  await connection();
  return <LiveDemo page={await featuredRepo()} human={humanCheck()} />;
}
