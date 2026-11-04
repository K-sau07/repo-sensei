import type { Metadata } from "next";

import { AppearanceView } from "@/components/settings/appearance-view";

export const metadata: Metadata = { title: "Appearance" };

export default function AppearancePage() {
  return <AppearanceView />;
}
