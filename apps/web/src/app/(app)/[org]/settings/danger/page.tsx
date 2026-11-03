import type { Metadata } from "next";
import { Suspense } from "react";

import { OrgDangerZone } from "@/components/orgs/danger-zone";

export const metadata: Metadata = { title: "Danger zone" };

export default function DangerZonePage() {
  return (
    <Suspense>
      <OrgDangerZone />
    </Suspense>
  );
}
