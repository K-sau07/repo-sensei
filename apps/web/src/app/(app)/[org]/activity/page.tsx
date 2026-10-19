import type { Metadata } from "next";

import { ComingSoon } from "@/components/coming-soon";

export const metadata: Metadata = { title: "Activity" };

export default function ActivityPage() {
  return <ComingSoon title="Activity" />;
}
