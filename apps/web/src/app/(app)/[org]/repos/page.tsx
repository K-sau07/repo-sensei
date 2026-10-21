import type { Metadata } from "next";
import { Suspense } from "react";

import { RepoListPage } from "@/components/repos/repo-pages";

export const metadata: Metadata = { title: "Repositories" };

export default function ReposPage() {
  return (
    <Suspense>
      <RepoListPage />
    </Suspense>
  );
}
