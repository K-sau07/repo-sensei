import type { Metadata } from "next";

import { ComingSoon } from "@/components/coming-soon";

export const metadata: Metadata = { title: "Inbox" };

export default function InboxPage() {
  return <ComingSoon title="Inbox" />;
}
