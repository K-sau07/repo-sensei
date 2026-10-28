import type { Metadata } from "next";
import { Suspense } from "react";

import { TracePage } from "@/components/trace/trace-page";

export const metadata: Metadata = { title: "Trace" };

export default function RepoTracePage() {
  return (
    <Suspense>
      <TracePage />
    </Suspense>
  );
}
