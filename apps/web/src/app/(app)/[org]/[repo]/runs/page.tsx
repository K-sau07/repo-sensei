import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Runs" };

export default function RunsPage() {
  return (
    <Suspense>
      <TabPlaceholder tab="runs" />
    </Suspense>
  );
}
