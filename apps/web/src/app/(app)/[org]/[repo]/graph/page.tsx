import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Graph" };

export default function GraphPage() {
  return (
    <Suspense>
      <TabPlaceholder tab="graph" />
    </Suspense>
  );
}
