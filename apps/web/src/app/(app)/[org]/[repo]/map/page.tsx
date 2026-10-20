import type { Metadata } from "next";
import { Suspense } from "react";

import { TabPlaceholder } from "@/components/repo/tab-placeholder";

export const metadata: Metadata = { title: "Map" };

export default function MapPage() {
  return (
    <Suspense>
      <TabPlaceholder tab="map" />
    </Suspense>
  );
}
