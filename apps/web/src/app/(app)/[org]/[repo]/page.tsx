import { Suspense } from "react";

import { RepoOverview } from "@/components/indexing/index-pages";

export default function RepoPage() {
  return (
    <Suspense>
      <RepoOverview />
    </Suspense>
  );
}
