import { Suspense } from "react";

import { InternalNav } from "@/components/internal/internal-nav";

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <Suspense>
        <InternalNav />
      </Suspense>
      <main className="flex flex-1 flex-col">{children}</main>
    </>
  );
}
