import type { Metadata } from "next";
import { Suspense } from "react";

import { UsageSettings } from "@/components/orgs/usage-settings";

export const metadata: Metadata = { title: "Usage" };

export default function UsageSettingsPage() {
  return (
    <Suspense>
      <UsageSettings />
    </Suspense>
  );
}
