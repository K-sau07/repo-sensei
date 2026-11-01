import type { Metadata } from "next";
import { Suspense } from "react";

import { DiagramPage } from "@/components/diagrams/diagram-page";

export const metadata: Metadata = { title: "Diagram" };

export default function Page() {
  return (
    <Suspense>
      <DiagramPage />
    </Suspense>
  );
}
