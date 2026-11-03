import type { Metadata } from "next";
import { Suspense } from "react";

import { SecuritySettings } from "@/components/orgs/security-settings";

export const metadata: Metadata = { title: "Security" };

export default function SecuritySettingsPage() {
  return (
    <Suspense>
      <SecuritySettings />
    </Suspense>
  );
}
