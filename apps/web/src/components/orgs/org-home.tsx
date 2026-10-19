"use client";

import { FolderGit2 } from "lucide-react";

import { EmptyState } from "@/components/feedback/empty-state";
import { Button } from "@/components/ui/button";
import { useOrgRole } from "@/hooks/use-org-role";

export function OrgHome() {
  const { org } = useOrgRole();
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 p-8">
      <h1 className="text-2xl font-semibold tracking-[-0.015em]">{org?.name ?? "Home"}</h1>
      <EmptyState
        icon={<FolderGit2 />}
        title="Connect your first repository"
        description="repo-sensei indexes a repository so you can ask questions, trace requests across layers and get fixes with citations. Connecting repositories arrives with the next release."
        action={
          <Button disabled aria-describedby="connect-soon">
            Connect a repository
          </Button>
        }
      />
      <p id="connect-soon" className="sr-only">
        Not available yet.
      </p>
    </div>
  );
}
