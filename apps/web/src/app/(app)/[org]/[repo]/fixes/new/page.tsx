import type { Metadata } from "next";
import { Suspense } from "react";

import { NewFixPage } from "@/components/fixes/new-fix-page";

export const metadata: Metadata = { title: "New fix" };

export default function Page() {
  return (
    <Suspense>
      <NewFixPage />
    </Suspense>
  );
}
