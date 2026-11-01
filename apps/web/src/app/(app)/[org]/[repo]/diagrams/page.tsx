import type { Metadata } from "next";
import { Suspense } from "react";

import { DiagramsPage } from "@/components/diagrams/diagrams-page";

export const metadata: Metadata = { title: "Diagrams" };

export default function Page() {
  return (
    <Suspense>
      <DiagramsPage />
    </Suspense>
  );
}
