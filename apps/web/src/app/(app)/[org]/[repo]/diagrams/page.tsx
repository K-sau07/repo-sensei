import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Diagrams" };

export default function DiagramsPage() {
  return (
    <Suspense>
      <TabPlaceholder tab="diagrams" />
    </Suspense>
  );
}
