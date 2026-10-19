import type { Metadata } from "next";
import { Suspense } from "react";

import { InvitationView } from "@/components/orgs/invitation-view";

export const metadata: Metadata = { title: "Invitation", robots: { index: false } };

export default function InvitationPage() {
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <Suspense>
        <InvitationView />
      </Suspense>
    </div>
  );
}
