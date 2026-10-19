import type { Metadata } from "next";

import { SessionsView } from "@/components/settings/sessions-view";

export const metadata: Metadata = { title: "Sessions" };

export default function SessionsPage() {
  return <SessionsView />;
}
