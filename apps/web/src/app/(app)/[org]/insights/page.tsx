import type { Metadata } from "next";
import { Suspense } from "react";

import { InsightsPage } from "@/components/insights/insights-page";

export const metadata: Metadata = { title: "Insights" };

export default function Page() {
  return (
    <Suspense>
      <InsightsPage />
    </Suspense>
  );
}
