import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Impact" };

export default function ImpactPage() {
  return (
    <Suspense>
      <TabPlaceholder tab="impact" />
    </Suspense>
  );
}
