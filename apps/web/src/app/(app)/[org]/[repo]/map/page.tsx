import type { Metadata } from "next";
import { Suspense } from "react";

import { GalaxyPage } from "@/components/galaxy/galaxy-page";

export const metadata: Metadata = { title: "Map" };

export default function Page() {
  return (
    <Suspense>
      <GalaxyPage />
    </Suspense>
  );
}
