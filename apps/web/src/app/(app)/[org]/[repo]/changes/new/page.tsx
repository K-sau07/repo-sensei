import type { Metadata } from "next";
import { Suspense } from "react";

import { NewChangePage } from "@/components/changes/new-change-page";

export const metadata: Metadata = { title: "New change" };

export default function Page() {
  return (
    <Suspense>
      <NewChangePage />
    </Suspense>
  );
}
