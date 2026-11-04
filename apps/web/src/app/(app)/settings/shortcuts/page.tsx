import type { Metadata } from "next";

import { ShortcutsView } from "@/components/settings/shortcuts-view";

export const metadata: Metadata = { title: "Keyboard shortcuts" };

export default function ShortcutsPage() {
  return <ShortcutsView />;
}
