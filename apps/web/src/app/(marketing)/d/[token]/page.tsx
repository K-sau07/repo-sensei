import type { Metadata } from "next";
import { Suspense } from "react";

import { SharedDiagramView } from "@/components/diagrams/shared-diagram";

export const metadata: Metadata = { title: "Shared diagram", robots: { index: false } };

export default function SharedDiagramPage() {
  return (
    <Suspense>
      <SharedDiagramView />
    </Suspense>
  );
}
