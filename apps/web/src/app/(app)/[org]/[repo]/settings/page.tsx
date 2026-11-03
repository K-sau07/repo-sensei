import type { Metadata } from "next";
import { Suspense } from "react";

import { RepoSettings } from "@/components/repo/repo-settings";

export const metadata: Metadata = { title: "Settings" };

export default function SettingsPage() {
  return (
    <Suspense>
      <RepoSettings />
    </Suspense>
  );
}
