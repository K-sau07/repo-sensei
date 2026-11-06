import type { Metadata } from "next";
import { Suspense } from "react";

import { AdminPage } from "@/components/internal/admin-page";

export const metadata: Metadata = { title: "Admin", robots: { index: false } };

export default function Page() {
  return (
    <Suspense>
      <AdminPage />
    </Suspense>
  );
}
