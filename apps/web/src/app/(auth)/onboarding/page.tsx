import type { Metadata } from "next";

import { ComingSoon } from "@/components/coming-soon";

export const metadata: Metadata = { title: "Get started" };

export default function OnboardingPage() {
  return <ComingSoon title="Get started" />;
}
