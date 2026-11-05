import type { Metadata } from "next";
import { Suspense } from "react";

import { ModelSettings } from "@/components/orgs/model-settings";

export const metadata: Metadata = { title: "Models" };

export default function ModelSettingsPage() {
  return (
    <Suspense>
      <ModelSettings />
    </Suspense>
  );
}
