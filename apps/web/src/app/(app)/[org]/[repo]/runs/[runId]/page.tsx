import type { Metadata } from "next";
import { Suspense } from "react";

import { RunView } from "@/components/runs/run-view";

export const metadata: Metadata = { title: "Run" };

export default function RunPage() {
  return (
    <Suspense>
      <RunView />
    </Suspense>
  );
}
