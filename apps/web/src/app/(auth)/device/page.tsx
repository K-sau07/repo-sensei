import type { Metadata } from "next";
import { Suspense } from "react";

import { DeviceView } from "@/components/oauth/device-view";

export const metadata: Metadata = { title: "Sign in on a device", robots: { index: false } };

export default function DevicePage() {
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <Suspense>
        <DeviceView />
      </Suspense>
    </div>
  );
}
