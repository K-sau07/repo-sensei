import type { Metadata } from "next";
import { Suspense } from "react";

import { EvalsList } from "@/components/internal/evals-list";

export const metadata: Metadata = { title: "Evals", robots: { index: false } };

export default function InternalPage() {
  return (
    <Suspense>
      <EvalsList />
    </Suspense>
  );
}
