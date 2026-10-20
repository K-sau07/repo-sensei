import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Index" };

export default function IndexPage() {
  return (
    <Suspense>
      <TabPlaceholder tab="index" />
    </Suspense>
  );
}
