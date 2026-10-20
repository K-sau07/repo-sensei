import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Changes" };

export default function ChangesPage() {
  return (
    <Suspense>
      <TabPlaceholder tab="changes" />
    </Suspense>
  );
}
