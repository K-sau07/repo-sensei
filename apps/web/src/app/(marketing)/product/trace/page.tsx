import type { Metadata } from "next";

import { ProductPage } from "@/components/marketing/product/product-page";
import { TraceDemo } from "@/components/marketing/product/trace-demo";
import { DEMO_REPO, DEMO_SHA, demoTracePath } from "@/lib/demo/product-demo";

const LEAD =
  "Follow a feature through the whole codebase: from the component a person clicks, through state and the API client, to the route, its handler and the tables it reads.";

export const metadata: Metadata = {
  title: "Trace",
  description: LEAD,
  alternates: { canonical: "/product/trace" },
};

const POINTS = [
  "Hops cross languages: a frontend call is matched to the backend route that serves it.",
  "Where a trace breaks, it shows the gap and why, instead of guessing past it.",
  "A call two endpoints could serve gets a path through each.",
  "Every hop opens its code; any hop shows what a change to it would reach.",
  "A pinned trace says when the code under it has changed.",
] as const;

export default function TracePage() {
  return (
    <ProductPage
      title="Trace"
      lead={LEAD}
      points={POINTS}
      example={`Example: signing in to ${DEMO_REPO} at ${DEMO_SHA}, as the engine traced it`}
    >
      <TraceDemo path={demoTracePath()} />
    </ProductPage>
  );
}
