import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Ask" };

export default function AskPage() {
  return (
    <Suspense>
      <TabPlaceholder tab="ask" />
    </Suspense>
  );
}
