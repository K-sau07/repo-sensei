import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Fixes" };

export default function FixesPage() {
  return (
    <Suspense>
      <TabPlaceholder tab="fixes" />
    </Suspense>
  );
}
