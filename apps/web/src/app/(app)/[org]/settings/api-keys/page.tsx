import type { Metadata } from "next";
import { Suspense } from "react";

import { ApiKeysSettings } from "@/components/integrations/api-keys-settings";

export const metadata: Metadata = { title: "API keys" };

export default function ApiKeysSettingsPage() {
  return (
    <Suspense>
      <ApiKeysSettings />
    </Suspense>
  );
}
