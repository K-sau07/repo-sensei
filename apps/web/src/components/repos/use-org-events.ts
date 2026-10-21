"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import type { Repo, Stage } from "@/lib/api/types";
import { followStream } from "@/lib/event-stream";
import { queryKeys } from "@/lib/query/keys";

interface OrgEvent {
  type: "queued" | "stage_started" | "done" | "failed" | "cancelled";
  job_id: string;
  repo_id: string;
  stage: Stage | null;
  progress?: number;
}

/**
 * Follow the org's event stream (index runs starting stages and ending) and keep what shows
 * index runs current: progress moves the status dots in place, and each event refreshes the
 * repository's job list (tab locks, the overview) and, when a run ends, its page. The stream
 * reconnects and resumes from the last event id (see `followStream`); one stream per tab.
 */
export function useOrgEvents(org: string | null) {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!org || typeof EventSource === "undefined") return;
    const key = queryKeys.repos(org);
    // Every repository's own page and job list (by prefix: the repository is in the rest).
    const details = queryKeys.repo(org, "").slice(0, 2);
    const allJobLists = queryKeys.indexJobs(org, "").slice(0, 2);
    let lastId: string | null = null;

    const refreshJobs = (repoId: string) => {
      const slug = queryClient.getQueryData<Repo[]>(key)?.find((repo) => repo.id === repoId)?.slug;
      void queryClient.invalidateQueries({
        queryKey: slug ? queryKeys.indexJobs(org, slug) : allJobLists,
      });
    };

    const onEvent = (message: MessageEvent<string>) => {
      let event: OrgEvent;
      try {
        event = JSON.parse(message.data) as OrgEvent;
      } catch {
        return;
      }
      if (message.lastEventId) lastId = message.lastEventId;
      refreshJobs(event.repo_id);
      if (event.type === "stage_started" || event.type === "queued") {
        const known = queryClient
          .getQueryData<Repo[]>(key)
          ?.some((repo) => repo.id === event.repo_id);
        queryClient.setQueryData<Repo[]>(key, (repos) =>
          repos?.map((repo) => {
            if (repo.id !== event.repo_id) return repo;
            return {
              ...repo,
              status: event.type === "queued" ? repo.status : "indexing",
              active_job: {
                id: event.job_id,
                status: event.type === "queued" ? "queued" : "running",
                stage: event.stage ?? null,
                progress: event.progress ?? repo.active_job?.progress ?? 0,
                eta_s: repo.active_job?.eta_s ?? null,
              },
            };
          }),
        );
        if (!known) void queryClient.invalidateQueries({ queryKey: key });
        void queryClient.invalidateQueries({ queryKey: details });
        return;
      }
      // A run ended: the list and the repository's own page should show the outcome.
      void queryClient.invalidateQueries({ queryKey: key });
      void queryClient.invalidateQueries({ queryKey: details });
    };

    return followStream(`/api/v1/orgs/${encodeURIComponent(org)}/events`, {
      types: ["queued", "stage_started", "done", "failed", "cancelled"],
      onEvent, // the mock API sends unnamed events, which come as "message"
      onLive: () => undefined,
      lastId: () => lastId,
    });
  }, [org, queryClient]);
}
