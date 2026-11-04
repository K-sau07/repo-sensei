import type { Metadata } from "next";
import { Suspense } from "react";

import { GitHubInstallations } from "@/components/orgs/github-installations";
import { SentrySettings } from "@/components/orgs/sentry-settings";
import { SlackSettings } from "@/components/orgs/slack-settings";

export const metadata: Metadata = { title: "Integrations" };

export default function IntegrationsSettingsPage() {
  return (
    <Suspense>
      <div className="flex flex-col gap-10">
        <GitHubInstallations />
        <SentrySettings />
        <SlackSettings />
      </div>
    </Suspense>
  );
}
