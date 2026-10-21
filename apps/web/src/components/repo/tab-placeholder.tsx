"use client";

import { Construction, Lock } from "lucide-react";
import Link from "next/link";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { LockedState } from "@/components/feedback/locked-state";
import { Skeleton } from "@/components/ui/skeleton";
import { STAGE_LABELS } from "@/lib/indexing";
import { repoPath } from "@/lib/routes";

import { ALL_TABS } from "./repo-tabs-config";
import { useRepoFeatures, useRepoSlug } from "./use-repo";

/** A tab whose page isn't built yet: locked until its indexing stage is done, then "coming in
 * phase X". */
export function TabPlaceholder({ tab: id }: { tab: string }) {
  const { org, repo } = useRepoSlug();
  const { state, ready, failed, retry } = useRepoFeatures();
  const tab = ALL_TABS.find((item) => item.id === id);
  if (!tab) return null;
  if (failed) {
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col p-8">
        <ErrorState
          title={`Couldn't check whether ${tab.label} is ready`}
          description="Its index status didn't load."
          onRetry={retry}
        />
      </div>
    );
  }
  if (!ready) {
    return (
      <div
        className="mx-auto flex w-full max-w-3xl flex-col p-8"
        role="status"
        aria-label="Loading"
      >
        <Skeleton className="h-40" />
      </div>
    );
  }
  const feature = tab.feature ? state(tab.feature) : null;
  const stage = feature?.stage ? STAGE_LABELS[feature.stage] : "indexing";
  let content;
  if (feature && !feature.available && feature.coming) {
    content = (
      <LockedState
        feature={tab.label}
        stage={stage}
        {...(feature.etaSeconds !== null ? { etaSeconds: feature.etaSeconds } : {})}
        href={repoPath(org, repo, "index")}
      />
    );
  } else if (feature && !feature.available && !tab.phase) {
    content = (
      <EmptyState
        icon={<Lock />}
        title={`${tab.label} isn't available yet`}
        description={`It unlocks when ${stage} has run on this repository.`}
        action={
          <Link
            href={repoPath(org, repo, "index")}
            className="text-sm underline underline-offset-4"
          >
            Indexing
          </Link>
        }
      />
    );
  } else {
    content = (
      <EmptyState
        icon={<Construction />}
        title={`${tab.label} is on its way`}
        description={
          tab.phase
            ? `This page arrives in phase ${String(tab.phase)} of the build.`
            : "This page arrives in a later step of this phase."
        }
      />
    );
  }
  return <div className="mx-auto flex w-full max-w-3xl flex-col p-8">{content}</div>;
}
