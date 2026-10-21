import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Wiki" };

export default function WikiPage() {
  return (
    <Suspense>
      <TabPlaceholder tab="wiki" />
    </Suspense>
  );
}
