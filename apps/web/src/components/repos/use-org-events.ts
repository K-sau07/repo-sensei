"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import type { Repo, Stage } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";

interface OrgEvent {
  type: "queued" | "stage_started" | "done" | "failed" | "cancelled";
  job_id: string;
  repo_id: string;
  stage: Stage | null;
  progress?: number;
}

/**
 * Follow the org's event stream (index runs starting stages and ending) and keep the repository
 * list current: progress moves the status dots in place, a finished run refetches. The browser
 * reconnects by itself (resuming from the last event id); one stream per tab.
 */
export function useOrgEvents(org: string | null) {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!org || typeof EventSource === "undefined") return;
    let source: EventSource | null = null;
    let closed = false;
    const key = queryKeys.repos(org);
    // Every repository's own page (by prefix: the repository is in the rest of the key).
    const details = queryKeys.repo(org, "").slice(0, 2);
    const onEvent = (message: MessageEvent<string>) => {
      let event: OrgEvent;
      try {
        event = JSON.parse(message.data) as OrgEvent;
      } catch {
        return;
      }
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
    const open = () => {
      if (closed) return;
      source = new EventSource(`/api/v1/orgs/${encodeURIComponent(org)}/events`);
      for (const type of ["queued", "stage_started", "done", "failed", "cancelled"]) {
        source.addEventListener(type, onEvent);
      }
      source.addEventListener("message", onEvent); // the mock API sends unnamed events
    };
    // In mock mode the stream must start after the mock worker does, or it reaches the dev
    // server's proxy instead. The env check is inline so real builds drop this branch.
    if (process.env.NEXT_PUBLIC_MOCKS === "1") {
      void import("@/mocks/start").then(({ startMocks }) => startMocks()).then(open);
    } else {
      open();
    }
    return () => {
      closed = true;
      source?.close();
    };
  }, [org, queryClient]);
}
