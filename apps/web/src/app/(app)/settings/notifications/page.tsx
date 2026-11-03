import type { Metadata } from "next";

import { NotificationsView } from "@/components/settings/notifications-view";

export const metadata: Metadata = { title: "Notifications" };

export default function NotificationsPage() {
  return <NotificationsView />;
}
