"use client";

import { useParams } from "next/navigation";

import { useOrgSlug } from "@/components/shell/use-org-slug";
import { $api } from "@/lib/api/client";
import type { Stage } from "@/lib/api/types";
import type { Feature } from "@/lib/indexing";

/** The repository in the URL (`/[org]/[repo]/…`). */
export function useRepoSlug(): { org: string; repo: string } {
  const org = useOrgSlug();
  const { repo } = useParams<{ repo: string }>();
  return { org, repo };
}

export function useRepo() {
  const { org, repo } = useRepoSlug();
  return $api.useQuery("get", "/api/v1/orgs/{org}/repos/{repo}", {
    params: { path: { org, repo } },
  });
}

export interface FeatureState {
  available: boolean;
  /** The stage that unlocks it. */
  stage: Stage | null;
  /** Seconds until it is expected, when the run that brings it is under way. */
  etaSeconds: number | null;
  /** That run is under way. */
  coming: boolean;
}

/**
 * What the repository can do: everything the last successful index unlocked, plus what the run
 * in progress has unlocked so far. Updated live: the org and job event streams refetch the job
 * list.
 */
export function useRepoFeatures() {
  const { org, repo } = useRepoSlug();
  const stages = $api.useQuery("get", "/api/v1/indexing/stages", {}, { staleTime: Infinity });
  const jobs = $api.useQuery("get", "/api/v1/orgs/{org}/repos/{repo}/index-jobs", {
    params: { path: { org, repo } },
  });

  const history = jobs.data ?? [];
  const active = history.find((job) => job.status === "queued" || job.status === "running");
  const lastGood = history.find((job) => job.status === "succeeded");
  const available = new Set<Feature>([...(lastGood?.features ?? []), ...(active?.features ?? [])]);
  const unlockedBy = new Map<Feature, Stage>();
  for (const info of stages.data?.stages ?? []) {
    for (const feature of info.unlocks) unlockedBy.set(feature, info.stage);
  }

  const state = (feature: Feature): FeatureState => {
    const stage = unlockedBy.get(feature) ?? null;
    const coming = !available.has(feature) && active !== undefined;
    const atStage = coming && active.stage === stage;
    return {
      available: available.has(feature),
      stage,
      etaSeconds: atStage ? (active.eta_s ?? null) : null,
      coming,
    };
  };
  return {
    state,
    ready: jobs.isSuccess && stages.isSuccess,
    failed: jobs.isError || stages.isError,
    retry: () => {
      if (jobs.isError) void jobs.refetch();
      if (stages.isError) void stages.refetch();
    },
    active: active ?? null,
  };
}
