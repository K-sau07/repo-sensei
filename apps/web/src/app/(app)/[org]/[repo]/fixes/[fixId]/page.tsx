import type { Metadata } from "next";
import { Suspense } from "react";

import { FixPage } from "@/components/fixes/fix-page";

export const metadata: Metadata = { title: "Fix" };

export default function Page() {
  return (
    <Suspense>
      <FixPage />
    </Suspense>
  );
}
