import { Suspense } from "react";

import { RepoHeaderSkeleton } from "@/components/repo/repo-header";
import { RepoLayout } from "@/components/repo/repo-layout";

export default function RepoWorkspaceLayout({ children }: LayoutProps<"/[org]/[repo]">) {
  return (
    <Suspense fallback={<RepoHeaderSkeleton />}>
      <RepoLayout>{children}</RepoLayout>
    </Suspense>
  );
}
