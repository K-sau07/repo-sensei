import type { Metadata } from "next";
import { Suspense } from "react";

import { InboxPage } from "@/components/inbox/inbox-page";

export const metadata: Metadata = { title: "Inbox" };

export default function Inbox() {
  return (
    <Suspense>
      <InboxPage />
    </Suspense>
  );
}
