import type { Metadata } from "next";
import { Suspense } from "react";

import { OrgGeneralSettings } from "@/components/orgs/org-general-settings";

export const metadata: Metadata = { title: "General settings" };

export default function GeneralSettingsPage() {
  return (
    <Suspense>
      <OrgGeneralSettings />
    </Suspense>
  );
}
