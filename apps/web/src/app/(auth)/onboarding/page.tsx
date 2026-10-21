import type { Metadata } from "next";
import { Suspense } from "react";

import { OnboardingFlow } from "@/components/orgs/onboarding-flow";

export const metadata: Metadata = { title: "Get started" };

export default function OnboardingPage() {
  return (
    <Suspense>
      <OnboardingFlow />
    </Suspense>
  );
}
