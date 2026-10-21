import type { Metadata } from "next";
import { Suspense } from "react";

import { MembersSettings } from "@/components/orgs/members-settings";

export const metadata: Metadata = { title: "Members" };

export default function MembersSettingsPage() {
  return (
    <Suspense>
      <MembersSettings />
    </Suspense>
  );
}
