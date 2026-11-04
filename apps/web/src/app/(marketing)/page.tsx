import type { Metadata } from "next";
import { connection } from "next/server";
import { Suspense } from "react";

import { HeroSky } from "@/components/marketing/landing/hero-sky";
import { HeroSkySvg } from "@/components/marketing/landing/hero-sky-svg";
import { LiveDemo } from "@/components/marketing/landing/live-demo";
import { RepoInput } from "@/components/marketing/landing/repo-input";
import { Reveal } from "@/components/marketing/landing/reveal";
import { ScrollStory } from "@/components/marketing/landing/scroll-story";
import {
  Closing,
  Editors,
  Features,
  ProofStrip,
  Safety,
} from "@/components/marketing/landing/sections";
import { Skeleton } from "@/components/ui/skeleton";
import { featuredRepo, humanCheck } from "@/lib/api/explore-server";
import { heroSky } from "@/lib/demo/hero-sky";
import { DEMO_REPO, DEMO_SHA, storyHops } from "@/lib/demo/product-demo";

const DESCRIPTION =
  "Ask a codebase anything and get answers that cite the lines they stand on. Trace features across layers, draw verified diagrams, and fix bugs with tests that prove it.";

export const metadata: Metadata = {
  title: { absolute: "repo-sensei: understand any codebase" },
  description: DESCRIPTION,
  alternates: { canonical: "/" },
  openGraph: { title: "repo-sensei", description: DESCRIPTION, url: "/" },
};

export default function LandingPage() {
  const sky = heroSky();
  return (
    <div className="flex flex-col">
      <section aria-labelledby="hero-title" className="relative isolate overflow-hidden">
        <div aria-hidden className="hero-glow absolute inset-0 -z-20" />
        <div className="mx-auto flex w-full max-w-4xl flex-col items-center gap-6 px-6 pt-[10svh] text-center">
          <p className="rounded-full border border-line bg-bg/60 px-3 py-1 font-mono text-xs text-fg-muted backdrop-blur">
            <span className="text-signal">●</span> Below: {DEMO_REPO} at {DEMO_SHA}, its {sky.files}{" "}
            files, and the trace of signing in
          </p>
          {/* The largest text on the page and its first paint (the LCP element): no client JS. */}
          <h1
            id="hero-title"
            className="text-4xl font-semibold tracking-[-0.03em] text-balance sm:text-6xl lg:text-7xl"
          >
            Understand any codebase, with{" "}
            <span className="hero-accent">answers you can verify.</span>
          </h1>
          <p className="max-w-2xl text-lg text-pretty text-fg-muted">{DESCRIPTION}</p>
          <RepoInput />
        </div>
        {/* The repository as a globe, centered under the headline: its own space, over nothing. */}
        <div className="hero-globe relative mx-auto mt-6 h-[min(72svh,720px)] w-full max-w-6xl">
          <HeroSky
            sky={sky}
            picture={
              // Where the WebGL globe is: centered in the box, about 92% of its height across.
              <HeroSkySvg
                sky={sky}
                className="absolute top-1/2 left-1/2 aspect-square h-[147%] -translate-x-1/2 -translate-y-1/2"
              />
            }
          />
        </div>
      </section>

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-24 px-6 py-16 sm:py-24">
        <Reveal>
          <Suspense fallback={<Skeleton className="h-80 rounded-2xl" />}>
            <Demo />
          </Suspense>
        </Reveal>
        <ScrollStory hops={storyHops()} repo={`${DEMO_REPO} at ${DEMO_SHA}`} />
        <Reveal>
          <ProofStrip />
        </Reveal>
        <Reveal>
          <Features />
        </Reveal>
        <Reveal>
          <Safety />
        </Reveal>
        <Reveal>
          <Editors />
        </Reveal>
        <Reveal>
          <Closing />
        </Reveal>
      </div>
    </div>
  );
}

async function Demo() {
  await connection(); // the API's, read when asked: never at build (there is no API then)
  return <LiveDemo page={await featuredRepo()} human={humanCheck()} />;
}
