import type { Metadata } from "next";
import { Suspense } from "react";

import { GraphPage } from "@/components/graph/graph-page";

export const metadata: Metadata = { title: "Graph" };

export default function Page() {
  return (
    <Suspense>
      <GraphPage />
    </Suspense>
  );
}
