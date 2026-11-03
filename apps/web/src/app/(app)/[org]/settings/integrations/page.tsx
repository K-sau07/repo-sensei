import type { Metadata } from "next";
import { Suspense } from "react";

import { SlackSettings } from "@/components/orgs/slack-settings";

export const metadata: Metadata = { title: "Integrations" };

export default function IntegrationsSettingsPage() {
  return (
    <Suspense>
      <SlackSettings />
    </Suspense>
  );
}
