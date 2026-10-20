import type { Metadata } from "next";
import { Suspense } from "react";

import { ConnectRepoPage } from "@/components/repos/repo-pages";

export const metadata: Metadata = { title: "Connect a repository" };

export default function NewRepoPage() {
  return (
    <Suspense>
      <ConnectRepoPage />
    </Suspense>
  );
}
