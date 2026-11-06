import type { Metadata } from "next";
import { Suspense } from "react";

import { EvalRunPage } from "@/components/internal/eval-run-page";

export const metadata: Metadata = { title: "Eval run", robots: { index: false } };

export default function Page() {
  return (
    <Suspense>
      <EvalRunPage />
    </Suspense>
  );
}
