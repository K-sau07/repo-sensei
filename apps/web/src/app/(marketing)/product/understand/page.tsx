import type { Metadata } from "next";
import { connection } from "next/server";
import { Suspense } from "react";

import { LiveDemo } from "@/components/marketing/landing/live-demo";
import { ProductPage } from "@/components/marketing/product/product-page";
import { Skeleton } from "@/components/ui/skeleton";
import { featuredRepo, humanCheck } from "@/lib/api/explore-server";

const LEAD =
  "Ask about a codebase in plain words. The answer comes from its code, and each claim points at the lines it stands on.";

export const metadata: Metadata = {
  title: "Understand",
  description: LEAD,
  alternates: { canonical: "/product/understand" },
};

const POINTS = [
  "Each claim is checked against the lines it cites, and each citation says whether they hold it up.",
  "When the code doesn't answer the question, the answer says so instead of guessing.",
  "Citations open the exact lines, at the commit the answer read.",
  "Answers say when the code they cite has changed since.",
  "Search by exact text, by keyword or by meaning, and go to any symbol's definition.",
  "Share an answer by link; whoever can't read the repository sees it without the code.",
] as const;

export default function UnderstandPage() {
  return (
    <ProductPage
      title="Understand"
      lead={LEAD}
      points={POINTS}
      example="Live: ask a published open-source repository"
    >
      <Suspense fallback={<Skeleton className="h-64 rounded-2xl" />}>
        <Demo />
      </Suspense>
    </ProductPage>
  );
}

async function Demo() {
  await connection();
  return <LiveDemo page={await featuredRepo()} human={humanCheck()} />;
}
