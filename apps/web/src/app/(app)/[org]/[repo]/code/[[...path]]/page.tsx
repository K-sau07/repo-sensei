import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Code" };

export default function CodePage() {
  return (
    <Suspense>
      <TabPlaceholder tab="code" />
    </Suspense>
  );
}
