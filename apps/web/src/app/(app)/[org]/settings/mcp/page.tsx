import type { Metadata } from "next";
import { Suspense } from "react";

import { McpSettings } from "@/components/integrations/mcp-settings";

export const metadata: Metadata = { title: "Connect your editor" };

export default function McpSettingsPage() {
  return (
    <Suspense>
      <McpSettings />
    </Suspense>
  );
}
