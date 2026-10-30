import type { Metadata } from "next";
import { Suspense } from "react";

import { SharedConversationView } from "@/components/ask/shared-view";

export const metadata: Metadata = { title: "Shared conversation", robots: { index: false } };

export default function SharedPage() {
  return (
    <Suspense>
      <SharedConversationView />
    </Suspense>
  );
}
