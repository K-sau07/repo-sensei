import type { Metadata } from "next";
import { Suspense } from "react";

import { ChangesPage } from "@/components/changes/changes-page";

export const metadata: Metadata = { title: "Changes" };

export default function Page() {
  return (
    <Suspense>
      <ChangesPage />
    </Suspense>
  );
}
