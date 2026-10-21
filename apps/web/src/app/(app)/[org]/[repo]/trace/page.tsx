import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Trace" };

export default function TracePage() {
  return (
    <Suspense>
      <TabPlaceholder tab="trace" />
    </Suspense>
  );
}
