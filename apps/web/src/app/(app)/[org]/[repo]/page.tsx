import { Suspense } from "react";

import { RepoLandingPage } from "@/components/repos/repo-pages";

export default function RepoPage() {
  return (
    <Suspense>
      <RepoLandingPage />
    </Suspense>
  );
}
