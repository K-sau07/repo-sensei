import type { Metadata } from "next";
import { Suspense } from "react";

import { ImpactPage } from "@/components/impact/impact-page";

export const metadata: Metadata = { title: "Impact" };

export default function Page() {
  return (
    <Suspense>
      <ImpactPage />
    </Suspense>
  );
}
