import type { Metadata } from "next";
import { Suspense } from "react";

import { FixesPage } from "@/components/fixes/fixes-page";

export const metadata: Metadata = { title: "Fixes" };

export default function Page() {
  return (
    <Suspense>
      <FixesPage />
    </Suspense>
  );
}
