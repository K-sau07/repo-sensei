import type { Metadata } from "next";
import { Suspense } from "react";

import { NewRepoHandoff } from "@/components/repos/new-repo-handoff";

export const metadata: Metadata = { title: "Connect a repository" };

export default function NewPage() {
  return (
    <Suspense>
      <NewRepoHandoff />
    </Suspense>
  );
}
