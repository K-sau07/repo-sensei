import type { Metadata } from "next";
import { Suspense } from "react";

import { SearchPage } from "@/components/search/search-page";

export const metadata: Metadata = { title: "Search" };

export default function RepoSearchPage() {
  return (
    <Suspense>
      <SearchPage />
    </Suspense>
  );
}
