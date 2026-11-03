import type { Metadata } from "next";
import { Suspense } from "react";

import { ChangePage } from "@/components/changes/change-page";

export const metadata: Metadata = { title: "Change" };

export default function Page() {
  return (
    <Suspense>
      <ChangePage />
    </Suspense>
  );
}
