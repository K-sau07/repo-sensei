import type { Metadata } from "next";
import { Suspense } from "react";

import { ConsentView } from "@/components/oauth/consent-view";

export const metadata: Metadata = { title: "Allow access", robots: { index: false } };

export default function ConsentPage() {
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <Suspense>
        <ConsentView />
      </Suspense>
    </div>
  );
}
