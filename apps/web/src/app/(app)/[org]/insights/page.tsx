import type { Metadata } from "next";

import { ComingSoon } from "@/components/coming-soon";

export const metadata: Metadata = { title: "Insights" };

export default function InsightsPage() {
  return <ComingSoon title="Insights" />;
}
